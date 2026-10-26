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

import geo_tomtom

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


def _estacao(codigo) -> str:
    local = geo_tomtom.local_estacao(codigo) if codigo else ""
    return f"{codigo} ({local})" if local else (codigo or "?")


def _idade(minutos) -> str:
    if minutos is None:
        return ""
    return f"{int(minutos)} min" if minutos < 60 else _duracao(timedelta(minutes=minutos))


def _mensagem(titulo: str, subtitulo: str, r: dict, linhas: list, pedido: str) -> str:
    """Cobrança no padrão das mensagens da torre no WhatsApp (*negrito*, _itálico_), como a
    da rota alternativa."""
    fonte = r.get("Fonte Motorista")
    motorista = r.get("Motorista") and r["Motorista"] + (f" _({fonte})_" if fonte and fonte != "Rastreador" else "")
    return "\n".join(filter(lambda l: l is not None, [
        titulo, f"_{subtitulo}_", "",
        f"🚛 *Viagem:* {r['Trip Number']}",
        f"🔖 *Placas:* {_placas(r)}",
        f"👤 *Motorista:* {motorista}" if motorista else None,
        f"🏢 *TSP:* {r['TSP']}" if r.get("TSP") else None,
        f"🛣️ *Trecho:* {_estacao(r.get('Origin Station Code'))} ➜ {_estacao(r.get('Destination Station Code'))}",
        *linhas, "",
        f"👉 *{pedido}*",
    ]))


def _pendencia(tipo, texto, acionar, mensagem=""):
    return {"tipo": tipo, "texto": texto, "acionar": acionar, "mensagem": mensagem}


def _causa_nok(r, dados, sem_sm, outro_procedimento, grupos):
    desde = _dia_hora(_dt(dados.get("current_loss_start")))
    geral = dados.get("queda_geral")
    if geral in grupos:
        return "geral", _pendencia("Sem sinal", f"Queda geral do espelhamento desde {_dia_hora(_dt(geral))} "
                                                f"({len(grupos[geral])} rotas)", GERAL)
    min_pos = _num(r.get("Min. Sem Posição"))
    posicao = f"{r.get('Cidade/UF') or 'posição'} às {_sem_ano(r.get('Data da Posição')) or '?'}"
    idade = f" (há {_idade(min_pos)})" if min_pos is not None else ""
    sem_sinal = f"📵 *Sem sinal na Shopee desde:* {desde}"
    if r.get("Rastreamento") == "OK" and min_pos is not None and min_pos <= POSICAO_RECENTE_MIN:
        return "integracao", _pendencia(
            "Sem sinal", f"Integração Angellira → Shopee: a Angellira tem posição recente ({posicao})", ANGELLIRA,
            _mensagem("📡 *TORRE LH · FALHA DE ESPELHAMENTO*", "Para a Angellira verificar a integração com a Shopee",
                      r, [sem_sinal, f"📍 *Posição na Angellira:* {posicao}{idade}"],
                      "Podem verificar a integração Angellira → Shopee dessa SM?"))
    if r.get("Rastreamento") == "OK":
        return "rastreador", _pendencia(
            "Sem sinal", f"Rastreador sem posição: última na Angellira {posicao}{idade}", TRANSPORTADORA,
            _mensagem("📡 *TORRE LH · RASTREADOR SEM SINAL*", "Para a transportadora verificar o rastreador",
                      r, [sem_sinal, f"📍 *Última posição na Angellira:* {posicao}{idade}"],
                      "Favor verificar o rastreador do veículo com o motorista."))
    if sem_sm:
        return "sm", _pendencia_sm(r, outro_procedimento, "Sem sinal", [sem_sinal])
    return "", _pendencia("Sem sinal", "Sem leitura recente da Angellira para saber a causa", "")


