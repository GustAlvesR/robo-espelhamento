# -*- coding: utf-8 -*-
"""Notícias de acidente e interdição nas rodovias, ligadas às rotas pela cidade citada.

Fonte: busca do Google Notícias (RSS), uma consulta por rodovia dos corredores. Notícia
sai de 30 min a 2 h depois do fato e quase nunca traz o km, então complementa a TomTom e o
aviso do motorista (quanto dura uma interdição, se a pista foi liberada, manifestação).
A notícia só vale para a rota quando cita uma cidade a até NOTICIAS_RAIO_KM do caminho
que falta percorrer. Cidades vêm de dados/municipios.csv (IBGE, com coordenadas da sede).
Falha aqui nunca derruba o ciclo do robô."""

import csv
import email.utils
import html
import logging
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import alternativa
import geo_tomtom

NOTICIAS_URL = "https://news.google.com/rss/search?q={q}&hl=pt-BR&gl=BR&ceid=BR:pt-419"
# Rodovias consultadas além das que aparecem nos rotogramas.
NOTICIAS_RODOVIAS = os.getenv("NOTICIAS_RODOVIAS", "BR-116,BR-101,BR-381,BR-040,BR-251,BR-135,BR-324,"
                                                   "BR-262,BR-050,BR-153,BR-232,BR-104,BR-146")
NOTICIAS_ATUALIZAR_MIN = int(os.getenv("NOTICIAS_ATUALIZAR_MIN", "15"))
NOTICIAS_MAX_H = int(os.getenv("NOTICIAS_MAX_H", "6"))
NOTICIAS_RAIO_KM = float(os.getenv("NOTICIAS_RAIO_KM", "20"))
NOTICIAS_KM_FRENTE = float(os.getenv("NOTICIAS_KM_FRENTE", "600"))
NOTICIAS_MAX_CONSULTAS = int(os.getenv("NOTICIAS_MAX_CONSULTAS", "20"))
MAX_POR_ROTA = 3
# Duas consultas seguidas com erro: o Google está recusando, o resto fica para a próxima rodada.
MAX_FALHAS_SEGUIDAS = 2
TIMEZONE = os.getenv("TIMEZONE", "America/Sao_Paulo")
ARQUIVO_MUNICIPIOS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dados", "municipios.csv")

TERMOS_BUSCA = ('(acidente OR interditada OR interdição OR interditado OR bloqueio OR bloqueada OR tombamento '
                'OR tomba OR engavetamento OR "queda de barreira" OR deslizamento OR manifestação OR protesto '
                'OR "pista liberada")')
# Do mais grave para o menos; "liberada" vence os outros (a notícia é do fim da ocorrência).
TIPOS = [
    ("liberada", r"LIBERAD|REABERT|NORMALIZAD"),
    ("interdição", r"INTERDI|BLOQUE|FECHAD|INTERROMP"),
    ("queda de barreira", r"BARREIRA|DESLIZAMENT|DESMORON|CRATERA"),
    ("manifestação", r"MANIFESTA|PROTEST|GREVE"),
    ("acidente", r"ACIDENT|TOMB[AO]|TOMBAMENT|COLIS|COLID|ENGAVET|CAPOT|BATIDA"),
]
# BR com ou sem hífen ("BR-116", "BR 116"); estadual só com hífen ("SP-330"), senão "se 100" vira rodovia.
RODOVIA = re.compile(r"\b(?:(BR)[\s-]?|(SP|MG|BA|PE|RJ|GO|PR|SC|RS|ES|MS|MT|PB|RN|CE|AL|SE|PI|MA|PA|TO|DF)-)(\d{3})\b")
CONECTORES = {"DE", "DA", "DO", "DAS", "DOS", "E", "D"}
# Cidades com nome de palavra que aparece em maiúscula dentro de outros nomes
# ("Serra do Cafezal", "Zona da Mata", "Ponte da Amizade").
NOMES_IGNORADOS = {"SERRA", "MATA", "CENTRAL", "PLANALTO", "UNIAO", "FRONTEIRA", "CRUZ", "BARRA", "LAGOA",
                   "PEDRA", "PORTO", "PRATA", "OURO", "ESPERANCA", "LIBERDADE", "PROGRESSO", "ALIANCA",
                   "HARMONIA", "CONCORDIA", "TRIUNFO", "PARAISO", "JARDIM", "FLORESTA", "CAPELA", "COLINA",
                   "PALMEIRA", "AREIA", "ESTRELA", "PRIMAVERA", "CRISTAL", "PONTAL"}
