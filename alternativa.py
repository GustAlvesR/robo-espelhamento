# -*- coding: utf-8 -*-
"""Rota alternativa para o GR validar quando há ocorrência à frente do caminhão.

Caminho de referência = o que o motorista seguiria: o rotograma do trecho ou, sem
rotograma, a rota planejada. Só dois casos gastam chamadas da TomTom:
- a TomTom desviou: o caminho mais rápido com o trânsito de agora (o do ETA) se afastou
  da rota planejada. Calcula o caminho de referência com trânsito; se ele tem ocorrência
  e o mais rápido chega ALTERNATIVA_ECONOMIA_MIN antes por outro caminho, sugere o mais rápido.
- o motorista avisou acidente: a TomTom não sabe do acidente nem do atraso, então calcula
  o contorno dos próximos km do caminho de referência e mostra quanto ele custa.
Nada vai para o motorista: a torre leva o pedido ao GR (grupo Alinhamento GR), que valida.
O link abre o Google Maps com pontos de passagem tirados da rota de caminhão da TomTom
(o Google Maps comum calcula como carro; os pontos o seguram no trajeto)."""

import logging
import math
import os
import urllib.parse
from datetime import timedelta

import geo_tomtom

ECONOMIA_MIN = int(os.getenv("ALTERNATIVA_ECONOMIA_MIN", "30"))
DESVIO_KM = float(os.getenv("ALTERNATIVA_DESVIO_KM", "5"))
EVITAR_KM = float(os.getenv("ALTERNATIVA_EVITAR_KM", "6"))
RECALCULO_MIN = int(os.getenv("ALTERNATIVA_RECALCULO_MIN", "30"))
MAX_CHAMADAS_CICLO = int(os.getenv("ALTERNATIVA_MAX_CHAMADAS_CICLO", "6"))
MAX_PONTOS_GOOGLE = 8  # o link do Google Maps aceita até 9 pontos de passagem

_cache = {}  # trip -> {"chave", "em", "sugestao"}


def _dist(a, b) -> float:
    return geo_tomtom.distancia_km(a, b)


def a_frente(caminho: list, ponto) -> list:
    """Pedaço do caminho a partir do ponto mais próximo do caminhão."""
    if not caminho:
        return []
    i = min(range(len(caminho)), key=lambda k: _dist(caminho[k], ponto))
    return caminho[i:]


def distancia_ao_caminho(ponto, caminho: list) -> float:
    return min(_dist(ponto, c) for c in caminho) if caminho else math.inf


def afastamento_km(caminho: list, referencia: list) -> float:
    """Quanto o caminho se afasta da referência no ponto mais distante."""
    if not caminho or not referencia:
        return math.inf
    ref = referencia[::2] + [referencia[-1]]
    return max(distancia_ao_caminho(p, ref) for p in caminho[::3] + [caminho[-1]])


def _amostrar(pontos: list, cada_km: float, maximo: int) -> list:
    saida, acumulado = [], 0.0
    for a, b in zip(pontos, pontos[1:]):
        acumulado += _dist(a, b)
        if acumulado >= cada_km:
            saida.append(b)
            acumulado = 0.0
    if len(saida) > maximo:
        saida = saida[::math.ceil(len(saida) / maximo)]
    return saida


def _trecho(pontos: list, ini_km: float, fim_km: float) -> list:
    saida, acumulado = [], 0.0
    for a, b in zip(pontos, pontos[1:]):
        acumulado += _dist(a, b)
        if ini_km <= acumulado <= fim_km or (not saida and acumulado > fim_km):
            saida.append(b)
        if acumulado > fim_km:
            break
    return saida


def _retangulo(pontos: list, margem_km: float):
    lat_m = margem_km / 111.0
    lon_m = margem_km / (111.0 * max(0.2, math.cos(math.radians(pontos[0][0]))))
    lats, lons = [p[0] for p in pontos], [p[1] for p in pontos]
    return ((min(lats) - lat_m, min(lons) - lon_m), (max(lats) + lat_m, max(lons) + lon_m))


