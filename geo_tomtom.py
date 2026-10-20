# -*- coding: utf-8 -*-
"""
Camada de geografia do robô: coordenadas das estações, posição aproximada do
veículo (a partir do texto da Angellira) e rotas/ETA pela TomTom.

Princípios:
- Toda chamada à TomTom passa por um contador diário com teto
  (TOMTOM_MAX_CHAMADAS_DIA), para nunca estourar a cota gratuita.
- Geocodificação é cacheada para sempre (aba oculta _cache_geo).
- Geometria da rota planejada: 1 chamada por LH (cache em memória).
- ETA a partir da posição: só recalcula quando chega posição nova, e no
  máximo a cada ETA_RECALCULO_MIN minutos.
- Qualquer falha aqui NÃO derruba o ciclo do robô.
"""

import csv
import functools
import json
import logging
import math
import os
import re
import threading
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, date

TOMTOM_API_KEY = os.getenv("TOMTOM_API_KEY", "").strip()
TOMTOM_BASE = os.getenv("TOMTOM_BASE", "https://api.tomtom.com")
TOMTOM_MAX_CHAMADAS_DIA = int(os.getenv("TOMTOM_MAX_CHAMADAS_DIA", "2000"))
ETA_RECALCULO_MIN = int(os.getenv("ETA_RECALCULO_MIN", "30"))
# Posição mais velha que isso não gera ETA (o caminhão já pode estar longe).
ETA_MAX_IDADE_POSICAO_MIN = int(os.getenv("ETA_MAX_IDADE_POSICAO_MIN", "180"))
TOLERANCIA_ATRASO_MIN = int(os.getenv("TOLERANCIA_ATRASO_MIN", "30"))
# Ponto de referência da Angellira só é aceito se cair a até X km do centro da cidade.
RAIO_REFERENCIA_KM = float(os.getenv("RAIO_REFERENCIA_KM", "30"))
MAX_PONTOS_ROTA = 300

ESTACOES_WORKSHEET_NAME = os.getenv("ESTACOES_WORKSHEET_NAME", "Estacoes")
GEO_CACHE_WORKSHEET_NAME = os.getenv("GEO_CACHE_WORKSHEET_NAME", "_cache_geo")
CABECALHO_ESTACOES = ["Codigo", "Nome (Seguradora)", "Latitude", "Longitude", "Fonte"]

_PREFIXOS_ESTACAO = {"SOC", "FBS", "HUB", "XPT", "LM", "FM", "DC", "CD", "SB"}
# Veículo a menos disso do destino (ou com % percorrido >= 99) é tratado como chegado.
RAIO_CHEGADA_KM = float(os.getenv("RAIO_CHEGADA_KM", "5"))
_UFS = {"AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA", "MT", "MS", "MG", "PA",
        "PB", "PR", "PE", "PI", "RJ", "RN", "RS", "RO", "RR", "SC", "SP", "SE", "TO"}


def _sem_acento(t: str) -> str:
    return unicodedata.normalize("NFKD", str(t)).encode("ascii", "ignore").decode().upper().strip()


def _parse_br(texto):
    texto = str(texto or "").strip()
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(texto, fmt)
        except ValueError:
            pass
    return None


