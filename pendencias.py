# -*- coding: utf-8 -*-
"""Pendências de cada rota e quem acionar, com o texto de cobrança pronto.

- Queda geral: QUEDA_GERAL_MIN_ROTAS ou mais rotas perdendo o sinal no mesmo ciclo. No
  histórico, ciclos com 1 a 4 quedas são o normal; com 5 ou mais, falha comum (Shopee ou
  integração), não dos caminhões. Quedas nos QUEDA_GERAL_JANELA_MIN seguintes entram na
  mesma queda. O % de espelhamento continua pela regra da Shopee.
- Causa do NOK: Angellira com posição recente do veículo = falha da integração
  Angellira -> Shopee; posição velha = rastreador; sem SM no procedimento da Shopee = GR.
- Saída: CPT planejado (DBLH) vencido sem CPT realizado, veículo que não chegou na
  origem, e viagem perto do CPT ou já em viagem sem SM no procedimento da Shopee."""

import logging
import os
import re
import unicodedata
from datetime import datetime, timedelta

QUEDA_GERAL_MIN_ROTAS = int(os.getenv("QUEDA_GERAL_MIN_ROTAS", "5"))
# Com muitas rotas, 5 quedas juntas podem ser acaso: exige também esta fração das monitoradas.
QUEDA_GERAL_MIN_PCT = float(os.getenv("QUEDA_GERAL_MIN_PCT", "10"))
QUEDA_GERAL_JANELA_MIN = int(os.getenv("QUEDA_GERAL_JANELA_MIN", "30"))
POSICAO_RECENTE_MIN = int(os.getenv("POSICAO_RECENTE_MIN", "30"))
SAIDA_TOLERANCIA_MIN = int(os.getenv("SAIDA_TOLERANCIA_MIN", "30"))
SM_ANTECEDENCIA_MIN = int(os.getenv("SM_ANTECEDENCIA_MIN", "60"))
PROCEDIMENTO_SHOPEE = "CEVA – SHOPEE (LINE HAUL 3PL)"

GERAL, ANGELLIRA, TRANSPORTADORA, GR, ORIGEM = (
    "Shopee / Angellira", "Angellira", "Transportadora", "GR", "Origem (carregamento/emissão)")
SAUDACAO = "Olá! Aqui é a Torre LH da CEVA."


def _dt(iso):
    try:
        return datetime.fromisoformat(iso) if iso else None
    except (TypeError, ValueError):
        return None


def _dia_hora(d) -> str:
    return d.strftime("%d/%m %H:%M") if d else ""