def link_google(origem, destino, alternativa: list, referencia: list) -> str:
    """Google Maps com o trajeto: os pontos de passagem saem de onde a alternativa se
    afasta da referência (é ali que o Google escolheria outro caminho)."""
    divergentes = [p for p in alternativa if distancia_ao_caminho(p, referencia) > DESVIO_KM] if referencia else []
    base = divergentes or alternativa[1:-1]
    pontos = base[::max(1, math.ceil(len(base) / MAX_PONTOS_GOOGLE))][:MAX_PONTOS_GOOGLE]
    fmt = lambda p: f"{p[0]:.5f},{p[1]:.5f}"
    params = {"api": "1", "origin": fmt(origem), "destination": fmt(destino), "travelmode": "driving"}
    if pontos:
        params["waypoints"] = "|".join(fmt(p) for p in pontos)
    return "https://www.google.com/maps/dir/?" + urllib.parse.urlencode(params, safe=",|")


def _texto_ocorrencias(ocorrencias: list) -> str:
    return "; ".join(f"TomTom: {t['tipo']} a {t['km']} km" + (f" (+{t['atraso_min']} min)" if t["atraso_min"] else "")
                     for t in sorted(ocorrencias, key=lambda t: t["km"])[:3])


def _calcular(geo, veiculo, destino, eta, ref_frente, motorista: bool, desviou: bool, now):
    rapida_s, rapida_pts = eta["travel_s"], eta["pontos"]
    rapida_km = round((eta.get("dist_m") or 0) / 1000)
    if desviou:
        ref = geo.rota_com_transito([veiculo] + _amostrar(ref_frente, 50, 20) + [destino], now)
        if ref and ref.get("dur_s") and ref.get("transito"):
            economia = (ref["dur_s"] - rapida_s) / 60
            if economia >= ECONOMIA_MIN and afastamento_km(rapida_pts, ref_frente) > DESVIO_KM:
                return {"tipo": "tomtom", "motivo": _texto_ocorrencias(ref["transito"]),
                        "seg_ref": ref["dur_s"], "seg_alt": rapida_s, "economia_min": round(economia),
                        "km_ref": round((ref.get("dist_m") or 0) / 1000), "km_alt": rapida_km,
                        "pontos": rapida_pts}
    if motorista:
        trecho = _trecho(ref_frente, 0.5, EVITAR_KM)
        if trecho:
            alt = geo.rota_evitando(veiculo, destino, [_retangulo([veiculo] + trecho, 1.0)], now)
            if alt and alt.get("dur_s") and afastamento_km(alt["pontos"], ref_frente) > 1.0:
                return {"tipo": "motorista", "motivo": f"acidente informado pelo motorista; contorno dos próximos "
                                                       f"{EVITAR_KM:g} km do caminho",
                        "seg_ref": rapida_s, "seg_alt": alt["dur_s"],
                        "economia_min": round((rapida_s - alt["dur_s"]) / 60),
                        "km_ref": rapida_km, "km_alt": round((alt.get("dist_m") or 0) / 1000),
                        "pontos": alt["pontos"]}
    return None


def mensagem(r: dict, s: dict) -> str:
    placas = " / ".join(p for p in (r.get("Placa"), r.get("Placa 2")) if p) or "não informada"
    origem, destino = r.get("Origin Station Code", ""), r.get("Destination Station Code", "")
    local = lambda c: f" ({geo_tomtom.local_estacao(c)})" if geo_tomtom.local_estacao(c) else ""
    resultado = (f"economia de ~{s['economia_min']} min" if s["economia_min"] > 0
                 else f"custo de ~{-s['economia_min']} min a mais")
    return "\n".join(filter(None, [
        "*Torre LH: alteração de rota para o GR validar*",
        f"Viagem: {r['Trip Number']} | Placa: {placas} | Motorista: {r.get('Motorista') or 'não informado'}",
        f"Trecho: {origem}{local(origem)} → {destino}{local(destino)}" + (f" | SM {s['sm']}" if s.get("sm") else ""),
        f"Posição: {r.get('Cidade/UF') or '?'}" + (f" em {r['Data da Posição']}" if r.get("Data da Posição") else ""),
        f"Ocorrência: {s['motivo']}",
        f"Seguindo {s['referencia']}: chegada prevista {s['chegada_ref']} ({s['km_ref']} km)",
        f"Pela alternativa: chegada prevista {s['chegada_alt']} ({s['km_alt']} km, {resultado})",
        f"Trajeto alternativo: {s['link']}",
        "Favor validar a alteração de rota.",
    ]))


