# -*- coding: utf-8 -*-
"""
monitor_espelhamento.py
------------------------
Robô de monitoramento de telemetria (espelhamento) de rotas logísticas.

O QUE ELE FAZ
-------------
A cada N minutos, o robô:
  1. Lê a aba "Telemetria" da planilha de origem (alimentada em tempo real
     pelo banco DBLH do cliente).
  2. Para cada rota (trip_number), atualiza um "estado" persistido em disco
     (state_espelhamento.json), detectando:
       - a primeira vez que a rota aparece;
       - transições de status "OK" -> "Não espelhado" (perda de sinal) e
         "Não espelhado" -> "OK" (retorno de sinal). Uma mesma rota pode
         perder/retornar sinal várias vezes ao longo da viagem;
       - se o espelhamento já estava "OK" com pelo menos 1h30 de
         antecedência do horário previsto de início (SLA de início).
  3. Calcula o percentual de tempo em que a rota ficou efetivamente
     espelhada, na janela da Shopee: do que vier primeiro entre ETA Origem
     programado e CPT realizado até o ETA Destino realizado (aba DBLH).
  4. Escreve o resultado consolidado em uma aba de resultados e registra
     cada evento de perda/retorno em uma aba de histórico (auditoria).

Autor: gerado para uso interno (Orbyte Tech / operação CEVA-Shopee)
"""

import json
import logging
import numbers
import os
import re
import unicodedata
import time
import functools
import threading
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import gspread
import schedule
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError, WorksheetNotFound

import clima
import geo_tomtom


# =============================================================================
# 1. CONFIGURAÇÃO — edite esta seção conforme o seu ambiente
# =============================================================================

# Caminho do arquivo JSON da Service Account (ver README.md para como gerar).
# No Render: use Secret File (/etc/secrets/service_account.json) ou, de
# preferência alternativa, a variável GOOGLE_CREDENTIALS_JSON com o JSON inteiro.
CREDENTIALS_FILE = os.getenv("CREDENTIALS_FILE", "/etc/secrets/service_account.json")
GOOGLE_CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")  # opcional

# Fuso horário da operação. O Render roda em UTC, mas as datas da planilha
# estão em horário de Brasília. Os datetimes internos continuam "naive"
# (sem tzinfo), porém já em horário de Brasília.
TIMEZONE = os.getenv("TIMEZONE", "America/Sao_Paulo")

# ID da planilha de ORIGEM (a que recebe dados do DBLH em tempo real).
# É o trecho entre "/d/" e "/edit" na URL da planilha.
SOURCE_SPREADSHEET_ID = os.getenv("SOURCE_SPREADSHEET_ID", "17N_PsIh_ekz1pDopyArV-DnkRqemDctsFSWnsn7K_oE")
SOURCE_WORKSHEET_NAME = "Telemetria"

# ID da planilha de RESULTADOS (pode ser a mesma planilha de origem ou uma
# planilha separada — recomenda-se uma planilha separada para não competir
# com as escritas automáticas do DBLH).
RESULTS_SPREADSHEET_ID = os.getenv("RESULTS_SPREADSHEET_ID", "1qsluTmC9w_4GggvtvJQk6D5PMKYb0v7B6kZjRwKJm9A")
RESULTS_WORKSHEET_NAME = "Resultado_Espelhamento"
EVENTS_WORKSHEET_NAME = "Historico_Quedas_Sinal"  # aba de auditoria (extra)
# Rotas canceladas: abas da planilha de ORIGEM consultadas a cada ciclo.
# (aba, cabeçalhos aceitos para o trip, cabeçalhos aceitos para o status)
CANCELAMENTO_FONTES = [
    ("DBLH", ("trip_number",), ("status_agrupado",)),
    ("LH SEMANA ATUAL", ("LH TRIP",), ("STATUS REAL",)),
]
# Comparados sem acento/espaço (ver _norm_chave). DELETADA fica de fora de propósito.
STATUS_CANCELAMENTO = {"CANCELADO", "CANCELADA", "NOSHOW", "INFRUTIFERA", "NAOCONSUMIDA"}
# Uma linha por rota, gravada uma vez quando ela termina (retrato final).
FECHAMENTO_WORKSHEET_NAME = os.getenv("FECHAMENTO_WORKSHEET_NAME", "Fechamento_Rotas")
# Rota que some da Telemetria só é fechada depois disto (evita fechar numa piscada do DBLH).
FECHAMENTO_SUMIDA_MIN = int(os.getenv("FECHAMENTO_SUMIDA_MIN", "30"))

# Intervalo de execução do robô.
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))

# Regra de negócio: SLA de início — quantos minutos antes do início previsto
# o espelhamento já deveria estar "OK".
SLA_MINUTES_BEFORE_START = int(os.getenv("SLA_MINUTES_BEFORE_START", "90"))  # 1h30

# Janela do % de espelhamento (regra da Shopee): começa no que vier primeiro
# entre o ETA Origem programado e o CPT realizado e termina no ETA Destino
# realizado. Os realizados vêm desta aba (mesma planilha da Telemetria).
DBLH_WORKSHEET_NAME = os.getenv("DBLH_WORKSHEET_NAME", "DBLH")

# Rota que saiu da aba Telemetria continua em Resultado_Espelhamento, com o
# resultado final e "Status Viagem" = FINALIZADA, por estas horas após a chegada
# (tempo para o BSC copiar o resultado definitivo).
FINALIZADAS_HORAS = int(os.getenv("FINALIZADAS_HORAS", "24"))

# Meta de % de espelhamento durante a rota (usada apenas para compor a coluna
# extra "status_geral" — Sim/Não do SLA continua sendo a regra 2 pedida).
META_PERCENTUAL_ESPELHAMENTO = float(os.getenv("META_PERCENTUAL_ESPELHAMENTO", "95.0"))

# Havendo rotas antigas que já terminaram e sumiram da planilha de origem,
# depois de quantos dias podemos apagar o estado delas do JSON local.
STATE_RETENTION_DAYS = int(os.getenv("STATE_RETENTION_DAYS", "5"))

# Arquivo de estado (memória do robô entre execuções).
# No Render, aponte para o Persistent Disk (ex.: /data/state_espelhamento.json).
STATE_FILE = os.getenv("STATE_FILE", "state_espelhamento.json")

# Rastreamento da seguradora (Angellira): aba preenchida a partir do arquivo baixado
# no portal. Por padrão fica na planilha de RESULTADOS (onde o robô tem permissão
# de editor e a Telemetria não é sobrescrita pelo DBLH).
TRACKING_SPREADSHEET_ID = os.getenv("TRACKING_SPREADSHEET_ID", "") or os.getenv(
    "RESULTS_SPREADSHEET_ID", "1qsluTmC9w_4GggvtvJQk6D5PMKYb0v7B6kZjRwKJm9A")
TRACKING_WORKSHEET_NAME = os.getenv("TRACKING_WORKSHEET_NAME", "Rastreamento_Angellira")
# Acima disto (minutos entre a posição mais recente do arquivo e agora) o
# rastreamento é marcado como "Desatualizado".
TRACKING_MAX_AGE_MIN = int(os.getenv("TRACKING_MAX_AGE_MIN", "30"))
# Só entra o procedimento cujo texto (normalizado) contenha TODOS estes termos.
TRACKING_PROCEDIMENTO_TERMOS = ("SHOPEE", "LINEHAUL")

# Onde guardar o estado: "file" (arquivo local/Persistent Disk) ou "sheets"
# (aba oculta na planilha de resultados — recomendado no Web Service gratuito
# do Render, cujo disco é apagado a cada reinício/hibernação).
STATE_BACKEND = os.getenv("STATE_BACKEND", "sheets").lower()
STATE_WORKSHEET_NAME = os.getenv("STATE_WORKSHEET_NAME", "_state_robo")
STATE_CHUNK_SIZE = 40000  # limite do Sheets é 50.000 caracteres por célula

# Web Service: o Render exige uma porta HTTP aberta. O PORT é injetado por ele.
HTTP_PORT = int(os.getenv("PORT", "10000"))
# URL pública (o Render define RENDER_EXTERNAL_URL automaticamente). Se existir,
# o robô se auto-pinga para tentar evitar a hibernação do plano gratuito.
SELF_PING_URL = os.getenv("SELF_PING_URL", os.getenv("RENDER_EXTERNAL_URL", ""))
SELF_PING_MINUTES = int(os.getenv("SELF_PING_MINUTES", "10"))

# Arquivo de log.
# No Render os logs vão só para o console (aba Logs). Defina LOG_FILE para
# gravar também em arquivo (útil só em execução local).
LOG_FILE = os.getenv("LOG_FILE", "")


