# -*- coding: utf-8 -*-
"""Paradas da viagem e tempo parado total, montados a cada ciclo com as leituras da Angellira.

Parada = leituras seguidas com o veículo parado (velocidade até VEL_PARADO; a Angellira deixa a
velocidade em branco quando é zero). O início vem do "Tempo Parado" da Angellira quando ele é
coerente com as leituras (senão, a primeira leitura parada); o fim, da primeira leitura em
movimento. Só conta da saída da origem até a chegada; parada perto da origem ou do destino é
carga e descarga e fica de fora. Precisão: a de uma leitura (alguns minutos).

O motivo sai, nesta ordem, da macro do motorista (refeição, abastecimento...), do aviso de
acidente (motorista ou TomTom), do texto da posição (posto, unidade Shopee), dos pontos
autorizados do rotograma e do trânsito da TomTom. Trânsito e acidente na via não contam no total."""

import os
import re
import unicodedata
from datetime import datetime, timedelta

import geo_tomtom
import rotograma

PARADA_MIN = int(os.getenv("PARADAS_MIN_MIN", "10"))
VEL_PARADO = 5
RAIO_ESTACAO_KM = rotograma.RAIO_ESTACAO_KM
RAIO_TRANSITO_KM = 3
# Parada curta com o motor ligado, sem macro e sem lugar conhecido: fila de trânsito.
TRANSITO_MAX_MIN = int(os.getenv("PARADAS_TRANSITO_MAX_MIN", "20"))
# Posição mais velha que isto não diz se o veículo está parado ou andando agora.
SITUACAO_POSICAO_MAX_MIN = int(os.getenv("SITUACAO_POSICAO_MAX_MIN", "60"))
MAX_PARADAS = 80

# (chave, rótulo, conta no total)
TIPOS = [("indevida", "Fora de ponto autorizado", True), ("descanso", "Descanso / refeição", True),
         ("abastecimento", "Posto / abastecimento", True), ("autorizado", "Ponto autorizado", True),
         ("fiscalizacao", "Fiscalização", True), ("mecanica", "Mecânica / pneu", True),
         ("transito", "Trânsito", False), ("acidente", "Acidente / interdição na via", False),
         ("shopee", "Unidade Shopee", False)]
ROTULO = {k: r for k, r, _c in TIPOS}
CONTA = {k: c for k, _r, c in TIPOS}
MACROS = [("acidente", r"ACIDENT"), ("abastecimento", r"ABASTEC"),
          ("descanso", r"REFEIC|ALMOC|JANTA|CAFE|LANCHE|PERNOIT|DESCANS|REPOUS|DORMI|BANHO"),
          ("fiscalizacao", r"FISCALIZ|POLICIA|\bPRF\b|BALANCA|BLITZ|SEFAZ"),
          ("mecanica", r"MANUTEN|MECANIC|PNEU|QUEBR|\bPANE\b|GUINCHO")]


def _norm(texto) -> str:
    t = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().upper()
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", t).split())


def _dt(iso):
    try:
        return datetime.fromisoformat(iso) if iso else None
    except (TypeError, ValueError):
        return None


def _hhmm(minutos) -> str:
    h, m = divmod(int(round(minutos)), 60)
    return f"{h}h{m:02d}"


def _duracao(minutos) -> str:
    return f"{int(round(minutos))} min" if minutos < 60 else _hhmm(minutos)