def _pendencia_sm(r, outro_procedimento, tipo, situacao: list):
    if outro_procedimento:
        return _pendencia(
            tipo, f"SM no procedimento '{outro_procedimento}', não no da Shopee", GR,
            _mensagem("📄 *TORRE LH · SM NO PROCEDIMENTO ERRADO*", "Para o GR corrigir a SM", r,
                      [*situacao, f"⚠️ *SM encontrada em:* {outro_procedimento}",
                       f"✅ *Procedimento correto:* {PROCEDIMENTO_SHOPEE}", "",
                       "_Fora desse procedimento, o espelhamento não chega na Shopee._"],
                      "Favor verificar e corrigir a SM."))
    return _pendencia(
        tipo, "Sem SM na Angellira", GR,
        _mensagem("📄 *TORRE LH · VIAGEM SEM SM*", "Para o GR abrir a SM", r,
                  [*situacao, f"⚠️ *SM:* não encontrada no procedimento {PROCEDIMENTO_SHOPEE}", "",
                   "_Sem a SM, o espelhamento não chega na Shopee._"],
                  "Favor abrir a SM."))


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
            previsto = f"previsto {_dia_hora(inicio)}" if inicio else "sem horário previsto"
            if chegou_origem:
                situacao = f"chegou na origem às {_dia_hora(chegou_origem)}"
                atrasou = inicio and chegou_origem > inicio + tol
                acionar = TRANSPORTADORA if atrasou else ORIGEM
                if atrasou:
                    situacao += f", depois do previsto ({_dia_hora(inicio)})"
                chegada = f"{_dia_hora(chegou_origem)} ({previsto if atrasou else 'no horário'})"
            else:
                situacao, acionar = "ainda não chegou na origem", TRANSPORTADORA
                chegada = f"ainda não chegou ({previsto})"
            quem = "a origem" if acionar == ORIGEM else "a transportadora"
            saida.append(_pendencia(
                "Saída", f"CPT {_dia_hora(cpt)} vencido há {_duracao(now - cpt)}, sem saída ({situacao})", acionar,
                _mensagem("⏰ *TORRE LH · SAÍDA ATRASADA*", f"Para {quem} informar a previsão de saída", r,
                          [f"⏰ *CPT previsto:* {_dia_hora(cpt)}", f"⌛ *Atraso:* {_duracao(now - cpt)}",
                           f"🏁 *Chegada na origem:* {chegada}"],
                          "Qual a previsão de saída?")))
        elif not chegou_origem and inicio and now > inicio + tol:
            saida.append(_pendencia(
                "Saída", f"Não chegou na origem (previsto {_dia_hora(inicio)}, atraso de {_duracao(now - inicio)})",
                TRANSPORTADORA,
                _mensagem("⏰ *TORRE LH · ATRASO NA CHEGADA À ORIGEM*",
                          "Para a transportadora informar a previsão de chegada", r,
                          [f"🏁 *Chegada prevista na origem:* {_dia_hora(inicio)}",
                           f"⌛ *Atraso:* {_duracao(now - inicio)}", f"⏰ *CPT previsto:* {_dia_hora(cpt)}"],
                          "Qual a previsão de chegada?")))
    if sem_sm and causa != "sm":
        referencia = cpt or inicio
        if saiu:
            saida.append(_pendencia_sm(r, outro_procedimento, "SM", ["🚚 *Situação:* em viagem"]))
        elif referencia and now >= referencia - timedelta(minutes=SM_ANTECEDENCIA_MIN):
            vencido = " (vencido)" if now > referencia else ""
            saida.append(_pendencia_sm(r, outro_procedimento, "SM", [f"⏰ *CPT:* {_dia_hora(referencia)}{vencido}"]))
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
        conclusao = ("A Angellira segue recebendo a posição dos caminhões: a falha parece ser da integração."
                     if com_posicao * 2 >= len(rotas)
                     else "Várias viagens caíram juntas: a falha parece ser comum, não de cada veículo.")
        saida.append({
            "desde": desde, "rotas": [r["Trip Number"] for r in rotas], "com_posicao": com_posicao,
            "mensagem": "\n".join([
                "🚨 *TORRE LH · QUEDA GERAL DO ESPELHAMENTO*", "_Para a Shopee e a Angellira verificarem_", "",
                f"📵 *Sem sinal desde:* {desde}",
                f"🚛 *Viagens afetadas:* {len(rotas)}",
                f"📍 *Com posição recente na Angellira:* {com_posicao} de {len(rotas)}", "",
                "⚠️ *Viagens*",
                *(f"• {r['Trip Number']} ({_placas(r)})" for r in rotas), "",
                f"_{conclusao}_", "",
                "👉 *Podem verificar?*"])})
    return saida
