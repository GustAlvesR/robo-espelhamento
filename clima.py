# -*- coding: utf-8 -*-
"""Alertas de chuva do INMET para o mapa e para as rotas.

Fonte: feed RSS de avisos do INMET, um item por aviso, com link para o aviso no
padrão CAP (Common Alerting Protocol), que traz evento, severidade, vigência e
o polígono da área. Só entram avisos de chuva e tempestade vigentes (ou que
começam nas próximas horas). Falha aqui nunca derruba o ciclo do robô."""

import logging
import os
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

INMET_RSS_URL = os.getenv("INMET_RSS_URL", "https://apiprevmet3.inmet.gov.br/avisos/rss")
INMET_ATUALIZAR_MIN = int(os.getenv("INMET_ATUALIZAR_MIN", "30"))
# Aviso que começa dentro disto já é mostrado (a rota pode chegar lá nesse período).
INMET_ANTECEDENCIA_H = int(os.getenv("INMET_ANTECEDENCIA_H", "12"))
EVENTOS_CHUVA = ("CHUVA", "TEMPESTADE", "TEMPORAL")
# Feed que falha (conexão derrubada pelo servidor do INMET, por exemplo) é tentado
# de novo no mesmo ciclo e, se continuar falhando, no ciclo seguinte.
INMET_RETENTATIVA_MIN = int(os.getenv("INMET_RETENTATIVA_MIN", "5"))
ESPERAS_RETENTATIVA_S = (2, 6)
# Avisos novos lidos por ciclo: o resto fica para o próximo (o primeiro ciclo não trava).
INMET_MAX_NOVOS_POR_CICLO = int(os.getenv("INMET_MAX_NOVOS_POR_CICLO", "40"))
MAX_VERTICES = 150
TIMEZONE = os.getenv("TIMEZONE", "America/Sao_Paulo")

# Severidade CAP -> escala de cores do INMET.
NIVEIS = {
    "MODERATE": (1, "Perigo Potencial"), "SEVERE": (2, "Perigo"), "EXTREME": (3, "Grande Perigo"),
    "PERIGO POTENCIAL": (1, "Perigo Potencial"), "PERIGO": (2, "Perigo"), "GRANDE PERIGO": (3, "Grande Perigo"),
}


def _norm(texto) -> str:
    return unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().upper().strip()


def _filho(no, nome):
    """Primeiro filho com esse nome local, ignorando o namespace (CAP 1.1 ou 1.2)."""
    for f in no:
        if f.tag.rsplit("}", 1)[-1] == nome:
            return f
    return None


def _filhos(no, nome):
    return [f for f in no if f.tag.rsplit("}", 1)[-1] == nome]


def _texto(no, nome) -> str:
    f = _filho(no, nome) if no is not None else None
    return (f.text or "").strip() if f is not None and f.text else ""