# Nome de estado na notícia é pista da UF, não cidade (menos as capitais de mesmo nome).
ESTADOS = {"ACRE": "AC", "ALAGOAS": "AL", "AMAPA": "AP", "AMAZONAS": "AM", "BAHIA": "BA", "CEARA": "CE",
           "DISTRITO FEDERAL": "DF", "ESPIRITO SANTO": "ES", "GOIAS": "GO", "MARANHAO": "MA",
           "MATO GROSSO": "MT", "MATO GROSSO DO SUL": "MS", "MINAS GERAIS": "MG", "MINAS": "MG", "PARA": "PA",
           "PARAIBA": "PB", "PARANA": "PR", "PERNAMBUCO": "PE", "PIAUI": "PI", "RIO GRANDE DO NORTE": "RN",
           "RIO GRANDE DO SUL": "RS", "RONDONIA": "RO", "RORAIMA": "RR", "SANTA CATARINA": "SC",
           "SERGIPE": "SE", "TOCANTINS": "TO"}
UFS = set(ESTADOS.values()) | {"SP", "RJ"}


def _norm(texto) -> str:
    t = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().upper()
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", t).split())


def _palavras(texto: str) -> list:
    return re.findall(r"[0-9A-Za-zÀ-ÖØ-öø-ÿ]+", texto)


def carregar_municipios(caminho: str = ARQUIVO_MUNICIPIOS) -> dict:
    """{NOME NORMALIZADO: [(lat, lon, uf, nome), ...]} (há nomes repetidos entre estados)."""
    indice = {}
    with open(caminho, encoding="utf-8") as f:
        for r in csv.DictReader(f, delimiter=";"):
            indice.setdefault(_norm(r["nome"]), []).append((float(r["lat"]), float(r["lon"]), r["uf"], r["nome"]))
    return indice


def cidades_citadas(texto: str, municipios: dict) -> list:
    """Cidades citadas no texto, [(lat, lon, uf, nome)]. Vale o nome mais longo, com as
    palavras em maiúscula (título em português só põe maiúscula em nome próprio). Com a
    UF citada ("/BA", "(BA)", "na Bahia"), ficam só as homônimas dessa UF."""
    palavras = _palavras(texto)
    normais = [_norm(p) for p in palavras]
    ufs = {m for m in re.findall(r"(?:[/(]\s?|-\s)([A-Z]{2})\b", texto) if m in UFS}
    achados, i = [], 0
    while i < len(palavras):
        for n in range(min(6, len(palavras) - i), 0, -1):
            frase = " ".join(normais[i:i + n])
            if frase in ESTADOS and palavras[i][:1].isupper() and (frase != "PARA" or "Á" in palavras[i].upper()):
                ufs.add(ESTADOS[frase])
                break
            if frase in municipios and frase not in NOMES_IGNORADOS and all(
                    p[:1].isupper() or p[:1].isdigit() or q in CONECTORES
                    for p, q in zip(palavras[i:i + n], normais[i:i + n])) and palavras[i][:1].isupper():
                achados.append(frase)
                break
        else:
            n = 1
        i += n
    cidades = []
    for frase in dict.fromkeys(achados):
        opcoes = municipios[frase]
        na_uf = [c for c in opcoes if c[2] in ufs]
        cidades += na_uf or opcoes
    return cidades


def tipo_da_noticia(texto: str) -> str:
    t = _norm(texto)
    return next((tipo for tipo, padrao in TIPOS if re.search(padrao, t)), "")


def rodovias_citadas(texto: str) -> list:
    return list(dict.fromkeys(f"{br or uf}-{n}" for br, uf, n in RODOVIA.findall(str(texto or "").upper())))


def _data(texto):
    try:
        d = email.utils.parsedate_to_datetime(texto)
    except (TypeError, ValueError):
        return None
    return d.astimezone(ZoneInfo(TIMEZONE)).replace(tzinfo=None) if d.tzinfo else d