def now_local() -> datetime:
    """Agora no fuso da operação, como datetime naive (compatível com as
    datas da planilha e com o estado já gravado em ISO)."""
    return datetime.now(ZoneInfo(TIMEZONE)).replace(tzinfo=None)

# Valores esperados na coluna "Espelhamento" da planilha de origem.
STATUS_OK = "OK"
# Uma mudança de status (OK <-> Não espelhado) só vale depois de lida em N ciclos
# seguidos. 1 = comportamento antigo (aceita na primeira leitura).
CONFIRMACAO_LEITURAS = max(1, int(os.getenv("CONFIRMACAO_LEITURAS", "2")))
STATUS_NOK = "NAO ESPELHADO"  # comparado após normalização (ver classify_status)


# =============================================================================
# 2. LOGGING
# =============================================================================

def setup_logging():
    handlers = [logging.StreamHandler()]
    if LOG_FILE:
        handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        handlers=handlers,
    )


# =============================================================================
# 3. AUTENTICAÇÃO GOOGLE SHEETS
# =============================================================================

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


def get_gspread_client():
    """Autentica no Google Sheets usando a Service Account."""
    if GOOGLE_CREDENTIALS_JSON:
        info = json.loads(GOOGLE_CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    return gspread.authorize(creds)


def gspread_retry(max_retries=5, base_delay=2):
    """
    Decorator para tornar chamadas ao gspread resilientes a instabilidades
    da API do Google (erro 429 - limite de requisições, 500/503 - erro
    temporário do servidor). Faz backoff exponencial entre as tentativas.
    Essencial para um robô que roda continuamente sem supervisão.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except APIError as e:
                    status = None
                    try:
                        status = e.response.status_code
                    except Exception:
                        pass
                    if status in (429, 500, 503) and attempt < max_retries:
                        wait = base_delay * (2 ** (attempt - 1))
                        logging.warning(
                            "Erro de API do Google (%s) na tentativa %s/%s. "
                            "Aguardando %ss antes de tentar novamente.",
                            status, attempt, max_retries, wait,
                        )
                        time.sleep(wait)
                        continue
                    raise
        return wrapper
    return decorator


# =============================================================================
# 4. ESTADO PERSISTIDO (memória do robô entre execuções)
# =============================================================================
#
# Estrutura do state_espelhamento.json:
#
# {
#   "<trip_number>": {
#       "last_status": "OK" | "NOK" | "SEM_DADO",
#       "last_seen": "2026-09-09T17:00:00",           # última vez visto na origem
#
#       "sla_check_done": true/false,                  # regra 2 já avaliada?
#       "sla_result": "Sim" | "Não" | null,
#       "sla_checked_at": "2026-09-09T16:30:00" | null,
#
#       "current_loss_start": "2026-09-09T10:00:00" | null,  # queda em aberto
#       "loss_periods": [                              # histórico de quedas fechadas
#           {"perda": "...", "retorno": "...", "duracao_segundos": 2100}
#       ],
#       "total_loss_seconds": 2100                     # soma de todas as quedas
#   },
#   ...
# }
#
# Por que um dict simples salvo em JSON, e não um banco de dados?
#   - O robô roda em ciclos curtos (minutos), então não precisa de
#     concorrência real; um arquivo local é suficiente e não exige infra.
#   - JSON é fácil de inspecionar manualmente se algo parecer errado.
#   - A escrita é feita de forma atômica (grava em arquivo temporário e
#     substitui o original) para nunca deixar o arquivo de estado corrompido
#     caso o processo seja encerrado no meio de uma gravação.
# =============================================================================

def _get_state_worksheet(client):
    spreadsheet = client.open_by_key(RESULTS_SPREADSHEET_ID)
    try:
        return spreadsheet.worksheet(STATE_WORKSHEET_NAME)
    except WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=STATE_WORKSHEET_NAME, rows=200, cols=1)
        try:
            ws.hide()  # aba técnica: esconde para não poluir a planilha
        except Exception:
            pass
        return ws


@gspread_retry()
def load_state(client=None) -> dict:
    if STATE_BACKEND == "sheets":
        try:
            ws = _get_state_worksheet(client)
            partes = [r[0] for r in ws.get_all_values() if r]
            texto = "".join(partes).strip()
            return json.loads(texto) if texto else {}
        except json.JSONDecodeError as e:
            logging.error("Estado no Sheets inválido (%s). Iniciando vazio.", e)
            return {}
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logging.error("Não foi possível ler %s (%s). Iniciando estado vazio.", STATE_FILE, e)
        return {}


@gspread_retry()
def save_state(state: dict, client=None) -> None:
    """Sheets: grava o JSON em pedaços (1 por linha, coluna A).
    Arquivo: gravação atômica (temporário + os.replace)."""
    texto = json.dumps(state, ensure_ascii=False, default=str)
    if STATE_BACKEND == "sheets":
        ws = _get_state_worksheet(client)
        pedacos = [[texto[i:i + STATE_CHUNK_SIZE]]
                   for i in range(0, len(texto), STATE_CHUNK_SIZE)] or [[""]]
        ws.clear()
        ws.update(values=pedacos, range_name="A1", raw=True)
        return
    pasta = os.path.dirname(STATE_FILE)
    if pasta:
        os.makedirs(pasta, exist_ok=True)
    tmp_path = STATE_FILE + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(texto)
    os.replace(tmp_path, STATE_FILE)


def cleanup_state(state: dict, trip_numbers_ativos: set) -> None:
    """Remove do estado rotas que não aparecem mais na origem há mais de
    STATE_RETENTION_DAYS dias, para o arquivo não crescer indefinidamente."""
    limite = now_local() - timedelta(days=STATE_RETENTION_DAYS)
    para_remover = []
    for trip_number, dados in state.items():
        if trip_number in trip_numbers_ativos:
            continue
        last_seen = _parse_iso(dados.get("last_seen"))
        if last_seen and last_seen < limite:
            para_remover.append(trip_number)
    for trip_number in para_remover:
        del state[trip_number]
    if para_remover:
        logging.info("Estado limpo: %s rota(s) antiga(s) removida(s).", len(para_remover))


def _parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# =============================================================================
# 5. LEITURA E NORMALIZAÇÃO DOS DADOS DE ORIGEM
# =============================================================================

REQUIRED_COLUMNS = {
    "trip_number": "trip_number",
    "eta_scheduled_origin_edited": "eta_scheduled_origin_edited",
    "eta_destination_edited": "eta_destination_edited",
    "espelhamento": "Espelhamento",
}


# Colunas informativas (opcionais) da aba Telemetria que são repassadas para a
# aba de resultados. Para cada uma: nomes de cabeçalho aceitos (comparação sem
# diferenciar maiúsculas, espaços ou "_") e a letra da coluna como reserva,
# usada se nenhum cabeçalho bater. Se nada for encontrado, a coluna sai vazia
# e o robô segue funcionando (só registra um aviso no log).
OPTIONAL_COLUMNS = {
    # chave interna:            (nomes aceitos,                                   letra, título na saída)
    "vehicle_number":           (["vehicle_number"],                              "E", "Placa"),
    "vehicle_number2":          (["vehicle_number2"],                             "F", "Placa 2"),
    "origin_station_code":      (["origin_station_code"],                         "H", "Origin Station Code"),
    "destination_station_code": (["destination_station_code"],                    "I", "Destination Station Code"),
    "drive_id":                 (["drive_id", "driver_id"],                       "O", "Drive ID"),
}

# Status da viagem no DBLH (PRE VIAGEM / EM TRANSITO). Vai para a última coluna do
# Resultado ("Status Viagem") e decide no site se o trajeto é só previsto.
STATUS_VIAGEM_NOMES = {"statuscorrigido"}


def _norm_cab(texto) -> str:
    return str(texto).strip().lower().replace(" ", "").replace("_", "")


def _letra_para_indice(letra: str) -> int:
    n = 0
    for ch in letra.upper():
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def _resolver_colunas_opcionais(colunas_originais: list, excluir: set = frozenset()) -> dict:
    """Devolve {nome_da_coluna_na_planilha: chave_interna} para as opcionais."""
    por_nome = {_norm_cab(c): c for c in colunas_originais}
    mapa = {}
    for chave, (nomes, letra, _titulo) in OPTIONAL_COLUMNS.items():
        achou = next((por_nome[_norm_cab(n)] for n in nomes if _norm_cab(n) in por_nome), None)
        origem = "cabeçalho"
        if achou is None:
            idx = _letra_para_indice(letra)
            if idx < len(colunas_originais):
                candidata = colunas_originais[idx]
                if candidata in excluir or candidata in mapa:
                    logging.warning(
                        "Coluna opcional '%s': a coluna %s ('%s') já é usada para outra finalidade; "
                        "ignorada. Confira o cabeçalho na aba Telemetria.", chave, letra, candidata)
                else:
                    achou, origem = candidata, f"coluna {letra}"
        if achou is None:
            logging.warning("Coluna opcional '%s' não encontrada (nem pelo nome nem pela letra %s).",
                            chave, letra)
        elif achou not in mapa:
            logging.info("Coluna '%s' <- '%s' (%s).", chave, achou, origem)
            mapa[achou] = chave
    return mapa


@gspread_retry()
def read_source_dataframe(client: gspread.Client) -> pd.DataFrame:
    """Lê a aba Telemetria e devolve um DataFrame já com as colunas
    necessárias renomeadas para nomes internos padronizados."""
    ws = client.open_by_key(SOURCE_SPREADSHEET_ID).worksheet(SOURCE_WORKSHEET_NAME)
    # numericise_ignore=['all']: tudo como texto (não perde zeros à esquerda de IDs/placas)
    registros = ws.get_all_records(numericise_ignore=["all"])  # 1ª linha = cabeçalho
    df = pd.DataFrame(registros)

    if df.empty:
        return df

    # Mapeamento case-insensitive para não quebrar se o cabeçalho vier com
    # variação de maiúsculas/espaços.
    colunas_normalizadas = {c.strip().lower(): c for c in df.columns}
    renomear = {}
    for chave_interna, nome_esperado in REQUIRED_COLUMNS.items():
        achou = colunas_normalizadas.get(nome_esperado.strip().lower())
        if achou is None:
            raise KeyError(
                f"Coluna obrigatória '{nome_esperado}' não encontrada na aba "
                f"'{SOURCE_WORKSHEET_NAME}'. Colunas disponíveis: {list(df.columns)}"
            )
        renomear[achou] = chave_interna

    # Colunas informativas (opcionais). Resolvidas ANTES do rename das obrigatórias.
    renomear.update(_resolver_colunas_opcionais(list(df.columns), excluir=set(renomear)))
    df = df.rename(columns=renomear)
    for chave in OPTIONAL_COLUMNS:
        if chave not in df.columns:
            df[chave] = ""

    # Sem reserva por letra: pegar a coluna errada aqui marcaria rotas como "não iniciadas".
    col_status = next((c for c in df.columns if _norm_cab(c) in STATUS_VIAGEM_NOMES), None)
    df["status_viagem"] = df[col_status] if col_status else ""
    if col_status is None:
        logging.warning("Coluna status_corrigido não encontrada; 'Status Viagem' sai vazio.")

    # Remove linhas sem trip_number (linhas em branco no fim da planilha).
    df = df[df["trip_number"].astype(str).str.strip() != ""]

    # Converte datas. dayfirst=False pois o formato observado é YYYY-MM-DD HH:MM.
    df["eta_scheduled_origin_edited"] = pd.to_datetime(
        df["eta_scheduled_origin_edited"], errors="coerce"
    )
    df["eta_destination_edited"] = pd.to_datetime(
        df["eta_destination_edited"], errors="coerce"
    )

    return df.reset_index(drop=True)



DBLH_COLUNAS = ("trip_number", "status_agrupado", "eta_scheduled_origin_edited",
                "cpt_realizado", "eta_destino_realizado")


def _dt_dblh(texto):
    texto = str(texto or "").strip()
    return None if texto in ("", "-") else _parse_dt_flex(texto)


@gspread_retry()
def read_realizados(client) -> dict:
    """{TRIP: {"cpt": datetime|None, "chegada": datetime|None}} a partir da aba DBLH.
    Viagem com várias pernas: CPT da primeira perna e chegada da última."""
    ws = client.open_by_key(SOURCE_SPREADSHEET_ID).worksheet(DBLH_WORKSHEET_NAME)
    cab = [_norm_cab(c) for c in ws.row_values(1)]
    faltam = [c for c in DBLH_COLUNAS if _norm_cab(c) not in cab]
    if faltam:
        raise KeyError(f"Aba '{DBLH_WORKSHEET_NAME}' sem as colunas {faltam}.")
    letras = [re.sub(r"\d", "", gspread.utils.rowcol_to_a1(1, cab.index(_norm_cab(c)) + 1))
              for c in DBLH_COLUNAS]
    colunas = ws.batch_get([f"{l}2:{l}" for l in letras])

    def cel(col, i):
        return str(col[i][0]) if i < len(col) and col[i] else ""

    pernas = {}
    for i in range(len(colunas[0])):
        trip, status, eta_origem, cpt, chegada = (cel(col, i) for col in colunas)
        trip = trip.strip().upper()
        if not trip or "CANCEL" in status.upper():
            continue
        pernas.setdefault(trip, []).append((_dt_dblh(eta_origem) or datetime.max, _dt_dblh(cpt), _dt_dblh(chegada)))
    saida = {}
    for trip, lista in pernas.items():
        lista.sort(key=lambda p: p[0])
        saida[trip] = {"cpt": lista[0][1], "chegada": lista[-1][2]}
    return saida


# =============================================================================
# RASTREAMENTO DA SEGURADORA (aba Rastreamento_Angellira)
# =============================================================================
# Saída -> (chave interna no DataFrame, título da coluna em Resultado/Histórico)
TRACKING_OUTPUT = [
    ("trk_status", "Rastreamento"),
    ("trk_defasagem", "Defasagem Rastreamento (min)"),
    ("trk_ignicao", "Ignição"),
    ("trk_cidade_uf", "Cidade/UF"),
    ("trk_posicao", "Posição Atual"),
    ("trk_data_posicao", "Data da Posição"),
    ("trk_min_sem_posicao", "Min. Sem Posição"),
    ("trk_velocidade", "Velocidade (km/h)"),
    ("trk_tempo_parado", "Tempo Parado"),
    ("trk_pct_percorrido", "% Percorrido (Seguradora)"),
    ("trk_ultima_msg", "Última Mensagem"),
    ("trk_encerrada", "Viagem Encerrada (Seguradora)"),
]
# Usadas só internamente (nomes das estações para geocodificar), não vão à planilha.
TRACKING_INTERNO = [("trk_origem_nome", ""), ("trk_destino_nome", "")]
_MSG_ENCERRADA = ("FIMDEVIAGEM", "CHEGADANOCLIENTE", "FIMDEDESCARGA")


def _norm_chave(texto) -> str:
    """'IGNIÇÃO' -> 'IGNICAO'; '% PERCORRIDO' -> 'PCTPERCORRIDO' (sem acento/símbolos)."""
    t = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode().upper()
    return re.sub(r"[^A-Z0-9]", "", t.replace("%", "PCT"))


def _placa(texto) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(texto or "").upper())


def _parse_dt_flex(texto):
    """ISO ('2026-10-12T09:23:48') ou dd/mm/aaaa hh:mm[:ss], como o Sheets exibir."""
    texto = str(texto or "").strip()
    if not texto:
        return None
    try:
        d = datetime.fromisoformat(texto)
        if d.tzinfo is not None:
            d = d.astimezone(ZoneInfo(TIMEZONE)).replace(tzinfo=None)
        return d
    except ValueError:
        pass
    for fmt in ("%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(texto, fmt)
        except ValueError:
            continue
    return None


def _num(texto):
    try:
        return float(str(texto).strip().replace(",", "."))
    except ValueError:
        return None


def _colunas_trk_vazias(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for chave, _titulo in TRACKING_OUTPUT + TRACKING_INTERNO:
        # dtype=object: a coluna recebe texto E números (o pandas novo recusa
        # número em coluna criada como texto).
        df[chave] = pd.Series([""] * len(df), index=df.index, dtype=object)
    return df


def carregar_rastreamento(client):
    """Lê a aba de rastreamento. Devolve (registros_shopee, referencia) ou None
    se a aba não existir / não tiver as colunas esperadas (recurso desligado)."""
    sh = client.open_by_key(TRACKING_SPREADSHEET_ID)
    try:
        ws = sh.worksheet(TRACKING_WORKSHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        logging.info("Aba '%s' não existe; rastreamento da seguradora desligado.", TRACKING_WORKSHEET_NAME)
        return None
    valores = ws.get_all_values()
    if len(valores) < 2:
        logging.info("Aba '%s' vazia; rastreamento sem dados.", TRACKING_WORKSHEET_NAME)
        return [], None
    cab = {}
    for i, c in enumerate(valores[0]):
        if c.strip():
            cab.setdefault(_norm_chave(c), i)
    faltam = [n for n in ("PROCEDIMENTODEEMBARQUE", "VEICULO", "DATAPOSICAO") if n not in cab]
    if faltam:
        logging.warning("Aba '%s' sem as colunas %s (cabeçalhos lidos: %s). Rastreamento desligado.",
                        TRACKING_WORKSHEET_NAME, faltam, list(cab)[:12])
        return None

    def cel(linha, nome):
        i = cab.get(nome)
        return linha[i].strip() if i is not None and i < len(linha) else ""

    registros, referencias = [], []
    for l in valores[1:]:
        dt_pos = _parse_dt_flex(cel(l, "DATAPOSICAO"))
        if dt_pos:
            referencias.append(dt_pos)  # todas as linhas (qualquer procedimento) dão a hora do arquivo
        proc = _norm_chave(cel(l, "PROCEDIMENTODEEMBARQUE"))
        if not all(t in proc for t in TRACKING_PROCEDIMENTO_TERMOS):
            continue
        veiculo = _placa(cel(l, "VEICULO"))
        if not veiculo:
            continue
        registros.append({
            "veiculo": veiculo,
            "placas": {p for p in (veiculo, _placa(cel(l, "REBOQUE1")), _placa(cel(l, "REBOQUE2"))) if p},
            "saida": _parse_dt_flex(cel(l, "PREVISAOSAIDAORIGEM")),
            "dt_pos": dt_pos,
            "ignicao": cel(l, "IGNICAO"),
            "cidade": cel(l, "CIDADEPOSICAO"),
            "uf": cel(l, "ESTADOPOSICAO"),
            "posicao": cel(l, "POSICAO"),
            "velocidade": cel(l, "VELOCIDADE"),
            "tempo_parado": cel(l, "TEMPOPARADO"),
            "pct": cel(l, "PCTPERCORRIDO"),
            "ultima_msg": cel(l, "ULTIMAMENSAGEM"),
            "origem_nome": cel(l, "ORIGEM"),
            "destino_nome": cel(l, "CLIENTE"),
        })
    return registros, (max(referencias) if referencias else None)


def casar_rastreamento(df: pd.DataFrame, registros: list, now: datetime) -> dict:
    """{índice_no_df: registro}. Liga pela PLACA (cavalo ou reboque). Se a placa
    aparecer em mais de uma rota, fica a de Início Previsto mais próximo da
    previsão de saída da seguradora. Cada linha da seguradora vai para 1 rota."""
    indice = {}
    for i, row in df.iterrows():
        for col in ("vehicle_number", "vehicle_number2"):
            p = _placa(row.get(col))
            if p:
                indice.setdefault(p, set()).add(i)

    escolhido = {}  # i_df -> (prioridade, registro)
    for reg in registros:
        candidatos = set()
        for p in reg["placas"]:
            candidatos |= indice.get(p, set())
        if not candidatos:
            continue
        referencia = reg["saida"] or now

        def prioridade(i):
            vn, vn2 = _placa(df.at[i, "vehicle_number"]), _placa(df.at[i, "vehicle_number2"])
            tipo = 0 if reg["veiculo"] == vn else 1 if reg["veiculo"] == vn2 else 2
            ini = df.at[i, "eta_scheduled_origin_edited"]
            dist = abs((ini.to_pydatetime() - referencia).total_seconds()) if pd.notna(ini) else float("inf")
            return (tipo, dist)

        melhor = min(candidatos, key=prioridade)
        pr = prioridade(melhor)
        if melhor not in escolhido or pr < escolhido[melhor][0]:
            escolhido[melhor] = (pr, reg)
    return {i: reg for i, (_pr, reg) in escolhido.items()}


def enriquecer_com_rastreamento(client, df: pd.DataFrame, now: datetime):
    """Acrescenta ao DataFrame as colunas trk_* (ignição, posição etc.).
    Devolve (df, hora do relatório da seguradora ou None).
    Qualquer falha aqui NÃO derruba o ciclo: as colunas saem vazias."""
    df = _colunas_trk_vazias(df)
    try:
        carregado = carregar_rastreamento(client)
        if carregado is None:
            return df, None
        registros, ref = carregado
        defasagem = max(0, round((now - ref).total_seconds() / 60)) if ref else None
        status_base = ("" if defasagem is None else
                       "OK" if defasagem <= TRACKING_MAX_AGE_MIN else "Desatualizado")
        casados = casar_rastreamento(df, registros, now)

        for i in df.index:
            reg = casados.get(i)
            if reg is None:
                df.at[i, "trk_status"] = "Sem correspondência"
                continue
            msg = reg["ultima_msg"]
            msg_limpa = "" if msg.strip() in ("", "0") else msg
            pct = _num(reg["pct"])
            vel = _num(reg["velocidade"])
            df.at[i, "trk_status"] = status_base
            df.at[i, "trk_defasagem"] = defasagem if defasagem is not None else ""
            df.at[i, "trk_ignicao"] = {"1": "Ligada", "2": "Desligada"}.get(reg["ignicao"].strip(), "")
            df.at[i, "trk_cidade_uf"] = "/".join(x for x in (reg["cidade"], reg["uf"]) if x)
            df.at[i, "trk_posicao"] = reg["posicao"]
            df.at[i, "trk_data_posicao"] = reg["dt_pos"].strftime("%d/%m/%Y %H:%M") if reg["dt_pos"] else ""
            # Idade da posição NA HORA DO ARQUIVO (não de agora): com o arquivo carregado à
            # mão, "agora" faria toda posição parecer velha.
            df.at[i, "trk_min_sem_posicao"] = (
                max(0, round((ref - reg["dt_pos"]).total_seconds() / 60)) if ref and reg["dt_pos"] else "")
            df.at[i, "trk_velocidade"] = int(vel) if vel is not None else ""
            df.at[i, "trk_tempo_parado"] = reg["tempo_parado"]
            df.at[i, "trk_pct_percorrido"] = f"{pct:.1f}%" if pct is not None else ""
            df.at[i, "trk_ultima_msg"] = msg_limpa
            df.at[i, "trk_origem_nome"] = reg["origem_nome"]
            df.at[i, "trk_destino_nome"] = reg["destino_nome"]
            df.at[i, "trk_encerrada"] = "Sim" if any(t in _norm_chave(msg) for t in _MSG_ENCERRADA) else "Não"

        logging.info("Rastreamento: %s linha(s) Shopee, %s rota(s) casada(s) por placa, "
                     "%s linha(s) sem rota, defasagem %s min.",
                     len(registros), len(casados), len(registros) - len(casados),
                     defasagem if defasagem is not None else "?")
    except Exception:
        logging.exception("Falha ao ler o rastreamento da seguradora; seguindo sem ele.")
        return _colunas_trk_vazias(df), None
    return df, ref


def _valor(v):
    """Números continuam números; o resto vira texto limpo."""
    if isinstance(v, numbers.Number) and not isinstance(v, bool):
        f = float(v)
        return int(f) if f.is_integer() else f
    return _txt(v)


def classify_status(valor_bruto: str) -> str:
    """Normaliza o valor da coluna Espelhamento para OK / NOK / SEM_DADO."""
    if valor_bruto is None:
        return "SEM_DADO"
    v = str(valor_bruto).strip().upper()
    v = v.replace("Ã", "A").replace("Á", "A")  # tolera variações de acentuação
    if v == "OK":
        return "OK"
    if not v:
        return "SEM_DADO"
    return "NOK"  # qualquer outra coisa (ex.: "Não espelhado") conta como queda


def seconds_to_hhmm(total_seconds: float) -> str:
    if total_seconds is None or total_seconds < 0:
        total_seconds = 0
    total_seconds = int(round(total_seconds))
    horas, resto = divmod(total_seconds, 3600)
    minutos = resto // 60
    return f"{horas}h{minutos:02d}m"


# =============================================================================
# 6. REGRAS DE NEGÓCIO — processamento de cada rota
# =============================================================================

def process_trip(row: pd.Series, state: dict, now: datetime, eventos: list, realizado=None) -> dict:
    """
    Atualiza o estado da rota `row` e devolve um dicionário já pronto para
    virar uma linha da planilha de resultados.

    `eventos` é uma lista (efeito colateral) na qual cada perda/retorno
    detectado nesta execução é registrado, para depois ser gravado na aba
    de histórico/auditoria. `realizado` vem de read_realizados.
    """
    trip_number = str(row["trip_number"]).strip()
    inicio_previsto = row["eta_scheduled_origin_edited"]
    fim_previsto = row["eta_destination_edited"]
    status_atual = classify_status(row["espelhamento"])

    dados = state.setdefault(trip_number, {
        "last_status": None,
        "last_seen": None,
        "sla_check_done": False,
        "sla_result": None,
        "sla_checked_at": None,
        "current_loss_start": None,
        "loss_periods": [],
        "total_loss_seconds": 0,
    })

    status_anterior = dados["last_status"]

    # Confirmação da mudança: evita quedas falsas quando a Telemetria "pisca"
    # (várias rotas trocando de status no mesmo segundo durante a atualização do
    # DBLH). Confirmada, a mudança é datada na PRIMEIRA leitura, então a duração
    # das quedas continua correta.
    t_mudanca = now
    if status_anterior is not None and status_atual != status_anterior:
        if dados.get("pendente_status") == status_atual:
            dados["pendente_leituras"] = dados.get("pendente_leituras", 1) + 1
        else:
            dados["pendente_status"], dados["pendente_leituras"] = status_atual, 1
            dados["pendente_desde"] = now.isoformat()
        if dados["pendente_leituras"] >= CONFIRMACAO_LEITURAS:
            t_mudanca = _parse_iso(dados.get("pendente_desde")) or now
            dados["pendente_status"] = None
        else:
            status_atual = status_anterior
    else:
        dados["pendente_status"] = None

    # Dados do veículo/rota, repassados também para o histórico de quedas.
    info_veiculo = {titulo: _txt(row.get(chave))
                    for chave, (_n, _l, titulo) in OPTIONAL_COLUMNS.items()}

    # --- Regra 3: detecção de queda / retorno de sinal ----------------------
    # O estado interno (current_loss_start / loss_periods) registra TODA queda,
    # inclusive a da janela pré-início, porque o % de espelhamento usa essa
    # janela. Já o HISTÓRICO (eventos) só recebe quedas a partir do início
    # previsto da viagem:
    #  - queda que começa e termina antes do início: não vai ao histórico;
    #  - queda que começa antes e continua após o início: PERDA é registrada no
    #    primeiro ciclo depois do início, e a duração do RETORNO conta só a
    #    partir do início;
    #  - queda que começa após o início: registrada normalmente.
    if "loss_logged" not in dados:  # estado antigo: queda em aberto já estava no histórico
        dados["loss_logged"] = bool(dados.get("current_loss_start"))

    viagem_iniciada = bool(pd.isna(inicio_previsto)) or now >= inicio_previsto

    def _registrar_evento(tipo: str, duracao):
        eventos.append({
            "timestamp_deteccao": now.isoformat(),
            "trip_number": trip_number,
            "evento": tipo,
            "duracao_segundos": duracao,
            **info_veiculo,
        })

    # Guiado pela queda em aberto, não pelo status anterior: com uma leitura vazia
    # (SEM_DADO) no meio, NOK -> SEM_DADO -> OK precisa fechar a queda.
    if status_atual == "NOK" and not dados.get("current_loss_start"):
        # Início de uma nova queda (ou rota já "nasceu" caída na 1ª leitura).
        dados["current_loss_start"] = t_mudanca.isoformat()
        dados["loss_logged"] = False
        logging.info("[%s] Perda de sinal confirmada (desde %s).", trip_number, t_mudanca)

    elif status_atual == "OK" and dados.get("current_loss_start"):
        # Fim de uma queda em aberto.
        inicio_queda = _parse_iso(dados.get("current_loss_start")) or t_mudanca
        duracao = (t_mudanca - inicio_queda).total_seconds()
        duracao = max(duracao, 0)
        dados["loss_periods"].append({
            "perda": inicio_queda.isoformat(),
            "retorno": t_mudanca.isoformat(),
            "duracao_segundos": duracao,
        })
        dados["total_loss_seconds"] += duracao
        dados["current_loss_start"] = None
        if dados["loss_logged"]:
            # Duração no histórico: só a parte após o início da viagem.
            inicio_hist = inicio_queda
            if pd.notna(inicio_previsto):
                inicio_hist = max(inicio_queda, inicio_previsto.to_pydatetime())
            _registrar_evento("RETORNO", max((t_mudanca - inicio_hist).total_seconds(), 0))
        dados["loss_logged"] = False
        logging.info(
            "[%s] Sinal restabelecido às %s (ficou %s sem sinal).",
            trip_number, t_mudanca, seconds_to_hhmm(duracao),
        )

    # Queda em aberto e viagem já iniciada, ainda não enviada ao histórico.
    if (status_atual == "NOK" and dados.get("current_loss_start")
            and not dados["loss_logged"] and viagem_iniciada):
        _registrar_evento("PERDA", "")
        dados["loss_logged"] = True

    dados["last_status"] = status_atual
    dados["last_seen"] = now.isoformat()

    # --- Regra 2: SLA de início (1h30) ---------------------------------------
    # Como o robô só enxerga "fotos" periódicas da planilha (a cada
    # CHECK_INTERVAL_MINUTES), a forma robusta de avaliar "o espelhamento já
    # estava OK com pelo menos 1h30 de antecedência" é: assim que o horário
    # atual atingir (início_previsto - 1h30), registrar QUAL ERA o status
    # naquele exato instante de leitura. Isso é feito uma única vez por rota
    # (sla_check_done) para não ser sobrescrito por leituras futuras.
    if not dados["sla_check_done"] and pd.notna(inicio_previsto):
        limite_sla = inicio_previsto - timedelta(minutes=SLA_MINUTES_BEFORE_START)
        if now >= limite_sla:
            dados["sla_result"] = "Sim" if status_atual == "OK" else "Não"
            dados["sla_check_done"] = True
            dados["sla_checked_at"] = now.isoformat()
            logging.info(
                "[%s] Checagem de SLA (1h30 antes do início) = %s.",
                trip_number, dados["sla_result"],
            )

    dados["base"] = {
        "inicio": _iso(inicio_previsto),
        "fim": _iso(fim_previsto),
        "info": info_veiculo,
    }
    atualizar_realizado(dados, realizado)

    resultado = montar_resultado(trip_number, dados, status_atual, corte=now, now=now)
    # Rastreamento da seguradora (vazio se a aba Rastreamento_Angellira não existir).
    resultado.update({titulo: _valor(row.get(chave)) for chave, titulo in TRACKING_OUTPUT})
    return resultado


def _iso(value):
    if value is None or pd.isna(value):
        return None
    return pd.Timestamp(value).to_pydatetime().isoformat()


def atualizar_realizado(dados: dict, realizado) -> None:
    """Guarda no estado o CPT e a chegada realizados (sobrevivem a uma falha de
    leitura da DBLH e à saída da rota da aba Telemetria)."""
    if not realizado:
        return
    for chave, campo in (("cpt", "cpt_realizado"), ("chegada", "chegada_realizada")):
        if realizado.get(chave):
            dados[campo] = realizado[chave].isoformat()


def janela_shopee(inicio_previsto, fim_previsto, cpt, chegada, now):
    """Do que vier primeiro entre ETA Origem programado e CPT realizado até o ETA
    Destino realizado. Sem chegada, o fim é provisório: o fim previsto, ou agora
    se a viagem já passou dele."""
    inicio = min(inicio_previsto, cpt) if cpt else inicio_previsto
    return inicio, chegada or max(fim_previsto, now)


def montar_resultado(trip_number: str, dados: dict, status_atual: str, corte: datetime, now: datetime) -> dict:
    """Linha de Resultado_Espelhamento a partir do estado da rota. `corte` é até
    quando o sinal é conhecido: agora, ou a última leitura de uma rota que já
    saiu da aba Telemetria."""
    base = dados.get("base") or {}
    inicio_previsto, fim_previsto = _parse_iso(base.get("inicio")), _parse_iso(base.get("fim"))
    cpt, chegada = _parse_iso(dados.get("cpt_realizado")), _parse_iso(dados.get("chegada_realizada"))

    # --- Regra 4: % de espelhamento (regra da Shopee) ------------------------
    # Só a parte de cada queda que cai DENTRO da janela entra na conta. A queda
    # em aberto é contada até min(corte, fim da janela).
    inicio_janela = None
    duracao_total_rota = None
    duracao_janela = None
    tempo_sem_sinal_total = dados["total_loss_seconds"]  # bruto, sem recorte
    percentual_espelhamento = None

    if inicio_previsto and fim_previsto:
        inicio_janela, fim_janela = janela_shopee(inicio_previsto, fim_previsto, cpt, chegada, now)
        duracao_total_rota = (fim_previsto - inicio_previsto).total_seconds()
        duracao_janela = (fim_janela - inicio_janela).total_seconds()

        def _overlap(ini, fim):
            """Segundos de [ini, fim] que caem dentro da janela."""
            if ini is None or fim is None:
                return 0.0
            return max(0.0, (min(fim, fim_janela) - max(ini, inicio_janela)).total_seconds())

        sem_sinal_janela = 0.0
        for per in dados["loss_periods"]:
            sem_sinal_janela += _overlap(_parse_iso(per["perda"]), _parse_iso(per["retorno"]))
        if dados["current_loss_start"]:
            sem_sinal_janela += _overlap(_parse_iso(dados["current_loss_start"]), corte)

        tempo_sem_sinal_total = sem_sinal_janela
        if duracao_janela > 0:
            percentual_espelhamento = max(
                0.0, min(100.0, 100.0 * (1 - sem_sinal_janela / duracao_janela))
            )

    # --- Quedas contabilizadas: Qtd., Última Perda e Último Retorno ----------
    # Só contam a partir do início da janela. Queda que termina antes disso é
    # ignorada; queda que atravessa o início entra "cortada" no início.
    quedas = []  # (perda, retorno | None), em ordem cronológica
    for per in dados["loss_periods"]:
        perda, retorno = _parse_iso(per["perda"]), _parse_iso(per["retorno"])
        if perda is None or retorno is None:
            continue
        if inicio_janela is not None:
            if retorno <= inicio_janela:
                continue
            perda = max(perda, inicio_janela)
        quedas.append((perda, retorno))
    if dados["current_loss_start"]:
        perda_aberta = _parse_iso(dados["current_loss_start"])
        if perda_aberta is not None and (inicio_janela is None or corte > inicio_janela):
            if inicio_janela is not None:
                perda_aberta = max(perda_aberta, inicio_janela)
            quedas.append((perda_aberta, None))
    retornos = [r for _p, r in quedas if r is not None]
    ultima_perda = quedas[-1][0] if quedas else None
    ultimo_retorno = retornos[-1] if retornos else None

    # --- Status geral ---------------------------------------------------------
    # Regra da Shopee: OK só se cumpriu as duas etapas (espelhado 1h30 antes do
    # ETA Origem e pelo menos 95% da janela). Antes da chegada realizada o % é
    # provisório.
    if not dados["sla_check_done"]:
        status_geral = "Pendente"
    elif dados["sla_result"] == "Não":
        status_geral = "NOK"
    elif percentual_espelhamento is not None and percentual_espelhamento < META_PERCENTUAL_ESPELHAMENTO:
        status_geral = "NOK"
    elif percentual_espelhamento is None:
        status_geral = "Pendente"
    else:
        status_geral = "OK"

    return {
        "Trip Number": trip_number,
        "Início Previsto": _fmt(inicio_previsto),
        "Fim Previsto": _fmt(fim_previsto),
        "Início da Contagem": _fmt(inicio_janela),
        "Espelhamento Atual": status_atual,
        "SLA 1h30 Cumprido": dados["sla_result"] or "Pendente",
        "SLA Verificado Em": _fmt(_parse_iso(dados["sla_checked_at"])),
        "Duração Total da Rota": seconds_to_hhmm(duracao_total_rota) if duracao_total_rota else "",
        "Duração da Janela": seconds_to_hhmm(duracao_janela) if duracao_janela else "",
        "Tempo Sem Sinal": seconds_to_hhmm(tempo_sem_sinal_total),
        "% Espelhamento": f"{percentual_espelhamento:.1f}%" if percentual_espelhamento is not None else "",
        "Qtd. Quedas de Sinal": len(quedas),
        "Última Perda": _fmt(ultima_perda),
        "Último Retorno": _fmt(ultimo_retorno),
        "Status Geral": status_geral,
        "Atualizado Em": _fmt(now),
        # Colunas informativas, ao final para não deslocar as letras das colunas existentes.
        **base.get("info", {}),
    }


def colunas_realizadas(resultado: dict, dados: dict) -> None:
    """Últimas colunas de Resultado_Espelhamento (depois de Status Viagem)."""
    resultado["CPT Realizado"] = _fmt(_parse_iso(dados.get("cpt_realizado")))
    resultado["Chegada Realizada"] = _fmt(_parse_iso(dados.get("chegada_realizada")))
    resultado["Janela Final"] = "Sim" if dados.get("chegada_realizada") else "Não"


def resultados_finalizados(state: dict, ativos: set, realizados, now: datetime) -> list:
    """Rotas que já saíram da aba Telemetria mas chegaram há menos de
    FINALIZADAS_HORAS: resultado com a janela fechada na chegada realizada."""
    saida = []
    for trip_number, dados in state.items():
        if trip_number in ativos or not dados.get("base") or dados.get("cancelada"):
            continue
        atualizar_realizado(dados, (realizados or {}).get(trip_number.upper()))
        chegada = _parse_iso(dados.get("chegada_realizada"))
        if chegada is None or now - chegada > timedelta(hours=FINALIZADAS_HORAS):
            continue
        corte = _parse_iso(dados.get("last_seen")) or now
        r = montar_resultado(trip_number, dados, dados.get("last_status") or "", corte=corte, now=now)
        r["Status Viagem"] = "FINALIZADA"
        colunas_realizadas(r, dados)
        saida.append(r)
    return saida


def _txt(value) -> str:
    """Texto limpo para colunas informativas (NaN/None -> vazio)."""
    if value is None or (not isinstance(value, str) and pd.isna(value)):
        return ""
    return str(value).strip()


def _fmt(value) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    if pd.isna(value):
        return ""
    if hasattr(value, "strftime"):
        return value.strftime("%d/%m/%Y %H:%M")
    return str(value)


# =============================================================================
# 7. ESCRITA NA PLANILHA DE RESULTADOS
# =============================================================================

@gspread_retry()
def get_or_create_worksheet(spreadsheet, nome_aba: str, linhas=1000, colunas=20):
    try:
        return spreadsheet.worksheet(nome_aba)
    except WorksheetNotFound:
        logging.info("Aba '%s' não existe. Criando...", nome_aba)
        return spreadsheet.add_worksheet(title=nome_aba, rows=linhas, cols=colunas)


def garantir_colunas(ws, minimo: int) -> None:
    """Aumenta a grade da aba se ela tiver menos colunas do que o necessário
    (abas criadas pelo robô nascem com 20 colunas)."""
    if ws.col_count < minimo:
        ws.add_cols(minimo - ws.col_count)


@gspread_retry()
def write_results(client: gspread.Client, resultados: list) -> None:
    """Sobrescreve a aba de resultados com o snapshot mais recente de todas
    as rotas ativas/monitoradas. Como o volume de rotas é pequeno (dezenas),
    reescrever a aba inteira a cada ciclo é simples e evita inconsistência
    entre linhas antigas e novas."""
    spreadsheet = client.open_by_key(RESULTS_SPREADSHEET_ID)
    ws = get_or_create_worksheet(spreadsheet, RESULTS_WORKSHEET_NAME)

    if not resultados:
        ws.clear()
        return

    cabecalho = list(resultados[0].keys())
    garantir_colunas(ws, len(cabecalho))
    linhas = [cabecalho] + [[linha.get(c, "") for c in cabecalho] for linha in resultados]

    ws.clear()
    ws.update(values=linhas, range_name="A1")


def _preparar_cabecalho(ws, cabecalho: list) -> None:
    garantir_colunas(ws, len(cabecalho))
    atual = ws.row_values(1)
    if not atual:
        ws.append_row(cabecalho, value_input_option="USER_ENTERED")
    elif atual != cabecalho:
        if cabecalho[:len(atual)] == atual:
            # Cabeçalho antigo (menos colunas): completa. Linhas antigas ficam
            # com as colunas novas em branco.
            ws.update(values=[cabecalho], range_name="A1")
        else:
            logging.warning("Cabeçalho da aba '%s' diferente do esperado; não foi alterado. "
                            "Esperado: %s", ws.title, cabecalho)


def _texto(v):
    # Apóstrofo força texto no USER_ENTERED: a linha fica idêntica ao que está
    # em Resultado_Espelhamento (sem converter datas, % ou IDs).
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return v  # números (ex.: Qtd. Quedas) continuam numéricos
    return ("'" + str(v)) if v not in ("", None) else ""


@gspread_retry()
def append_events(client: gspread.Client, eventos: list) -> None:
    """Acrescenta ao final da aba de histórico os eventos de perda/retorno
    detectados nesta execução (não sobrescreve — é um log de auditoria)."""
    if not eventos:
        return
    spreadsheet = client.open_by_key(RESULTS_SPREADSHEET_ID)
    ws = get_or_create_worksheet(spreadsheet, EVENTS_WORKSHEET_NAME)

    # Colunas do histórico:
    #   1) as 4 do evento; 2) as 5 informativas (mesma posição de antes, para as
    #   linhas já gravadas continuarem alinhadas); 3) TODAS as demais colunas da
    #   aba Resultado_Espelhamento, na ordem em que ela as grava.
    titulos_extra = [titulo for (_n, _l, titulo) in OPTIONAL_COLUMNS.values()]
    modelo = next((ev["resultado"] for ev in eventos if ev.get("resultado")), {})
    colunas_resultado = [c for c in modelo
                         if c != "Trip Number" and c not in titulos_extra]
    cabecalho = (["Data/Hora Detecção", "Trip Number", "Evento", "Duração da Queda"]
                 + titulos_extra + colunas_resultado)
    _preparar_cabecalho(ws, cabecalho)

    linhas = []
    for ev in eventos:
        duracao = ev["duracao_segundos"]
        duracao_fmt = seconds_to_hhmm(duracao) if duracao != "" else ""
        snap = ev.get("resultado") or {}
        linhas.append([
            _fmt(_parse_iso(ev["timestamp_deteccao"])),
            ev["trip_number"],
            ev["evento"],
            duracao_fmt,
            *[_texto(ev.get(titulo)) for titulo in titulos_extra],
            *[_texto(snap.get(c, "")) for c in colunas_resultado],
        ])
    ws.append_rows(linhas, value_input_option="USER_ENTERED")


def _fechar(dados: dict, resultado: dict, now: datetime, motivo: str):
    """Marca a rota como fechada. Cancelada fecha sem linha (não pontua)."""
    dados["fechada_em"] = now.isoformat()
    dados.pop("ultimo_resultado", None)
    if resultado.get("Cancelamento"):
        dados["cancelada"] = resultado["Cancelamento"]
        return None
    pct = _num(str(resultado.get("% Espelhamento", "")).replace("%", ""))
    return {"Fechada Em": _fmt(now), "Motivo": motivo,
            "% Final": round(pct, 1) if pct is not None else "", **resultado}


def fechar_rotas(state: dict, resultados: list, finalizados: list, now: datetime) -> list:
    """Retrato final de cada rota, uma única vez: depois da chegada realizada
    (quando a janela da Shopee fecha e o % para de mudar) ou, se ela sumir da
    Telemetria antes, com o último retrato (fechado na chegada, se já houver)."""
    # Espera as leituras de confirmação: uma queda ainda pendente na chegada
    # é datada na 1ª leitura e mudaria o % depois de gravado.
    espera = timedelta(minutes=CHECK_INTERVAL_MINUTES * CONFIRMACAO_LEITURAS)
    fechadas, ativos = [], set()
    for r in resultados:
        trip = r["Trip Number"]
        ativos.add(trip)
        dados = state.get(trip)
        if dados is None or dados.get("fechada_em"):
            continue
        chegada = _parse_iso(dados.get("chegada_realizada"))
        if chegada and now >= chegada + espera:
            fechadas.append(_fechar(dados, r, now, "Chegada realizada"))
        else:
            dados["ultimo_resultado"] = r
    finais = {r["Trip Number"]: r for r in finalizados}
    limite = now - timedelta(minutes=FECHAMENTO_SUMIDA_MIN)
    for trip, dados in state.items():
        visto = _parse_iso(dados.get("last_seen"))
        if (trip not in ativos and not dados.get("fechada_em") and dados.get("ultimo_resultado")
                and visto and visto < limite):
            fechadas.append(_fechar(dados, finais.get(trip) or dados["ultimo_resultado"], now, "Saiu da Telemetria"))
    return [f for f in fechadas if f]


@gspread_retry()
def append_fechamentos(client: gspread.Client, fechadas: list) -> None:
    if not fechadas:
        return
    ws = get_or_create_worksheet(client.open_by_key(RESULTS_SPREADSHEET_ID), FECHAMENTO_WORKSHEET_NAME)
    cabecalho = list(fechadas[0].keys())
    for f in fechadas[1:]:
        cabecalho += [c for c in f if c not in cabecalho]
    _preparar_cabecalho(ws, cabecalho)
    ws.append_rows([[_texto(f.get(c, "")) for c in cabecalho] for f in fechadas],
                   value_input_option="USER_ENTERED")


def _letra_coluna(indice: int) -> str:
    letra, n = "", indice + 1
    while n:
        n, r = divmod(n - 1, 26)
        letra = chr(65 + r) + letra
    return letra


def carregar_cancelamentos(client) -> dict:
    """{trip: 'ABA: STATUS'} das rotas canceladas segundo CANCELAMENTO_FONTES.
    Rota repetida numa aba só conta como cancelada se todas as linhas com status
    forem de cancelamento. Falha aqui nunca derruba o ciclo: segue sem cancelamentos."""
    try:
        planilha = client.open_by_key(SOURCE_SPREADSHEET_ID)
        abas = {ws.title for ws in planilha.worksheets()}
        fontes = [f for f in CANCELAMENTO_FONTES if f[0] in abas]
        for aba in {f[0] for f in CANCELAMENTO_FONTES} - abas:
            logging.warning("Aba de cancelamento '%s' não encontrada; ignorada.", aba)
        if not fontes:
            return {}
        cabs = planilha.values_batch_get([f"'{aba}'!1:1" for aba, _t, _s in fontes])["valueRanges"]
        colunas = []
        for (aba, nomes_trip, nomes_status), vr in zip(fontes, cabs):
            cab = {_norm_cab(c): i for i, c in enumerate((vr.get("values") or [[]])[0])}
            it = next((cab[_norm_cab(n)] for n in nomes_trip if _norm_cab(n) in cab), None)
            ist = next((cab[_norm_cab(n)] for n in nomes_status if _norm_cab(n) in cab), None)
            if it is None or ist is None:
                logging.warning("Aba '%s' sem as colunas de trip/status; cancelamentos dela ignorados.", aba)
                continue
            colunas.append((aba, it, ist))
        if not colunas:
            return {}
        ranges = []
        for aba, it, ist in colunas:
            ranges += [f"'{aba}'!{_letra_coluna(it)}2:{_letra_coluna(it)}",
                       f"'{aba}'!{_letra_coluna(ist)}2:{_letra_coluna(ist)}"]
        dados = planilha.values_batch_get(ranges)["valueRanges"]
        canceladas = {}
        for n, (aba, _it, _ist) in enumerate(colunas):
            trips = dados[2 * n].get("values", [])
            status = dados[2 * n + 1].get("values", [])
            por_trip = {}
            for i, linha in enumerate(trips):
                trip = (linha[0] if linha else "").strip()
                st = (status[i][0] if i < len(status) and status[i] else "").strip()
                if trip and st:
                    por_trip.setdefault(trip, []).append(st)
            for trip, sts in por_trip.items():
                if all(_norm_chave(st) in STATUS_CANCELAMENTO for st in sts):
                    canceladas.setdefault(trip, f"{aba}: {sts[-1].upper()}")
        return canceladas
    except Exception:
        logging.exception("Falha ao ler os cancelamentos; seguindo sem eles.")
        return {}


# =============================================================================
# 7b. MAPA / TOMTOM E DADOS DO SITE
# =============================================================================

GEO = geo_tomtom.GeoTomTom()
INMET_ATIVO = os.getenv("INMET_ATIVO", "1") != "0"
CLIMA = clima.AlertasInmet()
_SNAPSHOT = {}  # lido pelo site; trocado inteiro a cada ciclo (atribuição é atômica)


def enriquecer_geo(client, df: pd.DataFrame, resultados: list, now: datetime) -> dict:
    """Coordenadas das estações, posição do veículo e previsão TomTom.
    Acrescenta 3 colunas aos resultados. Nunca derruba o ciclo."""
    if not GEO.key:
        logging.warning("TOMTOM_API_KEY não definida neste serviço: mapa e previsão desligados.")
        for r in resultados:
            r.update({"Previsão TomTom": "", "Desvio vs Fim Previsto (min)": "", "Risco de Atraso": ""})
        return {}
    try:
        planilha = client.open_by_key(RESULTS_SPREADSHEET_ID)
        geo_tomtom.carregar_geocache(GEO, planilha)
        nomes = {}
        for _, row in df.iterrows():
            for col_cod, col_nome in (("origin_station_code", "trk_origem_nome"),
                                      ("destination_station_code", "trk_destino_nome")):
                cod = _txt(row.get(col_cod)).upper()
                if cod and not nomes.get(cod):
                    nomes[cod] = _txt(row.get(col_nome))
        coords = geo_tomtom.sincronizar_estacoes(GEO, planilha, nomes)
        geo_por_trip = geo_tomtom.calcular_geo(GEO, resultados, coords, now)
        geo_tomtom.salvar_geocache(GEO, planilha)
        logging.info("TomTom: %s chamada(s) hoje, %s estação(ões) com coordenadas.",
                     GEO.chamadas_hoje, len(coords))
        return geo_por_trip
    except Exception:
        logging.exception("Falha na etapa de mapa/TomTom; seguindo sem ela.")
        for r in resultados:
            for c in ("Previsão TomTom", "Desvio vs Fim Previsto (min)", "Risco de Atraso"):
                r.setdefault(c, "")
        return {}


TRILHA_MAX_PONTOS = int(os.getenv("TRILHA_MAX_PONTOS", "300"))


def viagem_iniciada(r: dict, dados: dict, now: datetime) -> bool:
    if dados.get("cpt_realizado"):
        return True
    status = _norm_chave(r.get("Status Viagem", ""))
    if status:
        return status != "PREVIAGEM"
    inicio = _parse_dt_flex(r.get("Início Previsto"))
    return inicio is not None and now >= inicio


def atualizar_trilhas(state: dict, resultados: list, geo_por_trip: dict, now: datetime) -> None:
    """Guarda no estado cada posição nova da Angellira (já no mapa) a partir do
    início da viagem e entrega a trilha ao site: [lat, lon, desde, até, precisão].
    Posição repetida só estende o 'até' (veículo parado)."""
    for r in resultados:
        trip = r["Trip Number"]
        dados, g = state.get(trip), geo_por_trip.get(trip)
        if dados is None or g is None:
            continue
        trilha = dados.setdefault("trilha", [])
        quando = _parse_dt_flex(r.get("Data da Posição"))
        v = g.get("veiculo")
        if v and quando and viagem_iniciada(r, dados, now):
            texto = _fmt(quando)
            ultimo = trilha[-1] if trilha else None
            ate_ultimo = _parse_dt_flex(ultimo[3]) if ultimo else None
            if ultimo and ultimo[:2] == list(v[:2]):
                if ate_ultimo is None or quando > ate_ultimo:
                    ultimo[3] = texto
            elif ate_ultimo is None or quando > ate_ultimo:
                trilha.append([v[0], v[1], texto, texto, g.get("precisao", "")])
                del trilha[:-TRILHA_MAX_PONTOS]
        g["trilha"] = [list(p) for p in trilha]  # cópia: o site serializa enquanto o próximo ciclo roda


def publicar_snapshot(resultados: list, geo_por_trip: dict, now: datetime, ref_rastreamento=None) -> None:
    global _SNAPSHOT
    _SNAPSHOT = {
        "atualizado_em": _fmt(now),
        "angellira_em": _fmt(ref_rastreamento),
        "resultados": resultados,
        "geo": geo_por_trip,
        "clima": CLIMA.para_mapa() if INMET_ATIVO else [],
        "clima_erro": CLIMA.ultimo_erro if INMET_ATIVO else "",
        "tomtom": {"chamadas_hoje": GEO.chamadas_hoje, "ultimo_erro": GEO.ultimo_erro},
    }


# =============================================================================
# 8. CICLO PRINCIPAL
# =============================================================================

def job():
    now = now_local()
    logging.info("========== Iniciando ciclo de verificação (%s) ==========", now)
    try:
        client = get_gspread_client()
        df = read_source_dataframe(client)

        if df.empty:
            logging.warning("Aba de origem retornou vazia nesta execução.")
            return

        df, ref_rastreamento = enriquecer_com_rastreamento(client, df, now)

        try:
            realizados = read_realizados(client)
        except Exception:
            # Sem a DBLH o ciclo segue com os realizados já guardados no estado.
            logging.exception("Falha ao ler CPT/chegada realizados da aba %s.", DBLH_WORKSHEET_NAME)
            realizados = None

        state = load_state(client)
        eventos_do_ciclo = []
        resultados = []

        for _, row in df.iterrows():
            n_antes = len(eventos_do_ciclo)
            realizado = (realizados or {}).get(str(row["trip_number"]).strip().upper())
            resultado_linha = process_trip(row, state, now, eventos_do_ciclo, realizado)
            resultados.append(resultado_linha)
            # Cada evento do histórico leva o "retrato" completo da linha de
            # Resultado_Espelhamento no momento em que o evento foi detectado.
            for ev in eventos_do_ciclo[n_antes:]:
                ev["resultado"] = resultado_linha

        if not _SNAPSHOT:
            # Logo após reiniciar, o site já mostra as rotas enquanto a TomTom
            # recalcula (a primeira passada pode levar mais de um minuto).
            # Cópias: a etapa TomTom altera os dicts enquanto o site os serializa.
            publicar_snapshot([dict(r) for r in resultados], {}, now, ref_rastreamento)
        geo_por_trip = enriquecer_geo(client, df, resultados, now)
        # Depois das colunas TomTom, para ser a última e não deslocar as existentes.
        for r, status_viagem in zip(resultados, df["status_viagem"]):
            r["Status Viagem"] = _txt(status_viagem)
            colunas_realizadas(r, state.get(r["Trip Number"], {}))
        atualizar_trilhas(state, resultados, geo_por_trip, now)

        cancelamentos = carregar_cancelamentos(client)
        for r in resultados:
            r["Cancelamento"] = cancelamentos.get(r["Trip Number"], "")
            if r["Cancelamento"]:
                r["Status Geral"] = "Cancelada"
        eventos_do_ciclo = [ev for ev in eventos_do_ciclo if not cancelamentos.get(ev["trip_number"])]
        # Por último, para a coluna nova não deslocar as existentes.
        if INMET_ATIVO:
            try:
                CLIMA.atualizar(now)
                clima.aplicar_nas_rotas(CLIMA, resultados, geo_por_trip,
                                        lambda r: viagem_iniciada(r, state.get(r["Trip Number"], {}), now))
            except Exception:
                logging.exception("Falha na etapa de clima (INMET); seguindo sem ela.")
                for r in resultados:
                    r.setdefault("Alerta Chuva (INMET)", "")

        # Depois da etapa TomTom: o retrato final e o guardado no estado saem completos.
        trip_numbers_ativos = set(df["trip_number"].astype(str).str.strip())
        finalizados = resultados_finalizados(state, trip_numbers_ativos, realizados, now)
        fechadas = fechar_rotas(state, resultados, finalizados, now)
        cleanup_state(state, trip_numbers_ativos)
        save_state(state, client)

        client_escrita = client  # mesma sessão autenticada
        # Finalizadas só vão para a planilha (o BSC lê de lá); o site e a TomTom
        # ficam com as rotas ativas.
        write_results(client_escrita, resultados + finalizados)
        append_events(client_escrita, eventos_do_ciclo)
        append_fechamentos(client_escrita, fechadas)

        publicar_snapshot(resultados, geo_por_trip, now, ref_rastreamento)

        logging.info(
            "Ciclo concluído: %s rota(s) processada(s), %s finalizada(s), %s evento(s) de sinal registrado(s), "
            "%s rota(s) fechada(s), %s cancelada(s).",
            len(resultados), len(finalizados), len(eventos_do_ciclo), len(fechadas),
            sum(1 for r in resultados if r["Cancelamento"]),
        )

    except Exception:
        # Nunca deixa uma exceção matar o loop principal — apenas loga e
        # tenta de novo no próximo ciclo agendado.
        logging.exception("Erro inesperado durante o ciclo de verificação.")


# =============================================================================
# 9. SITE (exigido também pelo Web Service do Render) + KEEP-ALIVE
# =============================================================================

def start_http_server():
    """Site + /health no mesmo processo (Web Service do Render)."""
    from waitress import serve
    from site_web import create_app

    app = create_app(lambda: _SNAPSHOT, get_gspread_client, RESULTS_SPREADSHEET_ID, GEO.key)
    threading.Thread(target=serve, args=(app,),
                     kwargs={"host": "0.0.0.0", "port": HTTP_PORT, "threads": 16},
                     daemon=True).start()
    logging.info("Site ouvindo na porta %s.", HTTP_PORT)


def self_ping():
    if not SELF_PING_URL:
        return
    try:
        urllib.request.urlopen(SELF_PING_URL.rstrip("/") + "/health", timeout=15).read()
    except Exception as e:
        logging.warning("Auto-ping falhou (%s).", e)


def main():
    setup_logging()
    start_http_server()
    logging.info(
        "Robô de monitoramento de espelhamento iniciado. Intervalo: %s min. "
        "Fuso: %s. Estado: %s",
        CHECK_INTERVAL_MINUTES, TIMEZONE,
        f"aba '{STATE_WORKSHEET_NAME}'" if STATE_BACKEND == "sheets" else STATE_FILE,
    )
    logging.info("TomTom: %s.", f"chave carregada (termina em ...{GEO.key[-4:]})" if GEO.key else "SEM CHAVE")

    job()  # executa uma vez imediatamente ao subir o robô
    schedule.every(CHECK_INTERVAL_MINUTES).minutes.do(job)
    if SELF_PING_URL:
        schedule.every(SELF_PING_MINUTES).minutes.do(self_ping)

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