def _data(texto):
    texto = str(texto or "").strip()
    if not texto:
        return None
    try:
        d = datetime.fromisoformat(texto.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                return datetime.strptime(texto, fmt)
            except ValueError:
                pass
        return None
    return d.astimezone(ZoneInfo(TIMEZONE)).replace(tzinfo=None) if d.tzinfo else d


def _poligono(texto):
    """'lat,lon lat,lon ...' (CAP) -> [(lat, lon), ...]."""
    pontos = []
    for par in str(texto or "").split():
        try:
            lat, lon = (float(v) for v in par.split(",")[:2])
        except ValueError:
            continue
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            pontos.append((lat, lon))
    return pontos if len(pontos) >= 3 else []


def ler_cap(xml_texto: str) -> list:
    """Avisos (um por bloco <info>) de um documento CAP."""
    raiz = ET.fromstring(xml_texto)
    if raiz.tag.rsplit("}", 1)[-1] != "alert":
        raise ValueError(f"não é CAP (raiz <{raiz.tag}>)")
    avisos = []
    for info in _filhos(raiz, "info"):
        poligonos = [p for area in _filhos(info, "area")
                     for p in (_poligono(el.text) for el in _filhos(area, "polygon")) if p]
        areas = [_texto(area, "areaDesc") for area in _filhos(info, "area")]
        avisos.append({
            "evento": _texto(info, "event"),
            "severidade": _texto(info, "severity"),
            "inicio": _data(_texto(info, "onset") or _texto(info, "effective")),
            "fim": _data(_texto(info, "expires")),
            "descricao": _texto(info, "description"),
            "area": "; ".join(a for a in areas if a),
            "poligonos": poligonos,
        })
    return avisos


def ler_rss(xml_texto: str) -> list:
    """[(título, link)] dos itens do feed."""
    raiz = ET.fromstring(xml_texto)
    itens = []
    for item in raiz.iter():
        if item.tag.rsplit("}", 1)[-1] != "item":
            continue
        link = _texto(item, "link") or _texto(item, "guid")
        if link:
            itens.append((_texto(item, "title"), link))
    return itens


def _titulo_sem_chuva(titulo: str) -> bool:
    """'Aviso de Baixa Umidade' dá para descartar sem baixar o aviso; título
    genérico ou vazio não decide nada."""
    t = _norm(titulo)
    return "AVISO" in t and not any(p in t for p in EVENTOS_CHUVA)


def _reduzir(poligono):
    if len(poligono) <= MAX_VERTICES:
        return poligono
    passo = -(-len(poligono) // MAX_VERTICES)
    return poligono[::passo] + [poligono[-1]]


def _dentro(lat, lon, poli) -> bool:
    dentro, j = False, len(poli) - 1
    for i in range(len(poli)):
        yi, xi = poli[i]
        yj, xj = poli[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            dentro = not dentro
        j = i
    return dentro


class AlertasInmet:
    def __init__(self, http_get=None, dormir=time.sleep):
        self._http_get = http_get or self._http_get_real
        self._dormir = dormir
        self._lock = threading.Lock()
        self._caps = {}          # link -> avisos já lidos (aviso publicado não muda)
        self._proxima = None     # próxima leitura do feed
        self.avisos = []         # avisos de chuva vigentes, com polígono e caixa
        self.lido_em = None      # última leitura do feed que deu certo
        self.ultimo_erro = ""

    def _baixar(self, url: str, esperas=ESPERAS_RETENTATIVA_S) -> str:
        """Tenta de novo em falha de rede e erro 5xx/429; outro erro HTTP não adianta repetir."""
        for espera in (*esperas, None):
            try:
                return self._http_get(url)
            except urllib.error.HTTPError as e:
                if e.code != 429 and e.code < 500 or espera is None:
                    raise
            except (OSError, TimeoutError):
                if espera is None:
                    raise
            self._dormir(espera)

    @staticmethod
    def _http_get_real(url: str) -> str:
        req = urllib.request.Request(url, headers={"Accept": "application/xml, text/xml, */*",
                                                   "User-Agent": "TorreLH-CEVA/1.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", errors="replace")

    def atualizar(self, now: datetime) -> list:
        # Com o feed fora do ar, continua valendo a última leitura, menos o que já venceu.
        self.avisos = [a for a in self.avisos if not a["fim"] or a["fim"] >= now]
        if self._proxima and now < self._proxima:
            return self.avisos
        with self._lock:
            try:
                itens = ler_rss(self._baixar(INMET_RSS_URL))
            except Exception as e:
                self.ultimo_erro = f"{type(e).__name__}: {e}"[:200]
                logging.warning("INMET: falha ao ler o feed (%s); segue a leitura de %s, nova tentativa em %s min.",
                                self.ultimo_erro, _fmt(self.lido_em) or "nenhuma", INMET_RETENTATIVA_MIN)
                self._proxima = now + timedelta(minutes=INMET_RETENTATIVA_MIN)
                return self.avisos
            vigentes, falhas, novos = [], 0, 0
            for titulo, link in itens:
                if link not in self._caps:
                    if _titulo_sem_chuva(titulo):
                        self._caps[link] = []
                        continue
                    if novos >= INMET_MAX_NOVOS_POR_CICLO:
                        continue
                    novos += 1
                    try:
                        self._caps[link] = ler_cap(self._baixar(link, esperas=(2,)))
                    except Exception as e:
                        falhas += 1
                        if falhas <= 2:
                            logging.warning("INMET: aviso %s não lido (%s: %s).", link, type(e).__name__, str(e)[:200])
                        continue
                for a in self._caps[link]:
                    if not any(p in _norm(a["evento"]) for p in EVENTOS_CHUVA) or not a["poligonos"]:
                        continue
                    if a["fim"] and a["fim"] < now:
                        continue
                    if a["inicio"] and a["inicio"] > now + timedelta(hours=INMET_ANTECEDENCIA_H):
                        continue
                    nivel, rotulo = NIVEIS.get(_norm(a["severidade"]), (1, a["severidade"] or "Aviso"))
                    for poli in a["poligonos"]:
                        lats, lons = [p[0] for p in poli], [p[1] for p in poli]
                        vigentes.append({**a, "poligono": _reduzir(poli), "nivel": nivel, "rotulo": rotulo,
                                         "caixa": (min(lats), max(lats), min(lons), max(lons))})
            ativos = {link for _t, link in itens}
            self._caps = {k: v for k, v in self._caps.items() if k in ativos}
            self.avisos, self.lido_em, self.ultimo_erro = vigentes, now, ""
            self._proxima = now + timedelta(minutes=INMET_ATUALIZAR_MIN)
            logging.info("INMET: %s aviso(s) no feed, %s área(s) de chuva vigente(s), %s não lido(s).",
                         len(itens), len(vigentes), falhas)
            return vigentes

    def avisos_no_caminho(self, pontos) -> list:
        """Avisos cujas áreas o caminho atravessa, do mais grave para o menos."""
        achados = {}
        for a in self.avisos:
            la0, la1, lo0, lo1 = a["caixa"]
            if any(la0 <= p[0] <= la1 and lo0 <= p[1] <= lo1 and _dentro(p[0], p[1], a["poligono"]) for p in pontos):
                chave = (a["evento"], a["rotulo"], a["fim"])
                achados[chave] = a
        return sorted(achados.values(), key=lambda a: -a["nivel"])

    def para_mapa(self) -> list:
        return [{"evento": a["evento"], "rotulo": a["rotulo"], "nivel": a["nivel"],
                 "inicio": _fmt(a["inicio"]), "fim": _fmt(a["fim"]), "area": a["area"][:300],
                 "poligono": [[round(p[0], 3), round(p[1], 3)] for p in a["poligono"]]}
                for a in self.avisos]


def _fmt(d) -> str:
    return d.strftime("%d/%m %H:%M") if d else ""


def resumo(avisos: list) -> str:
    """'Perigo: Chuvas Intensas até 21/10 12:00' (o mais grave primeiro)."""
    return "; ".join(f"{a['rotulo']}: {a['evento']}" + (f" até {_fmt(a['fim'])}" if a["fim"] else "")
                     for a in avisos)


def caminho_da_rota(info: dict, viagem_iniciada: bool) -> list:
    """Pontos a checar: o que falta percorrer (caminho atual da TomTom) ou, sem
    ele, o trajeto planejado inteiro; mais o destino."""
    pontos = list(info.get("rota_atual") or []) if viagem_iniciada else []
    if not pontos:
        pontos = list(info.get("pontos") or [])
    if info.get("destino"):
        pontos.append(info["destino"])
    return pontos


def aplicar_nas_rotas(alertas: AlertasInmet, resultados: list, geo_por_trip: dict, iniciada) -> None:
    """Coluna 'Alerta Chuva (INMET)' em cada resultado e a lista no dado do mapa."""
    for r in resultados:
        g = geo_por_trip.get(r["Trip Number"]) or {}
        avisos = alertas.avisos_no_caminho(caminho_da_rota(g, iniciada(r))) if alertas.avisos else []
        r["Alerta Chuva (INMET)"] = resumo(avisos)
        g["clima"] = [{"evento": a["evento"], "rotulo": a["rotulo"], "nivel": a["nivel"],
                       "inicio": _fmt(a["inicio"]), "fim": _fmt(a["fim"])} for a in avisos]
