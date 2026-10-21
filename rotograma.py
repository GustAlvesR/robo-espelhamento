# -*- coding: utf-8 -*-
"""Rotogramas da Angellira (dados/rotogramas/*.json, gerados por importar_rotograma.py).

Para cada trecho com rotograma (origem -> destino):
  - localiza os pontos autorizados e as áreas de risco (TomTom, com cache), aos poucos,
    para não gastar a cota de um dia num ciclo só;
  - desenha a rota do rotograma passando pelos pontos autorizados, na ordem;
  - em cada rota desse trecho, avisa quando o caminhão está longe da rota do rotograma
    ou parado perto de uma área de risco ou fora dos pontos autorizados.

A posição da Angellira é por cidade ou ponto de referência, não GPS: as margens são
largas, e alerta de parada só sai com a posição no nível de ponto de referência ou
quando o texto da posição cita o próprio local."""

import functools
import glob
import json
import logging
import math
import os
import re
import unicodedata

import geo_tomtom

PASTA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dados", "rotogramas")
DESVIO_KM = {"referência": float(os.getenv("ROTOGRAMA_DESVIO_KM", "15")),
             "cidade": float(os.getenv("ROTOGRAMA_DESVIO_CIDADE_KM", "30"))}
RAIO_RISCO_KM = float(os.getenv("ROTOGRAMA_RAIO_RISCO_KM", "2"))
RAIO_PONTO_KM = float(os.getenv("ROTOGRAMA_RAIO_PONTO_KM", "3"))
RAIO_ESTACAO_KM = 5            # carga e descarga: parada na origem ou no destino é normal
PARADA_MIN = int(os.getenv("ROTOGRAMA_PARADA_MIN", "30"))
IDADE_MAX_POSICAO_MIN = 90
MAX_CHAMADAS_POR_CICLO = int(os.getenv("ROTOGRAMA_MAX_CHAMADAS_CICLO", "40"))
ESPACO_WAYPOINT_KM = 40
MAX_WAYPOINTS = 25
DESVIO_MAX_TRECHO = 1.6        # waypoint que faz a rota andar 60% a mais que a reta é descartado
TIPOS_SEM_ALERTA = {"FRIGORIFICO"}


def _norm(texto) -> str:
    t = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().upper()
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", t).split())


def _km(a, b) -> float:
    return geo_tomtom.distancia_km(a, b)


def distancia_a_linha_km(p, linha) -> float:
    """Menor distância do ponto à polilinha (projeção plana local, boa para dezenas de km)."""
    if not linha:
        return float("inf")
    kx = 111.32 * math.cos(math.radians(p[0]))
    melhor = float("inf")
    for a, b in zip(linha, linha[1:] or linha):
        ax, ay = (a[1] - p[1]) * kx, (a[0] - p[0]) * 110.57
        bx, by = (b[1] - p[1]) * kx, (b[0] - p[0]) * 110.57
        dx, dy = bx - ax, by - ay
        t = 0.0 if dx == dy == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / (dx * dx + dy * dy)))
        melhor = min(melhor, math.hypot(ax + t * dx, ay + t * dy))
    return melhor


@functools.lru_cache(maxsize=1)
def carregar() -> dict:
    """{(origem, destino): rotograma}."""
    saida = {}
    for arquivo in sorted(glob.glob(os.path.join(PASTA, "*.json"))):
        try:
            with open(arquivo, encoding="utf-8") as f:
                r = json.load(f)
            saida[(r["origem"].upper(), r["destino"].upper())] = r
        except Exception:
            logging.exception("Rotograma %s ilegível; ignorado.", arquivo)
    return saida