def _num(v):
    try:
        return float(str(v).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def _em_ponto_autorizado(info: dict, r: dict, rot: dict) -> bool:
    v = info.get("veiculo")
    texto = _norm(r.get("Posição Atual"))
    for lat, lon, nome, _cidade, _rod, prec in (rot or {}).get("pontos", []):
        if prec == "referência" and info.get("precisao") == "referência" and v \
                and geo_tomtom.distancia_km(v, (lat, lon)) <= rotograma.RAIO_PONTO_KM:
            return True
        if len(_norm(nome)) >= 6 and _norm(nome) in texto:
            return True
    return False


def _classificar(p: dict) -> str:
    macros = " ".join(p.get("macros", []))
    for tipo, padrao in MACROS:
        if re.search(padrao, macros):
            return tipo
    if p.get("acidente"):
        return "acidente"
    local = _norm(p.get("local"))
    if "SHOPEE" in local:
        return "shopee"
    if re.search(r"\bPOSTO\b|\bAUTO POSTO\b|\bGRAAL\b", local):
        return "abastecimento"
    if p.get("autorizado"):
        return "autorizado"
    duracao = p.get("min") or 0
    if p.get("transito") or (p.get("ign_desligada", 0) == 0 and duracao <= TRANSITO_MAX_MIN):
        return "transito"
    return "indevida"


def atualizar(dados: dict, r: dict, info: dict, rot: dict, em_viagem: bool) -> None:
    """Lê a leitura nova da Angellira desta rota e abre, estende ou fecha a parada."""
    quando = geo_tomtom._parse_br(r.get("Data da Posição"))
    if not quando:
        return
    perto_estacao = any(e and info.get("veiculo") and geo_tomtom.distancia_km(info["veiculo"], e) <= RAIO_ESTACAO_KM
                        for e in (info.get("origem"), info.get("destino")))
    chegou = (dados.get("chegada_realizada") or dados.get("sm_encerrada_em")
              or r.get("Risco de Atraso") in ("No destino", "Encerrada"))
    if not em_viagem or chegou:
        dados.pop("parada_aberta", None)  # parada que segue até a chegada é a descarga
        return
    ultima = _dt(dados.get("paradas_leitura"))
    if ultima and quando <= ultima:
        return  # mesma leitura de antes: nada de novo
    dados["paradas_leitura"] = quando.isoformat()
    vel = _num(r.get("Velocidade (km/h)"))
    parado = (vel is None or vel <= VEL_PARADO) and not perto_estacao
    aberta = dados.get("parada_aberta")
    movimento = _dt(dados.get("paradas_movimento"))
    if parado:
        if not aberta:
            tempo = rotograma._minutos(r.get("Tempo Parado"))
            inicio = quando - timedelta(minutes=tempo) if tempo is not None else quando
            if movimento and inicio < movimento:
                inicio = movimento  # não estava parado quando a leitura anterior o viu andando
            aberta = dados["parada_aberta"] = {
                "inicio": inicio.isoformat(), "local": r.get("Posição Atual") or "", "cidade": r.get("Cidade/UF") or "",
                "lat": (info.get("veiculo") or [None, None])[0], "lon": (info.get("veiculo") or [None, None])[1],
                "macros": [], "ign_desligada": 0, "transito": False, "acidente": False, "autorizado": False}
        aberta["ultima"] = quando.isoformat()
        msg = _norm(r.get("Última Mensagem"))
        # Macro que já estava lá quando o veículo andava é de antes: não explica esta parada.
        if msg and msg != dados.get("paradas_msg_movimento") and msg not in aberta["macros"]:
            aberta["macros"].append(msg)
        if r.get("Ignição") == "Desligada":
            aberta["ign_desligada"] += 1
        trechos = info.get("transito") or []
        aberta["transito"] |= any(t["tipo"] == "congestionamento" and t["km"] <= RAIO_TRANSITO_KM for t in trechos)
        aberta["acidente"] |= (any(t["tipo"] in ("acidente", "interdição") and t["km"] <= RAIO_TRANSITO_KM for t in trechos)
                               or str(r.get("Alerta Acidente") or "").startswith("Motorista:"))
        aberta["autorizado"] |= _em_ponto_autorizado(info, r, rot)
        return
    dados["paradas_movimento"] = quando.isoformat()
    dados["paradas_msg_movimento"] = _norm(r.get("Última Mensagem"))
    if aberta:
        inicio, fim = _dt(aberta["inicio"]), quando
        # Andando, o "Tempo Parado" da Angellira é o da última parada: afina o fim.
        tempo = rotograma._minutos(r.get("Tempo Parado"))
        if tempo is not None and _dt(aberta["ultima"]) <= inicio + timedelta(minutes=tempo) <= quando:
            fim = inicio + timedelta(minutes=tempo)
        minutos = (fim - inicio).total_seconds() / 60
        if minutos >= PARADA_MIN:
            p = {k: aberta[k] for k in ("local", "cidade", "lat", "lon", "macros", "ign_desligada", "transito",
                                       "acidente", "autorizado")}
            p.update(inicio=inicio.isoformat(), fim=fim.isoformat(), min=round(minutos))
            p["tipo"] = _classificar(p)
            dados.setdefault("paradas", []).append(p)
            del dados["paradas"][:-MAX_PARADAS]
        dados.pop("parada_aberta", None)


def resumo(dados: dict, r: dict, now: datetime) -> dict:
    """Colunas 'Tempo Parado Total' e 'Paradas Indevidas' e o dado do site."""
    paradas = list(dados.get("paradas") or [])
    aberta = dados.get("parada_aberta")
    atual = None
    if aberta:
        minutos = (max(now, _dt(aberta["ultima"])) - _dt(aberta["inicio"])).total_seconds() / 60
        atual = {**aberta, "min": round(minutos), "fim": ""}
        atual["tipo"] = _classificar(atual)
    todas = paradas + ([atual] if atual and atual["min"] >= PARADA_MIN else [])
    por_tipo = {}
    for p in todas:
        t = por_tipo.setdefault(p["tipo"], {"tipo": p["tipo"], "rotulo": ROTULO[p["tipo"]], "conta": CONTA[p["tipo"]],
                                            "min": 0, "qtd": 0})
        t["min"] += p["min"]
        t["qtd"] += 1
    total = sum(t["min"] for t in por_tipo.values() if t["conta"])
    indevida = por_tipo.get("indevida", {}).get("min", 0)
    r["Tempo Parado Total"] = _hhmm(total) if todas else ""
    r["Paradas Indevidas"] = _hhmm(indevida) if todas else ""
    fmt = lambda iso: _dt(iso).strftime("%d/%m %H:%M") if _dt(iso) else ""
    return {
        "total_min": total, "indevida_min": indevida,
        "nao_conta_min": sum(t["min"] for t in por_tipo.values() if not t["conta"]),
        "por_tipo": sorted(por_tipo.values(), key=lambda t: (not t["conta"], -t["min"])),
        "aberta": ({"inicio": fmt(atual["inicio"]), "min": atual["min"], "tipo": ROTULO[atual["tipo"]],
                    "local": atual["local"], "cidade": atual["cidade"]} if atual else None),
        "lista": [{"inicio": fmt(p["inicio"]), "fim": fmt(p["fim"]), "min": p["min"], "tipo": ROTULO[p["tipo"]],
                   "conta": CONTA[p["tipo"]], "local": p["local"], "cidade": p["cidade"],
                   "macro": (p.get("macros") or [""])[-1].title(), "lat": p.get("lat"), "lon": p.get("lon")}
                  for p in reversed(paradas)][:40],
    }


def situacao(dados: dict, r: dict, info: dict, em_viagem: bool, now: datetime):
    """Veículo agora: em movimento, parado (há quanto tempo, motor ligado ou desligado e onde)
    ou sem posição recente. Preenche 'Situação Veículo'; só vale da saída até a chegada."""
    r["Situação Veículo"] = ""
    chegou = (dados.get("chegada_realizada") or dados.get("sm_encerrada_em")
              or r.get("Risco de Atraso") in ("No destino", "Encerrada"))
    quando = geo_tomtom._parse_br(r.get("Data da Posição"))
    # Relatório da Angellira velho: a posição antiga é do robô, não do veículo.
    if not em_viagem or chegou or r.get("Cancelamento") or not quando or r.get("Rastreamento") != "OK":
        return None
    idade = max(0.0, (now - quando).total_seconds() / 60)
    if idade > SITUACAO_POSICAO_MAX_MIN:
        s = {"estado": "sem_posicao", "min": round(idade), "texto": f"Sem posição há {_duracao(idade)}"}
    else:
        vel = _num(r.get("Velocidade (km/h)"))
        ignicao = r.get("Ignição") or ""
        if vel is not None and vel > VEL_PARADO:
            s = {"estado": "movimento", "min": 0, "ignicao": ignicao, "texto": f"Em movimento · {int(vel)} km/h"}
        else:
            aberta = dados.get("parada_aberta")
            tempo = rotograma._minutos(r.get("Tempo Parado"))
            if aberta:
                minutos = (max(now, _dt(aberta["ultima"])) - _dt(aberta["inicio"])).total_seconds() / 60
            else:
                # Tempo Parado é o da hora da posição: soma o tempo desde ela (sem posição nova, segue parado).
                minutos = (tempo or 0) + idade
            v = info.get("veiculo")
            if v and info.get("origem") and geo_tomtom.distancia_km(v, info["origem"]) <= RAIO_ESTACAO_KM:
                onde = "na origem"
            elif v and info.get("destino") and geo_tomtom.distancia_km(v, info["destino"]) <= RAIO_ESTACAO_KM:
                onde = "no destino"
            else:
                onde = ROTULO[_classificar({**aberta, "min": round(minutos)})] if aberta else ""
            motor = {"Ligada": "motor ligado", "Desligada": "motor desligado"}.get(ignicao, "")
            s = {"estado": "parado", "min": round(minutos), "ignicao": ignicao, "onde": onde,
                 "texto": " · ".join(t for t in (f"Parado há {_duracao(minutos)}", motor, onde) if t)}
    r["Situação Veículo"] = s["texto"]
    return s