def ler_rss(xml_texto: str) -> list:
    """Itens do feed do Google Notícias: título (sem o ' - Fonte' do fim), fonte, link,
    data e o texto da descrição (traz as manchetes do mesmo assunto em outros veículos)."""
    raiz = ET.fromstring(xml_texto)
    itens = []
    for item in raiz.iter("item"):
        campo = lambda nome: (item.findtext(nome) or "").strip()
        fonte = campo("source")
        titulo = campo("title")
        if fonte and titulo.endswith(f" - {fonte}"):
            titulo = titulo[:-len(fonte) - 3].strip()
        descricao = html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(campo("description"))))
        link = campo("link") or campo("guid")
        if titulo and link:
            itens.append({"titulo": titulo, "fonte": fonte, "link": link, "data": _data(campo("pubDate")),
                          "descricao": " ".join(descricao.split())})
    return itens


def _rodovia_padrao(texto) -> str:
    achadas = rodovias_citadas(texto)
    return achadas[0] if achadas else ""


def rodovias_dos_rotogramas(rotogramas_mapa: dict) -> list:
    """Rodovias citadas nos pontos dos rotogramas, da mais citada para a menos."""
    contagem = {}
    for rot in (rotogramas_mapa or {}).values():
        for p in rot.get("pontos") or []:
            r = _rodovia_padrao(p[4]) if len(p) > 4 else ""
            if r:
                contagem[r] = contagem.get(r, 0) + 1
    return sorted(contagem, key=lambda r: -contagem[r])