def _num(v):
    try:
        return float(str(v).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None


def distancia_km(a, b) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def consulta_da_estacao(nome: str):
    """'SOC_MG_BETIM' -> 'BETIM, MG'; 'SOC_PE_RECIFE_04' -> 'RECIFE, PE';
    'SOC_PE_JABOATÃO DOS GUARARAPES (NOVO)' -> 'JABOATAO DOS GUARARAPES, PE'.
    Devolve None se não houver UF reconhecível."""
    consultas = consultas_da_estacao(nome)
    return consultas[0] if consultas else None


def consultas_da_estacao(nome: str) -> list:
    """Consultas em ordem de preferência: nome completo e, como reserva, só o
    primeiro trecho ('CONTAGEM INDUSTRIAL, MG' -> 'CONTAGEM, MG')."""
    t = re.sub(r"\(.*?\)", " ", _sem_acento(nome))
    prefixos = "|".join(sorted(_PREFIXOS_ESTACAO))
    t = re.sub(rf"^(?:(?:{prefixos})[\s_\-]+)+", "", t)
    partes = [p.strip() for p in re.split(r"[_\-]", t) if p.strip()]
    if not partes or partes[0] not in _UFS:
        return []
    uf = partes.pop(0)
    partes = [p for p in partes if not p.isdigit()]
    if not partes:
        return []
    saida = [f"{' '.join(partes)}, {uf}"]
    if len(partes) > 1:
        saida.append(f"{partes[0]}, {uf}")
    return saida


def referencia_da_posicao(posicao: str, cidade: str):
    """'0.33 Km - GESTAMP - BETIM' -> 'GESTAMP'. Devolve None quando o texto já
    é só a cidade ('1.59 Km - SANTO ANTONIO DO AMPARO/MG')."""
    m = re.match(r"^\s*[\d.,]+\s*KM\s*-\s*(.+)$", _sem_acento(posicao))
    if not m:
        return None
    resto = re.sub(r"\(.*?\)", " ", m.group(1)).strip(" *")
    cid = _sem_acento(cidade)
    # 'BETIM/MG - WMB COMERCIO...' -> 'WMB COMERCIO...'
    resto = re.sub(r"^[A-Z .']+/[A-Z]{2}\s*(-\s*)?", "", resto).strip(" *")
    if not resto or (cid and resto.split("/")[0].strip() == cid):
        return None
    ref = re.sub(r"\s+", " ", resto.split(" - ")[0]).strip(" *")
    # Pedaços genéricos que não identificam lugar nenhum.
    if len(ref) < 4 or (cid and ref == cid):
        return None
    return ref


class GeoTomTom:
    def __init__(self, api_key: str = TOMTOM_API_KEY, http_get=None):
        self.key = api_key
        self._http_get = http_get or self._http_get_real
        self._lock = threading.Lock()
        self.geocache = {}          # consulta -> [lat, lon] | None
        self.geocache_alterado = False
        self.geocache_carregado = False
        self.rotas = {}             # trip -> {"chave", "pontos", "dur_s", "dist_m"}
        self.etas = {}              # trip -> {"chave", "calc_em", "travel_s"}
        self.dia = date.today()
        self.chamadas_hoje = 0
        self.ultimo_erro = ""

    # ------------------------------------------------------------------ HTTP
    @staticmethod
    def _http_get_real(url: str):
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read().decode("utf-8"))

    def _chamar(self, caminho: str, params: dict):
        if not self.key:
            return None
        with self._lock:
            if date.today() != self.dia:
                self.dia, self.chamadas_hoje = date.today(), 0
            if self.chamadas_hoje >= TOMTOM_MAX_CHAMADAS_DIA:
                self.ultimo_erro = "Teto diário de chamadas atingido"
                return None
            self.chamadas_hoje += 1
        params = {**params, "key": self.key}
        url = f"{TOMTOM_BASE}{caminho}?{urllib.parse.urlencode(params)}"
        try:
            return self._http_get(url)
        except urllib.error.HTTPError as e:
            self.ultimo_erro = f"HTTP {e.code} em {caminho.split('/')[1]}"
            logging.warning("TomTom %s: HTTP %s.", caminho.split("/")[1], e.code)
        except Exception as e:
            self.ultimo_erro = f"{type(e).__name__} em {caminho.split('/')[1]}"
            logging.warning("TomTom falhou (%s).", e)
        return None

    # ----------------------------------------------------------- Geocoding
    def geocodificar(self, consulta: str):
        """Cidade/endereço -> (lat, lon). Cacheado, inclusive os 'não achei'."""
        consulta = _sem_acento(consulta)
        if not consulta:
            return None
        if consulta in self.geocache:
            v = self.geocache[consulta]
            return tuple(v) if v else None
        dados = self._chamar(f"/search/2/geocode/{urllib.parse.quote(consulta, safe='')}.json",
                             {"countrySet": "BR", "limit": 1, "language": "pt-BR"})
        if dados is None:
            return None  # falha de rede/cota: não cacheia, tenta de novo depois
        res = (dados.get("results") or [None])[0]
        pos = (res or {}).get("position")
        valor = [round(pos["lat"], 5), round(pos["lon"], 5)] if pos else None
        self.geocache[consulta] = valor
        self.geocache_alterado = True
        return tuple(valor) if valor else None

    def buscar_referencia(self, referencia: str, centro):
        """Ponto de referência (POI) perto do centro da cidade, ou None."""
        chave = f"POI|{_sem_acento(referencia)}|{centro[0]:.3f},{centro[1]:.3f}"
        if chave in self.geocache:
            v = self.geocache[chave]
            return tuple(v) if v else None
        dados = self._chamar(f"/search/2/search/{urllib.parse.quote(referencia, safe='')}.json", {
            "countrySet": "BR", "limit": 1, "lat": centro[0], "lon": centro[1],
            "radius": int(RAIO_REFERENCIA_KM * 1000), "language": "pt-BR"})
        if dados is None:
            return None
        res = (dados.get("results") or [None])[0]
        pos = (res or {}).get("position")
        valor = None
        if pos and distancia_km(centro, (pos["lat"], pos["lon"])) <= RAIO_REFERENCIA_KM:
            valor = [round(pos["lat"], 5), round(pos["lon"], 5)]
        self.geocache[chave] = valor
        self.geocache_alterado = True
        return tuple(valor) if valor else None

    def posicao_veiculo(self, cidade_uf: str, posicao_texto: str):
        """Devolve (lat, lon, precisao) com precisao 'referência' ou 'cidade'."""
        if not cidade_uf or "/" not in cidade_uf:
            return None
        cidade, uf = cidade_uf.rsplit("/", 1)
        centro = self.geocodificar(f"{cidade}, {uf}")
        if not centro:
            return None
        ref = referencia_da_posicao(posicao_texto, cidade)
        if ref:
            ponto = self.buscar_referencia(ref, centro)
            if ponto:
                return (ponto[0], ponto[1], "referência")
        return (centro[0], centro[1], "cidade")

    # -------------------------------------------------------------- Routing
    def _rota(self, origem, destino, depart_at: datetime, com_pontos: bool):
        params = {"travelMode": "truck", "traffic": "true", "routeType": "fastest",
                  "vehicleCommercial": "true", "language": "pt-BR",
                  "departAt": depart_at.strftime("%Y-%m-%dT%H:%M:%S") + "-03:00"}
        if not com_pontos:
            params["routeRepresentation"] = "summaryOnly"
        locs = f"{origem[0]},{origem[1]}:{destino[0]},{destino[1]}"
        dados = self._chamar(f"/routing/1/calculateRoute/{locs}/json", params)
        if not dados or not dados.get("routes"):
            return None
        rota = dados["routes"][0]
        resumo = rota.get("summary", {})
        pontos = []
        if com_pontos:
            for leg in rota.get("legs", []):
                pontos += [[round(p["latitude"], 5), round(p["longitude"], 5)] for p in leg.get("points", [])]
            if len(pontos) > MAX_PONTOS_ROTA:
                passo = math.ceil(len(pontos) / MAX_PONTOS_ROTA)
                pontos = pontos[::passo] + [pontos[-1]]
        return {"dur_s": resumo.get("travelTimeInSeconds"),
                "dist_m": resumo.get("lengthInMeters"),
                "atraso_transito_s": resumo.get("trafficDelayInSeconds", 0),
                "pontos": pontos}

    def rota_planejada(self, trip, origem, destino, inicio: datetime, now: datetime):
        chave = (tuple(origem), tuple(destino), inicio.isoformat() if inicio else "")
        atual = self.rotas.get(trip)
        if atual and atual["chave"] == chave:
            return atual
        # departAt no passado é recusado pela API; usa o maior entre início e agora.
        partida = max(inicio or now, now + timedelta(minutes=1))
        r = self._rota(origem, destino, partida, com_pontos=True)
        if r:
            r["chave"] = chave
            self.rotas[trip] = r
        return r

    def eta_da_posicao(self, trip, posicao, destino, dt_pos: datetime, now: datetime):
        chave = (tuple(posicao[:2]), tuple(destino), dt_pos.isoformat())
        atual = self.etas.get(trip)
        if atual and atual["chave"] == chave and now - atual["calc_em"] < timedelta(minutes=ETA_RECALCULO_MIN):
            return atual
        r = self._rota(posicao[:2], destino, now + timedelta(minutes=1), com_pontos=False)
        if not r:
            return atual  # mantém o último cálculo válido, se houver
        novo = {"chave": chave, "calc_em": now, "travel_s": r["dur_s"],
                "atraso_transito_s": r["atraso_transito_s"], "dist_m": r["dist_m"]}
        self.etas[trip] = novo
        return novo

    def limpar(self, trips_ativas: set):
        for d in (self.rotas, self.etas):
            for t in [t for t in d if t not in trips_ativas]:
                del d[t]