def sugerir(geo, resultados: list, geo_por_trip: dict, rotogramas_mapa: dict, iniciada, now) -> None:
    """Preenche 'Rota Alternativa' (a última coluna) e info['alternativa'] para o site."""
    orcamento, ativas, sugeridas = MAX_CHAMADAS_CICLO, set(), 0
    for r in resultados:
        r["Rota Alternativa"] = ""
        trip = r["Trip Number"]
        info = geo_por_trip.get(trip) or {}
        eta = geo.etas.get(trip)
        veiculo, destino = info.get("veiculo"), info.get("destino")
        if (r.get("Cancelamento") or not iniciada(r) or not (veiculo and destino and info.get("rota_atual"))
                or not (eta and eta.get("pontos") and eta.get("travel_s"))):
            continue
        rot = rotogramas_mapa.get((info.get("rotograma") or {}).get("chave", "")) or {}
        ref_frente = a_frente(rot.get("linha") or info.get("pontos") or [], veiculo)
        if len(ref_frente) < 2:
            continue
        motorista = str(r.get("Alerta Acidente") or "").startswith("Motorista:")
        desviou = afastamento_km(eta["pontos"], a_frente(info.get("pontos") or [], veiculo) or ref_frente) > DESVIO_KM
        if not (desviou or motorista):
            continue
        ativas.add(trip)
        chave = (desviou, motorista, round(veiculo[0], 2), round(veiculo[1], 2))
        c = _cache.get(trip)
        if c and (c["chave"] == chave and now - c["em"] < timedelta(minutes=RECALCULO_MIN) or orcamento <= 0):
            s = c["sugestao"]
        elif orcamento > 0:
            antes = geo.chamadas_hoje
            s = _calcular(geo, veiculo, destino, eta, ref_frente, motorista, desviou, now)
            orcamento -= max(1, geo.chamadas_hoje - antes)
            if s:
                s.update(link=link_google(veiculo, destino, s["pontos"], ref_frente),
                         referencia=(f"o rotograma (SM {rot['sm']})" if rot.get("sm") else "o rotograma")
                         if rot.get("linha") else "a rota planejada", sm=rot.get("sm", ""),
                         chegada_ref=(now + timedelta(seconds=s["seg_ref"])).strftime("%d/%m %H:%M"),
                         chegada_alt=(now + timedelta(seconds=s["seg_alt"])).strftime("%d/%m %H:%M"),
                         calculado_em=now.strftime("%H:%M"))
                logging.info("[%s] Rota alternativa para o GR: %s (%s min).", trip, s["motivo"], s["economia_min"])
            _cache[trip] = {"chave": chave, "em": now, "sugestao": s}
        else:
            continue
        if s:
            sugeridas += 1
            info["alternativa"] = {**s, "mensagem": mensagem(r, s)}
            r["Rota Alternativa"] = (f"Economia ~{s['economia_min']} min" if s["economia_min"] > 0
                                     else f"Contorno +{-s['economia_min']} min") + f" ({s['motivo']}) · {s['link']}"
    for trip in [t for t in _cache if t not in ativas]:
        del _cache[trip]
    if ativas:
        logging.info("Rota alternativa: %s rota(s) com ocorrência ou desvio da TomTom, %s sugestão(ões) para o GR.",
                     len(ativas), sugeridas)
