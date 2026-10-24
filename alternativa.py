# -*- coding: utf-8 -*-
"""Rota alternativa para o GR validar quando há ocorrência à frente do caminhão.

Caminho de referência = o que o motorista seguiria: o rotograma do trecho ou, sem
rotograma, a rota planejada. Só dois casos gastam chamadas da TomTom:
- a TomTom desviou: o caminho mais rápido com o trânsito de agora (o do ETA) se afastou
  da rota planejada. Calcula o caminho de referência com trânsito; se ele tem ocorrência
  e o mais rápido chega ALTERNATIVA_ECONOMIA_MIN antes por outro caminho, sugere o mais rápido.
- o motorista avisou acidente: a TomTom não sabe do acidente nem do atraso, então calcula
  o contorno dos próximos km do caminho de referência e o compara com o ETA de quem segue
  esse caminho (o ETA sem a retenção do acidente, que ninguém sabe quanto dura).
Nada vai para o motorista: a torre leva o pedido ao GR (grupo Alinhamento GR), que valida.
O link abre o Google Maps com pontos de passagem tirados da rota de caminhão da TomTom
(o Google Maps comum calcula como carro; os pontos o seguram no trajeto)."""

import logging
import math
import os
import re
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


def _sugestao(tipo: str, motivo: str, ref: dict, alt: dict) -> dict:
    return {"tipo": tipo, "motivo": motivo, "seg_ref": ref["dur_s"], "seg_alt": alt["dur_s"],
            "economia_min": round((ref["dur_s"] - alt["dur_s"]) / 60),
            "km_ref": round((ref.get("dist_m") or 0) / 1000), "km_alt": round((alt.get("dist_m") or 0) / 1000),
            "pontos": alt["pontos"]}


def _calcular(geo, veiculo, destino, eta, ref_frente, motorista: bool, desviou: bool, now):
    rapida = {"dur_s": eta["travel_s"], "dist_m": eta.get("dist_m"), "pontos": eta["pontos"]}
    rapida_fora = afastamento_km(rapida["pontos"], ref_frente) > DESVIO_KM
    # O ETA de quem segue o trajeto atual sai do caminho de referência; o mais rápido só
    # serve de referência quando ele próprio segue esse caminho.
    ref = rapida if not rapida_fora else None
    if rapida_fora and (desviou or motorista):
        ref = geo.rota_com_transito([veiculo] + _amostrar(ref_frente, 50, 20) + [destino], now)
    if not (ref and ref.get("dur_s")):
        return None
    if desviou and rapida_fora and ref.get("transito") and (ref["dur_s"] - rapida["dur_s"]) / 60 >= ECONOMIA_MIN:
        return _sugestao("tomtom", _texto_ocorrencias(ref["transito"]), ref, rapida)
    if motorista:
        trecho = _trecho(ref_frente, 0.5, EVITAR_KM)
        if trecho:
            alt = geo.rota_evitando(veiculo, destino, [_retangulo([veiculo] + trecho, 1.0)], now)
            if alt and alt.get("dur_s") and afastamento_km(alt["pontos"], ref_frente) > 1.0:
                return _sugestao("motorista", f"acidente informado pelo motorista (contorno dos próximos "
                                              f"{EVITAR_KM:g} km do caminho)", ref, alt)
    return None


def _dia_hora(texto) -> str:
    return re.sub(r"^(\d{2}/\d{2})/\d{4}", r"\1", str(texto or ""))