def _minutos(texto):
    m = re.fullmatch(r"\s*(\d+):(\d{2})(?::\d{2})?\s*", str(texto or ""))
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def _num(v):
    try:
        return float(str(v).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


class Rotogramas:
    def __init__(self):
        self._lugares = {}   # (nome, cidade, uf) -> (lat, lon, precisão) | None
        self.linhas = {}     # (origem, destino) -> {"assinatura", "pontos", "km"}

    def chave(self, r: dict):
        return (str(r.get("Origin Station Code", "")).strip().upper(),
                str(r.get("Destination Station Code", "")).strip().upper())

    def _local(self, geo, item, orcamento):
        k = (_norm(item["nome"]), _norm(item["cidade"]), item["uf"])
        if k in self._lugares:
            return self._lugares[k]
        if orcamento["restante"] <= 0:
            return geo_tomtom.PENDENTE
        antes = geo.chamadas_hoje
        achado = geo.localizar_lugar(item["nome"], item["cidade"].title(), item["uf"])
        orcamento["restante"] -= max(0, geo.chamadas_hoje - antes)
        if achado != geo_tomtom.PENDENTE:
            self._lugares[k] = achado
        return achado

    def preparar(self, geo, chaves_ativas, coords_por_chave: dict) -> dict:
        """Localiza os lugares e monta a linha dos trechos em uso. Devolve o dado do mapa
        por trecho: {(origem, destino): {...}}."""
        orcamento = {"restante": MAX_CHAMADAS_POR_CICLO}
        saida, pendentes = {}, 0
        for chave in sorted(chaves_ativas):
            rot = carregar().get(chave)
            if not rot:
                continue
            pontos = [(p, self._local(geo, p, orcamento)) for p in rot["pontos"]]
            riscos = [(r, self._local(geo, r, orcamento)) for r in rot["riscos"]]
            faltam = sum(1 for _i, c in pontos + riscos if c == geo_tomtom.PENDENTE)
            pendentes += faltam
            orig, dest = coords_por_chave.get(chave, (None, None))
            if not faltam and orig and dest:
                self._montar_linha(geo, chave, orig, dest, [c for _p, c in pontos if c])
            linha = self.linhas.get(chave, {})
            saida[chave] = {
                "sm": rot.get("sm", ""), "km_rotograma": rot.get("distancia_km"),
                "km_linha": linha.get("km"), "linha": linha.get("pontos", []),
                "localizando": faltam,
                "pontos": [[c[0], c[1], p["nome"], f"{p['cidade'].title()}/{p['uf']}", p.get("rodovia", ""), c[2]]
                           for p, c in pontos if c and c != geo_tomtom.PENDENTE],
                "riscos": [[c[0], c[1], r["tipo"], r["nome"], f"{r['cidade'].title()}/{r['uf']}", r.get("obs", ""), c[2]]
                           for r, c in riscos if c and c != geo_tomtom.PENDENTE],
            }
        if pendentes:
            logging.info("Rotogramas: %s lugar(es) ainda por localizar (%s chamada(s) TomTom por ciclo).",
                         pendentes, MAX_CHAMADAS_POR_CICLO)
        return saida

    def _montar_linha(self, geo, chave, orig, dest, lugares):
        """Rota pelos pontos autorizados localizados no nível de referência (postos na
        rodovia), espaçados, na ordem do rotograma. Ponto que desvia demais sai."""
        escolhidos, ultimo = [], orig
        for lat, lon, precisao in lugares:
            p = (lat, lon)
            if precisao == "referência" and _km(ultimo, p) >= ESPACO_WAYPOINT_KM and _km(p, dest) >= ESPACO_WAYPOINT_KM / 2:
                escolhidos.append(p)
                ultimo = p
        if len(escolhidos) > MAX_WAYPOINTS:
            passo = math.ceil(len(escolhidos) / MAX_WAYPOINTS)
            escolhidos = escolhidos[::passo]
        assinatura = (tuple(orig), tuple(dest), tuple(escolhidos))
        if self.linhas.get(chave, {}).get("assinatura") == assinatura:
            return
        for _tentativa in range(2):
            caminho = [tuple(orig), *escolhidos, tuple(dest)]
            r = geo.rota_por_pontos(caminho)
            if not r:
                return
            trechos = r.get("trechos_m") or []
            ruins = {i for i in range(1, len(caminho) - 1)
                     if len(trechos) == len(caminho) - 1 and trechos[i - 1] and trechos[i]
                     and (trechos[i - 1] + trechos[i]) / 1000 > DESVIO_MAX_TRECHO * max(_km(caminho[i - 1], caminho[i + 1]), 1)}
            if not ruins:
                break
            escolhidos = [p for i, p in enumerate(caminho[1:-1], start=1) if i not in ruins]
        self.linhas[chave] = {"assinatura": assinatura, "pontos": r["pontos"],
                              "km": round(r["dist_m"] / 1000) if r.get("dist_m") else None}
        logging.info("Rotograma %s -> %s: linha com %s ponto(s) de passagem, %s km.",
                     chave[0], chave[1], len(escolhidos), self.linhas[chave]["km"])

    def avaliar(self, r: dict, info: dict, mapa_trecho: dict, iniciada: bool) -> dict:
        """Alertas da rota r contra o rotograma do trecho."""
        res = {"sm": mapa_trecho.get("sm", ""), "alertas": [], "desvio_km": None}
        v = info.get("veiculo")
        idade = info.get("idade_pos_min")
        if (not iniciada or not v or idade is None or idade > IDADE_MAX_POSICAO_MIN
                or r.get("Risco de Atraso") in ("No destino", "Encerrada") or r.get("Status Geral") == "Cancelada"):
            return res
        precisao = info.get("precisao") or "cidade"
        linha = mapa_trecho.get("linha") or []
        if linha:
            d = distancia_a_linha_km(v, linha)
            res["desvio_km"] = round(d, 1)
            if d > DESVIO_KM.get(precisao, DESVIO_KM["cidade"]):
                res["alertas"].append(f"Fora da rota do rotograma ({d:.0f} km)")

        vel = _num(r.get("Velocidade (km/h)"))
        parado = vel == 0 if vel is not None else r.get("Ignição") == "Desligada"
        perto_estacao = any(e and _km(v, e) <= RAIO_ESTACAO_KM for e in (info.get("origem"), info.get("destino")))
        if not parado or perto_estacao:
            return res
        texto_pos = _norm(r.get("Posição Atual"))
        cidade_pos = _norm(str(r.get("Cidade/UF", "")).split("/")[0])

        def cita(nome, cidade):
            n = _norm(nome)
            return len(n) >= 6 and n in texto_pos and _norm(cidade) == cidade_pos

        for lat, lon, tipo, nome, cidade_uf, _obs, prec in mapa_trecho.get("riscos", []):
            if tipo in TIPOS_SEM_ALERTA:
                continue
            perto = prec == "referência" and precisao == "referência" and _km(v, (lat, lon)) <= RAIO_RISCO_KM
            if perto or cita(nome, cidade_uf.split("/")[0]):
                res["alertas"].append(f"Parado em {tipo.lower()}: {nome} ({cidade_uf})")
                return res
        minutos = _minutos(r.get("Tempo Parado"))
        if precisao != "referência" or minutos is None or minutos < PARADA_MIN:
            return res
        autorizado = any((prec == "referência" and _km(v, (lat, lon)) <= RAIO_PONTO_KM) or cita(nome, cidade_uf.split("/")[0])
                         for lat, lon, nome, cidade_uf, _rod, prec in mapa_trecho.get("pontos", []))
        if not autorizado:
            res["alertas"].append(f"Parado fora dos pontos autorizados há {minutos // 60}h{minutos % 60:02d}")
        return res


def aplicar(rotos: Rotogramas, geo, resultados: list, geo_por_trip: dict, iniciada) -> dict:
    """Coluna 'Alerta Rotograma' em cada resultado, dado por rota no mapa e o dado de cada
    trecho para o site."""
    coords, chaves = {}, set()
    for r in resultados:
        chave = rotos.chave(r)
        if chave in carregar():
            chaves.add(chave)
            g = geo_por_trip.get(r["Trip Number"]) or {}
            if g.get("origem") and g.get("destino"):
                coords.setdefault(chave, (g["origem"], g["destino"]))
    mapa = rotos.preparar(geo, chaves, coords) if chaves else {}
    for r in resultados:
        chave = rotos.chave(r)
        g = geo_por_trip.get(r["Trip Number"])
        if chave not in mapa or g is None:
            r["Alerta Rotograma"] = ""
            continue
        res = rotos.avaliar(r, g, mapa[chave], iniciada(r))
        g["rotograma"] = {"chave": "__".join(chave), **res}
        r["Alerta Rotograma"] = "; ".join(res["alertas"])
    return {"__".join(k): v for k, v in mapa.items()}