# =============================================================================
# Persistência no Sheets (cache de geocodificação e aba Estacoes)
# =============================================================================

def carregar_geocache(geo: GeoTomTom, spreadsheet):
    if geo.geocache_carregado:
        return
    try:
        ws = spreadsheet.worksheet(GEO_CACHE_WORKSHEET_NAME)
        texto = "".join(r[0] for r in ws.get_all_values() if r).strip()
        geo.geocache = json.loads(texto) if texto else {}
    except Exception as e:
        if type(e).__name__ != "WorksheetNotFound":
            logging.warning("Cache de geocodificação ilegível (%s); começando vazio.", e)
        geo.geocache = {}
    geo.geocache_carregado = True


def salvar_geocache(geo: GeoTomTom, spreadsheet):
    if not geo.geocache_alterado:
        return
    try:
        try:
            ws = spreadsheet.worksheet(GEO_CACHE_WORKSHEET_NAME)
        except Exception:
            ws = spreadsheet.add_worksheet(title=GEO_CACHE_WORKSHEET_NAME, rows=50, cols=1)
            try:
                ws.hide()
            except Exception:
                pass
        texto = json.dumps(geo.geocache, ensure_ascii=False)
        pedacos = [[texto[i:i + 40000]] for i in range(0, len(texto), 40000)] or [[""]]
        ws.clear()
        ws.update(values=pedacos, range_name="A1", raw=True)
        geo.geocache_alterado = False
    except Exception:
        logging.exception("Falha ao gravar o cache de geocodificação.")