def mensagem(r: dict, s: dict, noticias=()) -> str:
    """Texto para o grupo Alinhamento GR no WhatsApp (*negrito* e _itálico_ do WhatsApp)."""
    placas = " / ".join(p for p in (r.get("Placa"), r.get("Placa 2")) if p) or "não informada"
    origem, destino = r.get("Origin Station Code", ""), r.get("Destination Station Code", "")
    local = lambda c: f" ({geo_tomtom.local_estacao(c)})" if geo_tomtom.local_estacao(c) else ""
    fonte = r.get("Fonte Motorista")
    motorista = (r.get("Motorista") or "não identificado") + (f" _({fonte})_" if fonte and fonte != "Rastreador" else "")
    posicao = (r.get("Cidade/UF") or "?") + (f" às {_dia_hora(r['Data da Posição'])}" if r.get("Data da Posição") else "")
    ocorrencias = [o[:1].upper() + o[1:] for o in s["motivo"].split("; ")]
    diferenca = s["economia_min"]
    if diferenca > 0:
        resultado = f"✅ *Ganho estimado:* ~{diferenca} min"
    elif diferenca == 0:
        resultado = "➖ *Diferença:* ~0 min (o contorno não atrasa a viagem)"
    else:
        resultado = f"⏳ *Custo do contorno:* +{-diferenca} min"
        if s["tipo"] == "motorista":
            resultado += f"\n_Compensa se a retenção do acidente passar de {-diferenca} min._"
    return "\n".join(filter(lambda l: l is not None, [
        "🚨 *TORRE LH · ALTERAÇÃO DE ROTA*",
        "_Pedido para o GR validar_",
        "",
        f"🚛 *Viagem:* {r['Trip Number']}",
        f"🔖 *Placas:* {placas}",
        f"👤 *Motorista:* {motorista}",
        f"🛣️ *Trecho:* {origem}{local(origem)} ➜ {destino}{local(destino)}",
        f"📄 *SM:* {s['sm']}" if s.get("sm") else None,
        f"📍 *Posição:* {posicao}",
        f"🎯 *Chegada planejada:* {_dia_hora(r['Fim Previsto'])}" if r.get("Fim Previsto") else None,
        "",
        "⚠️ *Ocorrência*",
        *(f"• {o}" for o in ocorrencias),
        *(f"📰 _{n['tipo'].capitalize()} em {n['cidade']}, a {n['km']} km ({n['fonte'] or 'notícia'}, {n['quando']}):_ "
          f"{n['titulo']}" for n in noticias),
        "",
        f"🕒 *ETA se continuar no trajeto atual ({s['referencia']})*",
        f"{s['chegada_ref']} · {s['km_ref']} km",
        "_Sem contar a retenção do acidente, que a TomTom não conhece._" if s["tipo"] == "motorista" else None,
        "",
        "🔀 *ETA pela rota alternativa*",
        f"{s['chegada_alt']} · {s['km_alt']} km",
        "",
        resultado,
        "",
        "🗺️ *Trajeto alternativo (Google Maps)*",
        s["link"],
        "",
        "👉 *Favor validar a alteração de rota.*",
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
                         referencia="rotograma" if rot.get("linha") else "rota planejada", sm=rot.get("sm", ""),
                         chegada_ref=(now + timedelta(seconds=s["seg_ref"])).strftime("%d/%m %H:%M"),
                         chegada_alt=(now + timedelta(seconds=s["seg_alt"])).strftime("%d/%m %H:%M"),
                         calculado_em=now.strftime("%H:%M"))
                logging.info("[%s] Rota alternativa para o GR: %s (%s min).", trip, s["motivo"], s["economia_min"])
            _cache[trip] = {"chave": chave, "em": now, "sugestao": s}
        else:
            continue
        if s:
            sugeridas += 1
            info["alternativa"] = {**s, "mensagem": mensagem(r, s, info.get("noticias") or ())}
            r["Rota Alternativa"] = (f"Economia ~{s['economia_min']} min" if s["economia_min"] > 0
                                     else f"Contorno +{-s['economia_min']} min") + f" ({s['motivo']}) · {s['link']}"
    for trip in [t for t in _cache if t not in ativas]:
        del _cache[trip]
    if ativas:
        logging.info("Rota alternativa: %s rota(s) com ocorrência ou desvio da TomTom, %s sugestão(ões) para o GR.",
                     len(ativas), sugeridas)
