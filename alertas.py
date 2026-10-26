# -*- coding: utf-8 -*-
"""Histórico das notícias no caminho e das rotas alternativas de cada viagem.

Resultado_Espelhamento é a foto do ciclo e o Fechamento_Rotas, a da chegada, quando
notícia e rota alternativa já não são procuradas: sem este histórico, o que apareceu
durante a viagem se perdia. Cada alerta novo vira uma linha na aba Historico_Alertas;
o estado da rota guarda o que já foi registrado e o resumo que vai para o fechamento."""

from datetime import datetime, timedelta

import noticias

ABA = "Historico_Alertas"
COLUNAS = ["Data/Hora", "Trip Number", "Tipo", "Placa", "Origem", "Destino", "Motorista", "Detalhe",
           "Cidade", "Km à Frente", "Economia (min)", "Chegada pela Rota", "Chegada pela Alternativa", "Link"]
# Rota alternativa volta a ser registrada quando muda o tipo, a economia muda mais que isto
# ou some por mais de ALTERNATIVA_SUMIDA_MIN (o motivo traz a distância, que muda a cada ciclo).
MUDANCA_ECONOMIA_MIN = 15
ALTERNATIVA_SUMIDA_MIN = 60
MAX_NOTICIAS_VISTAS = 40


def _dt(iso):
    try:
        return datetime.fromisoformat(iso) if iso else None
    except (TypeError, ValueError):
        return None


def registrar(state: dict, resultados: list, geo_por_trip: dict, now: datetime) -> list:
    """Linhas novas para a aba; atualiza o histórico guardado no estado de cada rota."""
    linhas = []
    for r in resultados:
        trip = r["Trip Number"]
        dados, info = state.get(trip), geo_por_trip.get(trip) or {}
        if dados is None:
            continue
        h = dados.setdefault("alertas", {})
        base = {"Data/Hora": now.strftime("%d/%m/%Y %H:%M"), "Trip Number": trip, "Placa": r.get("Placa", ""),
                "Origem": r.get("Origin Station Code", ""), "Destino": r.get("Destination Station Code", ""),
                "Motorista": r.get("Motorista", "")}
        vistas = h.setdefault("noticias", [])
        for n in info.get("noticias") or []:
            chave = n.get("link") or n.get("titulo")
            if not chave or chave in vistas:
                continue
            vistas.append(chave)
            del vistas[:-MAX_NOTICIAS_VISTAS]
            texto = noticias.resumo(n)
            h["qtd_noticia"] = h.get("qtd_noticia", 0) + 1
            h["ultima_noticia"] = texto
            linhas.append({**base, "Tipo": "Notícia", "Detalhe": texto, "Cidade": n.get("cidade", ""),
                           "Km à Frente": n.get("km", ""), "Link": n.get("link", "")})
        s = info.get("alternativa")
        if not s:
            continue
        ant = h.get("alternativa") or {}
        visto = _dt(ant.get("visto_em"))
        nova = (not ant or ant.get("tipo") != s["tipo"]
                or abs(ant.get("economia", 0) - s["economia_min"]) > MUDANCA_ECONOMIA_MIN
                or not visto or now - visto > timedelta(minutes=ALTERNATIVA_SUMIDA_MIN))
        h["alternativa"] = {"tipo": s["tipo"], "economia": s["economia_min"] if nova else ant["economia"],
                            "visto_em": now.isoformat()}
        if nova:
            h["qtd_alternativa"] = h.get("qtd_alternativa", 0) + 1
            h["ultima_alternativa"] = r.get("Rota Alternativa", "")
            linhas.append({**base, "Tipo": "Rota alternativa", "Detalhe": s["motivo"],
                           "Economia (min)": s["economia_min"], "Chegada pela Rota": s.get("chegada_ref", ""),
                           "Chegada pela Alternativa": s.get("chegada_alt", ""), "Link": s.get("link", "")})
    return linhas


def no_fechamento(dados: dict, linha: dict) -> None:
    """No fechamento, as colunas de notícia e rota alternativa trazem a viagem inteira."""
    h = dados.get("alertas") or {}
    if h.get("qtd_noticia"):
        linha["Alerta Notícia"] = f"{h['qtd_noticia']} na viagem · última: {h['ultima_noticia']}"
    if h.get("qtd_alternativa"):
        linha["Rota Alternativa"] = f"{h['qtd_alternativa']} na viagem · última: {h['ultima_alternativa']}"