def _geocodificar_estacao(geo: GeoTomTom, nome: str):
    for consulta in consultas_da_estacao(nome or ""):
        ponto = geo.geocodificar(consulta)
        if ponto:
            return ponto
    return None


def sincronizar_estacoes(geo: GeoTomTom, spreadsheet, nomes_por_codigo: dict) -> dict:
    """Lê a aba Estacoes (Codigo | Nome (Seguradora) | Latitude | Longitude | Fonte),
    acrescenta códigos novos e preenche lat/lon vazios pela TomTom.
    Linhas com Fonte 'manual' ou com lat/lon já preenchidos nunca são alteradas.
    Devolve {codigo: (lat, lon)}."""
    try:
        ws = spreadsheet.worksheet(ESTACOES_WORKSHEET_NAME)
    except Exception:
        ws = spreadsheet.add_worksheet(title=ESTACOES_WORKSHEET_NAME, rows=200, cols=5)
        ws.update(values=[CABECALHO_ESTACOES], range_name="A1")
    linhas = ws.get_all_values()
    if not linhas:
        ws.update(values=[CABECALHO_ESTACOES], range_name="A1")
        linhas = [CABECALHO_ESTACOES]

    coords, existentes, atualizacoes = {}, {}, []
    for n, l in enumerate(linhas[1:], start=2):
        l = (l + [""] * 5)[:5]
        codigo = l[0].strip().upper()
        if not codigo:
            continue
        existentes[codigo] = n
        lat, lon = _num(l[2]), _num(l[3])
        if lat is not None and lon is not None:
            coords[codigo] = (lat, lon)
            continue
        if l[4].strip().lower() == "manual":
            continue
        nome = l[1].strip() or nomes_por_codigo.get(codigo, "")
        ponto = _geocodificar_estacao(geo, nome)
        if ponto:
            coords[codigo] = ponto
            atualizacoes.append({"range": f"B{n}:E{n}",
                                 "values": [[nome, ponto[0], ponto[1], "auto (cidade)"]]})
        elif nome and not l[1].strip():
            atualizacoes.append({"range": f"B{n}", "values": [[nome]]})

    novas = []
    for codigo, nome in sorted(nomes_por_codigo.items()):
        if codigo in existentes or not codigo:
            continue
        ponto = _geocodificar_estacao(geo, nome)
        if ponto:
            coords[codigo] = ponto
            novas.append([codigo, nome, ponto[0], ponto[1], "auto (cidade)"])
        else:
            novas.append([codigo, nome, "", "", "preencher"])
        existentes[codigo] = -1

    if atualizacoes:
        ws.batch_update(atualizacoes, value_input_option="RAW")
    if novas:
        ws.append_rows(novas, value_input_option="RAW")
        logging.info("Aba %s: %s estação(ões) nova(s).", ESTACOES_WORKSHEET_NAME, len(novas))
    return coords