def _duracao(delta: timedelta) -> str:
    h, m = divmod(max(0, int(delta.total_seconds() // 60)), 60)
    return f"{h}h{m:02d}"


def _sem_ano(texto) -> str:
    return re.sub(r"(\d{2}/\d{2})/\d{4}", r"\1", str(texto or ""))


def _num(v):
    try:
        return float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _chave(texto) -> str:
    t = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().upper()
    return re.sub(r"[^A-Z0-9]", "", t)


def marcar_quedas_gerais(state: dict, ativos: list, eventos: list, now: datetime) -> list:
    """Marca 'queda_geral' (o início da falha) nas rotas que perderam o sinal juntas neste
    ciclo, e as PERDAS delas no histórico. Devolve as rotas marcadas."""
    agora = now.isoformat()
    novas = [t for t in ativos if state[t].get("queda_detectada_em") == agora]
    if not novas:
        return []
    monitoradas = sum(1 for t in ativos if state[t].get("last_status") in ("OK", "NOK"))
    janela = timedelta(minutes=QUEDA_GERAL_JANELA_MIN)
    recente = max((state[t]["queda_geral"] for t in ativos
                   if state[t].get("queda_geral") and state[t].get("current_loss_start")
                   and now - (_dt(state[t]["queda_geral"]) or now) <= janela), default=None)
    if len(novas) >= QUEDA_GERAL_MIN_ROTAS and len(novas) * 100 >= QUEDA_GERAL_MIN_PCT * monitoradas:
        inicio = recente or min(state[t]["current_loss_start"] for t in novas)
        logging.warning("Queda geral do espelhamento (desde %s): %s rota(s) perderam o sinal juntas neste ciclo, "
                        "de %s monitorada(s).", _dia_hora(_dt(inicio)), len(novas), monitoradas)
    elif recente:
        inicio = recente
        logging.info("Queda geral do espelhamento (desde %s): mais %s rota(s) entraram nela.",
                     _dia_hora(_dt(inicio)), len(novas))
    else:
        return []
    for t in novas:
        state[t]["queda_geral"] = inicio
    for ev in eventos:
        if ev["evento"] == "PERDA" and ev["trip_number"] in novas:
            ev["queda_geral"] = "Sim"
    return novas


def grupos_abertos(state: dict, ativos: list) -> dict:
    """{início: [rotas]} das quedas gerais com 2 ou mais rotas ainda sem sinal. Com uma só,
    ela volta a ter a causa própria (integração, rastreador, SM)."""
    grupos = {}
    for t in ativos:
        d = state[t]
        if d.get("queda_geral") and d.get("current_loss_start"):
            grupos.setdefault(d["queda_geral"], []).append(t)
    return {k: v for k, v in grupos.items() if len(v) >= 2}


def _placas(r: dict) -> str:
    return " / ".join(p for p in (r.get("Placa"), r.get("Placa 2")) if p) or "sem placa"


def _identificacao(r: dict) -> str:
    return (f"A viagem {r['Trip Number']} (placas {_placas(r)}, "
            f"{r.get('Origin Station Code') or '?'} → {r.get('Destination Station Code') or '?'})")


def _pendencia(tipo, texto, acionar, mensagem=""):
    return {"tipo": tipo, "texto": texto, "acionar": acionar,
            "mensagem": f"{SAUDACAO}\n{mensagem}" if mensagem else ""}


def _causa_nok(r, dados, sem_sm, outro_procedimento, grupos):
    desde = _dia_hora(_dt(dados.get("current_loss_start")))
    geral = dados.get("queda_geral")
    if geral in grupos:
        return "geral", _pendencia("Sem sinal", f"Queda geral do espelhamento desde {_dia_hora(_dt(geral))} "
                                                f"({len(grupos[geral])} rotas)", GERAL)
    min_pos = _num(r.get("Min. Sem Posição"))
    posicao = f"{r.get('Cidade/UF') or 'posição'} às {_sem_ano(r.get('Data da Posição')) or '?'}"
    if r.get("Rastreamento") == "OK" and min_pos is not None and min_pos <= POSICAO_RECENTE_MIN:
        return "integracao", _pendencia(
            "Sem sinal", f"Integração Angellira → Shopee: a Angellira tem posição recente ({posicao})", ANGELLIRA,
            f"{_identificacao(r)} está sem espelhamento na Shopee desde {desde}, mas a Angellira tem posição "
            f"atualizada do veículo ({posicao}).\nPodem verificar a integração Angellira → Shopee dessa SM?")
    if r.get("Rastreamento") == "OK":
        idade = f", há {int(min_pos)} min" if min_pos is not None else ""
        motorista = f" ({r['Motorista']})" if r.get("Motorista") else ""
        return "rastreador", _pendencia(
            "Sem sinal", f"Rastreador sem posição: última na Angellira {posicao}{idade}", TRANSPORTADORA,
            f"{_identificacao(r)} está sem sinal na Shopee desde {desde}, e a última posição na Angellira é de "
            f"{posicao}{idade}.\nFavor verificar o rastreador do veículo com o motorista{motorista}.")
    if sem_sm:
        return "sm", _pendencia_sm(r, outro_procedimento, "Sem sinal",
                                   f"sem espelhamento na Shopee desde {desde}")
    return "", _pendencia("Sem sinal", "Sem leitura recente da Angellira para saber a causa", "")


def _pendencia_sm(r, outro_procedimento, tipo, situacao):
    if outro_procedimento:
        return _pendencia(
            tipo, f"SM no procedimento '{outro_procedimento}', não no da Shopee", GR,
            f"{_identificacao(r)} está {situacao}. A placa aparece numa SM do procedimento "
            f"'{outro_procedimento}', e não no {PROCEDIMENTO_SHOPEE}: assim o espelhamento não chega na Shopee."
            f"\nFavor verificar e corrigir a SM.")
    return _pendencia(
        tipo, "Sem SM na Angellira", GR,
        f"{_identificacao(r)} está {situacao} e não tem SM aberta na Angellira no procedimento "
        f"{PROCEDIMENTO_SHOPEE}. Sem ela, o espelhamento não chega na Shopee.\nFavor abrir a SM.")


def avaliar(r: dict, dados: dict, outro_procedimento: str, relatorio_fresco: bool, grupos: dict,
            now: datetime) -> list:
    """Colunas 'Causa NOK', 'Alerta Saída' e 'Acionar'; devolve as pendências para o site."""
    r["Causa NOK"] = r["Alerta Saída"] = r["Acionar"] = ""
    if (r.get("Cancelamento") or dados.get("chegada_realizada")
            or _chave(r.get("Status Viagem")) == "FINALIZADA"):
        return []
    sem_sm = (relatorio_fresco and r.get("Rastreamento") == "Sem correspondência"
              and not dados.get("sm_encerrada_em"))
    pendencias, causa = [], ""
    if r.get("Espelhamento Atual") == "NOK":
        causa, p = _causa_nok(r, dados, sem_sm, outro_procedimento, grupos)
        r["Causa NOK"] = p["texto"]
        pendencias.append(p)

    base = dados.get("base") or {}
    inicio, cpt = _dt(base.get("inicio")), _dt(dados.get("cpt_previsto"))
    chegou_origem = _dt(dados.get("chegada_origem"))
    saiu = bool(dados.get("cpt_realizado")) or _chave(r.get("Status Viagem")) not in ("", "PREVIAGEM")
    tol = timedelta(minutes=SAIDA_TOLERANCIA_MIN)
    saida = []
    # Sem o CPT planejado, a linha da DBLH não foi lida: sem ele não dá para dizer que
    # o veículo não chegou na origem (todas as rotas virariam alerta).
    if not saiu and cpt:
        if now > cpt + tol:
            if chegou_origem:
                situacao = f"chegou na origem às {_dia_hora(chegou_origem)}"
                atrasou = inicio and chegou_origem > inicio + tol
                acionar = TRANSPORTADORA if atrasou else ORIGEM
                if atrasou:
                    situacao += f", depois do previsto ({_dia_hora(inicio)})"
            else:
                situacao, acionar = "ainda não chegou na origem", TRANSPORTADORA
            saida.append(_pendencia(
                "Saída", f"CPT {_dia_hora(cpt)} vencido há {_duracao(now - cpt)}, sem saída ({situacao})", acionar,
                f"{_identificacao(r)} tinha CPT às {_dia_hora(cpt)} e ainda não saiu (atraso de "
                f"{_duracao(now - cpt)}). O veículo {situacao}.\nQual a previsão de saída?"))
        elif not chegou_origem and inicio and now > inicio + tol:
            saida.append(_pendencia(
                "Saída", f"Não chegou na origem (previsto {_dia_hora(inicio)}, atraso de {_duracao(now - inicio)})",
                TRANSPORTADORA,
                f"{_identificacao(r)} tinha chegada na origem prevista para {_dia_hora(inicio)} e o veículo ainda "
                f"não chegou (atraso de {_duracao(now - inicio)}).\nQual a previsão de chegada?"))
    if sem_sm and causa != "sm":
        referencia = cpt or inicio
        if saiu:
            saida.append(_pendencia_sm(r, outro_procedimento, "SM", "em viagem"))
        elif referencia and now >= referencia - timedelta(minutes=SM_ANTECEDENCIA_MIN):
            vencido = "vencido " if now > referencia else ""
            saida.append(_pendencia_sm(r, outro_procedimento, "SM", f"com CPT {vencido}às {_dia_hora(referencia)}"))
    r["Alerta Saída"] = "; ".join(p["texto"] for p in saida)
    pendencias += saida
    r["Acionar"] = " / ".join(dict.fromkeys(p["acionar"] for p in pendencias if p["acionar"]))
    return pendencias


def resumo_quedas_gerais(grupos: dict, por_trip: dict) -> list:
    """Faixa do site: uma por queda geral aberta, com o aviso para a Shopee e a Angellira."""
    saida = []
    for inicio, trips in sorted(grupos.items()):
        rotas = [por_trip[t] for t in trips if t in por_trip]
        com_posicao = sum(1 for r in rotas if r.get("Rastreamento") == "OK"
                          and (_num(r.get("Min. Sem Posição")) or 1e9) <= POSICAO_RECENTE_MIN)
        desde = _dia_hora(_dt(inicio))
        lista = ", ".join(f"{r['Trip Number']} ({r.get('Placa') or 'sem placa'})" for r in rotas)
        saida.append({
            "desde": desde, "rotas": [r["Trip Number"] for r in rotas], "com_posicao": com_posicao,
            "mensagem": (f"{SAUDACAO}\nDesde {desde}, {len(rotas)} viagens estão sem espelhamento na Shopee ao "
                         f"mesmo tempo. A Angellira tem posição recente de {com_posicao} delas, então a falha "
                         f"parece ser da integração, não dos veículos.\nViagens: {lista}.\nPodem verificar?")})
    return saida