class NoticiasRodovias:
    def __init__(self, http_get=None, municipios=None):
        self._http_get = http_get or self._http_get_real
        self._municipios = municipios
        self._proxima = None
        self.noticias = {}       # link -> notícia recente, com tipo, cidades e rodovias
        self.lido_em = None
        self.ultimo_erro = ""

    @staticmethod
    def _http_get_real(url: str) -> str:
        req = urllib.request.Request(url, headers={"Accept": "application/rss+xml, application/xml, */*",
                                                   "User-Agent": "Mozilla/5.0 (compatible; TorreLH-CEVA/1.0)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.read().decode("utf-8", errors="replace")

    def consultas(self, rodovias_extra=()) -> list:
        rodovias = list(dict.fromkeys(r for r in (*map(_rodovia_padrao, rodovias_extra),
                                                  *map(_rodovia_padrao, NOTICIAS_RODOVIAS.split(","))) if r))
        # A consulta sem rodovia pega as estaduais e as notícias que só falam em "rodovia".
        textos = [f"(rodovia OR estrada) {TERMOS_BUSCA}"] + [f'"{r}" {TERMOS_BUSCA}' for r in rodovias]
        return [t + f" when:{NOTICIAS_MAX_H}h" for t in textos][:NOTICIAS_MAX_CONSULTAS]

    def atualizar(self, now: datetime, rodovias_extra=()) -> dict:
        limite = now - timedelta(hours=NOTICIAS_MAX_H)
        self.noticias = {k: v for k, v in self.noticias.items() if v["data"] >= limite}
        if self._proxima and now < self._proxima:
            return self.noticias
        if self._municipios is None:
            self._municipios = carregar_municipios()
        self._proxima = now + timedelta(minutes=NOTICIAS_ATUALIZAR_MIN)
        feitas, falhas, seguidas, recebidas = 0, 0, 0, 0
        for consulta in self.consultas(rodovias_extra):
            try:
                itens = ler_rss(self._http_get(NOTICIAS_URL.format(q=urllib.parse.quote(consulta))))
                seguidas = 0
            except (urllib.error.URLError, OSError, ET.ParseError, ValueError) as e:
                falhas, seguidas = falhas + 1, seguidas + 1
                self.ultimo_erro = f"{type(e).__name__}: {e}"[:200]
                if seguidas >= MAX_FALHAS_SEGUIDAS:
                    break
                continue
            feitas += 1
            recebidas += len(itens)
            for it in itens:
                if it["link"] in self.noticias or not it["data"] or it["data"] < limite or it["data"] > now + timedelta(hours=1):
                    continue
                tipo = tipo_da_noticia(it["titulo"])
                if not tipo:
                    continue
                texto = f"{it['titulo']}. {it['descricao']}"
                self.noticias[it["link"]] = {**it, "tipo": tipo, "rodovias": rodovias_citadas(texto),
                                             "cidades": cidades_citadas(texto, self._municipios)}
        if feitas:
            self.lido_em, self.ultimo_erro = now, (self.ultimo_erro if falhas else "")
        com_cidade = sum(1 for n in self.noticias.values() if n["cidades"])
        logging.info("Notícias: %s de %s consulta(s) ao Google Notícias, %s item(ns) recebido(s); %s notícia(s) de "
                     "ocorrência nas últimas %s h, %s com cidade identificada.%s", feitas, feitas + falhas, recebidas,
                     len(self.noticias), NOTICIAS_MAX_H, com_cidade,
                     f" Falha: {self.ultimo_erro}" if falhas else "")
        return self.noticias


def _caminho_a_frente(info: dict, rot: dict, iniciada: bool) -> list:
    """Caminhos que o caminhão ainda pode fazer: o da TomTom a partir da posição e o do
    rotograma (ou o planejado) a partir do ponto mais próximo dele. Antes do início, o
    rotograma ou o planejado inteiros."""
    base = (rot or {}).get("linha") or info.get("pontos") or []
    veiculo = info.get("veiculo")
    if iniciada and veiculo:
        return [c for c in (info.get("rota_atual") or [], alternativa.a_frente(base, veiculo)) if len(c) >= 2]
    return [base] if len(base) >= 2 else []


def _onde_passa(caminhos: list, lat: float, lon: float):
    """(km a percorrer até a cidade, distância da cidade ao caminho) no caminho mais
    próximo, ou None se ela fica longe de todos."""
    melhor = None
    for caminho in caminhos:
        lats, lons = [p[0] for p in caminho], [p[1] for p in caminho]
        margem = NOTICIAS_RAIO_KM / 90
        if not (min(lats) - margem <= lat <= max(lats) + margem and min(lons) - margem <= lon <= max(lons) + margem):
            continue
        km = 0.0
        for i, p in enumerate(caminho):
            if i:
                km += geo_tomtom.distancia_km(caminho[i - 1], p)
            if km > NOTICIAS_KM_FRENTE:
                break
            d = geo_tomtom.distancia_km(p, (lat, lon))
            if d <= NOTICIAS_RAIO_KM and (melhor is None or d < melhor[1]):
                melhor = (round(km), d)
    return melhor


def resumo(n: dict) -> str:
    rod = f" {n['rodovias'][0]}" if n["rodovias"] else ""
    return (f"{n['tipo'].capitalize()}{rod} em {n['cidade']} a {n['km']} km{n['desde']} "
            f"({n['fonte'] or 'notícia'}, {n['quando']}): {n['titulo']}")


def aplicar_nas_rotas(noticias: NoticiasRodovias, resultados: list, geo_por_trip: dict, rotogramas_mapa: dict,
                      iniciada) -> None:
    """Coluna 'Alerta Notícia' e info['noticias'] (até MAX_POR_ROTA, das mais perto para as mais longe)."""
    com_cidade = [n for n in noticias.noticias.values() if n["cidades"]]
    rotas = 0
    for r in resultados:
        r["Alerta Notícia"] = ""
        info = geo_por_trip.get(r["Trip Number"])
        if (not info or not com_cidade or r.get("Cancelamento") or r.get("Viagem Encerrada (Seguradora)") == "Sim"
                or r.get("Risco de Atraso") in ("Encerrada", "No destino")):
            continue
        rot = rotogramas_mapa.get((info.get("rotograma") or {}).get("chave", "")) or {}
        em_viagem = iniciada(r)
        caminhos = _caminho_a_frente(info, rot, em_viagem)
        achadas = []
        for n in com_cidade:
            melhor = None
            for lat, lon, uf, nome in n["cidades"]:
                passa = _onde_passa(caminhos, lat, lon)
                if passa and (melhor is None or passa[0] < melhor[0]):
                    melhor = (passa[0], lat, lon, f"{nome}/{uf}")
            if melhor:
                achadas.append({"titulo": n["titulo"], "fonte": n["fonte"], "link": n["link"], "tipo": n["tipo"],
                                "quando": n["data"].strftime("%d/%m %H:%M"), "rodovias": n["rodovias"],
                                "km": melhor[0], "desde": "" if em_viagem else " da origem",
                                "lat": melhor[1], "lon": melhor[2], "cidade": melhor[3]})
        achadas.sort(key=lambda a: a["km"])
        info["noticias"] = achadas[:MAX_POR_ROTA]
        if achadas:
            rotas += 1
            r["Alerta Notícia"] = "; ".join(resumo(a) for a in info["noticias"])
    logging.info("Notícias: %s notícia(s) recente(s) com cidade identificada, %s rota(s) com notícia no caminho.",
                 len(com_cidade), rotas)