ARQUIVO_CADASTRO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dados", "estacoes_shopee.csv")


@functools.lru_cache(maxsize=1)
def cadastro_estacoes() -> dict:
    """{codigo: (nome, cidade, uf)} da planilha de endereços da Shopee
    (gerado por importar_estacoes.py)."""
    try:
        with open(ARQUIVO_CADASTRO, encoding="utf-8", newline="") as f:
            return {l["codigo"].strip().upper(): (l["nome"].strip(), l["cidade"].strip(), l["uf"].strip())
                    for l in csv.DictReader(f) if l["codigo"].strip()}
    except OSError:
        logging.warning("Cadastro de estações %s não encontrado; siglas sem cidade.", ARQUIVO_CADASTRO)
        return {}


def local_estacao(codigo) -> str:
    """'Betim/MG' para SOC-MG2; vazio se a sigla não estiver no cadastro."""
    _nome, cidade, uf = cadastro_estacoes().get(str(codigo or "").strip().upper(), ("", "", ""))
    return f"{cidade}/{uf}" if cidade and uf else cidade or uf


def tipo_estacao(codigo: str) -> str:
    return {"HUB": "HUB", "FMH": "FM HUB", "SOC": "SOC"}.get(codigo.split("-", 1)[0], "Outros")


def ler_estacoes_mapa(spreadsheet) -> list:
    """Todas as estações da aba Estacoes com latitude e longitude válidas."""
    coords, nomes = {}, {}
    for l in spreadsheet.worksheet(ESTACOES_WORKSHEET_NAME).get_all_values()[1:]:
        l = (l + [""] * 5)[:5]
        codigo = l[0].strip().upper()
        lat, lon = _num(l[2]), _num(l[3])
        if codigo and lat is not None and lon is not None and -90 <= lat <= 90 and -180 <= lon <= 180:
            coords[codigo] = (lat, lon)
            if l[1].strip():
                nomes[codigo] = l[1].strip()
    return estacoes_para_mapa(coords, nomes)


def estacoes_para_mapa(coords: dict, nomes: dict) -> list:
    cadastro = cadastro_estacoes()
    return [{"codigo": c, "nome": nomes.get(c) or cadastro.get(c, ("",))[0], "local": local_estacao(c),
             "tipo": tipo_estacao(c), "lat": round(lat, 6), "lon": round(lon, 6)}
            for c, (lat, lon) in sorted(coords.items())]


# =============================================================================
# Enriquecimento por rota (chamado pelo robô a cada ciclo)
# =============================================================================

def calcular_geo(geo: GeoTomTom, resultados: list, coords_estacoes: dict, now: datetime) -> dict:
    """Devolve {trip: dados_para_o_mapa} e acrescenta 3 colunas a cada resultado:
    'Previsão TomTom', 'Desvio vs Fim Previsto (min)', 'Risco de Atraso'."""
    saida = {}
    for r in resultados:
        trip = r["Trip Number"]
        orig = coords_estacoes.get(str(r.get("Origin Station Code", "")).strip().upper())
        dest = coords_estacoes.get(str(r.get("Destination Station Code", "")).strip().upper())
        inicio = _parse_br(r.get("Início Previsto"))
        fim = _parse_br(r.get("Fim Previsto"))
        dt_pos = _parse_br(r.get("Data da Posição"))
        encerrada = r.get("Viagem Encerrada (Seguradora)") == "Sim"

        info = {"origem": orig, "destino": dest, "pontos": [], "veiculo": None,
                "precisao": "", "idade_pos_min": None, "rota_tomtom_min": None,
                "planejado_min": None}

        try:
            if orig and dest:
                rp = geo.rota_planejada(trip, orig, dest, inicio, now)
                if rp:
                    info["pontos"] = rp["pontos"]
                    info["rota_tomtom_min"] = round(rp["dur_s"] / 60) if rp.get("dur_s") else None
            if inicio and fim:
                info["planejado_min"] = round((fim - inicio).total_seconds() / 60)

            if r.get("Cidade/UF"):
                pv = geo.posicao_veiculo(r["Cidade/UF"], r.get("Posição Atual", ""))
                if pv:
                    info["veiculo"], info["precisao"] = [pv[0], pv[1]], pv[2]
            if dt_pos:
                info["idade_pos_min"] = max(0, round((now - dt_pos).total_seconds() / 60))

            previsao, desvio, risco = None, None, ""
            pct = _num(str(r.get("% Percorrido (Seguradora)", "")).replace("%", ""))
            chegou = (pct is not None and pct >= 99) or (
                info["veiculo"] and dest and distancia_km(info["veiculo"], dest) <= RAIO_CHEGADA_KM)
            if encerrada:
                risco = "Encerrada"
            elif chegou:
                risco = "No destino"
            elif info["veiculo"] and dest and dt_pos:
                if info["idade_pos_min"] > ETA_MAX_IDADE_POSICAO_MIN:
                    risco = "Posição antiga"
                else:
                    eta = geo.eta_da_posicao(trip, info["veiculo"], dest, dt_pos, now)
                    if eta and eta.get("travel_s") is not None:
                        vel = _num(r.get("Velocidade (km/h)"))
                        # A velocidade manda; a ignição só decide quando não há velocidade.
                        parado = vel == 0 if vel is not None else r.get("Ignição") == "Desligada"
                        # Parado: sai "agora". Em movimento: o tempo de viagem conta da hora da posição.
                        # Parado antes do início previsto: assume saída no horário previsto.
                        base = max(now, inicio or now) if parado else dt_pos
                        previsao = base + timedelta(seconds=eta["travel_s"])
            elif info["rota_tomtom_min"] is not None and inicio and (not dt_pos) and now < inicio:
                previsao = inicio + timedelta(minutes=info["rota_tomtom_min"])
            elif not dest:
                risco = "Sem coord. da estação"
            elif not r.get("Cidade/UF"):
                risco = "Sem posição"

            if previsao and fim:
                desvio = round((previsao - fim).total_seconds() / 60)
                risco = "Atraso provável" if desvio > TOLERANCIA_ATRASO_MIN else "No prazo"
        except Exception:
            logging.exception("[%s] Falha no cálculo geográfico; seguindo sem ele.", trip)
            previsao, desvio, risco = None, None, "Erro no cálculo"

        r["Previsão TomTom"] = previsao.strftime("%d/%m/%Y %H:%M") if previsao else ""
        r["Desvio vs Fim Previsto (min)"] = desvio if desvio is not None else ""
        r["Risco de Atraso"] = risco
        saida[trip] = info

    geo.limpar({r["Trip Number"] for r in resultados})
    return saida
