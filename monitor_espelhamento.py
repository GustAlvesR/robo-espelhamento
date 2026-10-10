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
     espelhada, com base na duração prevista da rota (fim - início) e no
     tempo total acumulado em "Não espelhado".
  4. Escreve o resultado consolidado em uma aba de resultados e registra
     cada evento de perda/retorno em uma aba de histórico (auditoria).

Autor: gerado para uso interno (Orbyte Tech / operação CEVA-Shopee)
"""

import json
import logging
import os
import time
import functools
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import gspread
import schedule
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError, WorksheetNotFound


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

# Intervalo de execução do robô.
CHECK_INTERVAL_MINUTES = int(os.getenv("CHECK_INTERVAL_MINUTES", "5"))

# Regra de negócio: SLA de início — quantos minutos antes do início previsto
# o espelhamento já deveria estar "OK".
SLA_MINUTES_BEFORE_START = int(os.getenv("SLA_MINUTES_BEFORE_START", "90"))  # 1h30

# Janela de contagem do % de espelhamento: começa X minutos ANTES do início
# previsto e termina no fim previsto. 95 min = 1h35.
JANELA_MINUTES_BEFORE_START = int(os.getenv("JANELA_MINUTES_BEFORE_START", "95"))

# Meta de % de espelhamento durante a rota (usada apenas para compor a coluna
# extra "status_geral" — Sim/Não do SLA continua sendo a regra 2 pedida).
META_PERCENTUAL_ESPELHAMENTO = float(os.getenv("META_PERCENTUAL_ESPELHAMENTO", "95.0"))

# Havendo rotas antigas que já terminaram e sumiram da planilha de origem,
# depois de quantos dias podemos apagar o estado delas do JSON local.
STATE_RETENTION_DAYS = int(os.getenv("STATE_RETENTION_DAYS", "5"))

# Arquivo de estado (memória do robô entre execuções).
# No Render, aponte para o Persistent Disk (ex.: /data/state_espelhamento.json).
STATE_FILE = os.getenv("STATE_FILE", "state_espelhamento.json")

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

def process_trip(row: pd.Series, state: dict, now: datetime, eventos: list) -> dict:
    """
    Atualiza o estado da rota `row` e devolve um dicionário já pronto para
    virar uma linha da planilha de resultados.

    `eventos` é uma lista (efeito colateral) na qual cada perda/retorno
    detectado nesta execução é registrado, para depois ser gravado na aba
    de histórico/auditoria.
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

    if status_atual == "NOK" and status_anterior != "NOK":
        # Início de uma nova queda (ou rota já "nasceu" caída na 1ª leitura).
        dados["current_loss_start"] = now.isoformat()
        dados["loss_logged"] = False
        logging.info("[%s] Perda de sinal detectada às %s.", trip_number, now)

    elif status_atual == "OK" and status_anterior == "NOK":
        # Fim de uma queda em aberto.
        inicio_queda = _parse_iso(dados.get("current_loss_start")) or now
        duracao = (now - inicio_queda).total_seconds()
        duracao = max(duracao, 0)
        dados["loss_periods"].append({
            "perda": inicio_queda.isoformat(),
            "retorno": now.isoformat(),
            "duracao_segundos": duracao,
        })
        dados["total_loss_seconds"] += duracao
        dados["current_loss_start"] = None
        if dados["loss_logged"]:
            # Duração no histórico: só a parte após o início da viagem.
            inicio_hist = inicio_queda
            if pd.notna(inicio_previsto):
                inicio_hist = max(inicio_queda, inicio_previsto.to_pydatetime())
            _registrar_evento("RETORNO", max((now - inicio_hist).total_seconds(), 0))
        dados["loss_logged"] = False
        logging.info(
            "[%s] Sinal restabelecido às %s (ficou %s sem sinal).",
            trip_number, now, seconds_to_hhmm(duracao),
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

    # --- Regra 4: % de espelhamento ------------------------------------------
    # A janela de contagem vai de (início previsto - 1h35) até o fim previsto.
    # Só a parte de cada queda que cai DENTRO da janela entra na conta; quedas
    # (ou trechos de queda) antes do início da janela ou depois do fim são
    # ignorados. A queda em aberto é contada até min(agora, fim da janela).
    inicio_janela = None
    duracao_total_rota = None
    duracao_janela = None
    tempo_sem_sinal_total = dados["total_loss_seconds"]  # bruto, sem recorte
    percentual_espelhamento = None

    if pd.notna(inicio_previsto) and pd.notna(fim_previsto):
        inicio_janela = (inicio_previsto - timedelta(minutes=JANELA_MINUTES_BEFORE_START)).to_pydatetime()
        fim_janela = fim_previsto.to_pydatetime()
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
            sem_sinal_janela += _overlap(_parse_iso(dados["current_loss_start"]), now)

        tempo_sem_sinal_total = sem_sinal_janela
        if duracao_janela > 0:
            percentual_espelhamento = max(
                0.0, min(100.0, 100.0 * (1 - sem_sinal_janela / duracao_janela))
            )

    # --- Quedas contabilizadas: Qtd., Última Perda e Último Retorno ----------
    # Só contam a partir do início da janela (início previsto - 1h35). Queda
    # que termina antes disso é ignorada; queda que atravessa o início da
    # janela entra com a perda "cortada" no início da janela.
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
        if perda_aberta is not None and (inicio_janela is None or now > inicio_janela):
            if inicio_janela is not None:
                perda_aberta = max(perda_aberta, inicio_janela)
            quedas.append((perda_aberta, None))
    retornos = [r for _p, r in quedas if r is not None]
    ultima_perda = quedas[-1][0] if quedas else None
    ultimo_retorno = retornos[-1] if retornos else None

    # --- Status geral (extra, além do que foi pedido) ------------------------
    # Combina as duas condições de NOK já usadas hoje no acompanhamento de
    # espelhamento (SLA não cumprido OU percentual abaixo da meta), para a
    # aba de resultados já sair pronta para cobrar o time de GR.
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
        **{titulo: _txt(row.get(chave)) for chave, (_n, _l, titulo) in OPTIONAL_COLUMNS.items()},
    }


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
    linhas = [cabecalho] + [[linha[c] for c in cabecalho] for linha in resultados]

    ws.clear()
    ws.update(values=linhas, range_name="A1")


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
            logging.warning("Cabeçalho do histórico diferente do esperado; não foi alterado. "
                            "Esperado: %s", cabecalho)

    def _texto(v) -> str:
        # Apóstrofo força texto no USER_ENTERED: o histórico fica idêntico ao
        # que está em Resultado_Espelhamento (sem converter datas, % ou IDs).
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            return v  # números (ex.: Qtd. Quedas) continuam numéricos
        return ("'" + str(v)) if v not in ("", None) else ""

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

        state = load_state(client)
        eventos_do_ciclo = []
        resultados = []

        for _, row in df.iterrows():
            n_antes = len(eventos_do_ciclo)
            resultado_linha = process_trip(row, state, now, eventos_do_ciclo)
            resultados.append(resultado_linha)
            # Cada evento do histórico leva o "retrato" completo da linha de
            # Resultado_Espelhamento no momento em que o evento foi detectado.
            for ev in eventos_do_ciclo[n_antes:]:
                ev["resultado"] = resultado_linha

        trip_numbers_ativos = set(df["trip_number"].astype(str).str.strip())
        cleanup_state(state, trip_numbers_ativos)
        save_state(state, client)

        client_escrita = client  # mesma sessão autenticada
        write_results(client_escrita, resultados)
        append_events(client_escrita, eventos_do_ciclo)

        logging.info(
            "Ciclo concluído: %s rota(s) processada(s), %s evento(s) de sinal registrado(s).",
            len(resultados), len(eventos_do_ciclo),
        )

    except Exception:
        # Nunca deixa uma exceção matar o loop principal — apenas loga e
        # tenta de novo no próximo ciclo agendado.
        logging.exception("Erro inesperado durante o ciclo de verificação.")


# =============================================================================
# 9. SERVIDOR HTTP MÍNIMO (exigido pelo Web Service do Render) + KEEP-ALIVE
# =============================================================================

class _HealthHandler(BaseHTTPRequestHandler):
    def _ok(self, body=True):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        if body:
            self.wfile.write(b"ok")

    def do_GET(self):
        self._ok()

    def do_HEAD(self):
        self._ok(body=False)

    def log_message(self, *args):  # silencia o log de cada requisição
        pass


def start_http_server():
    server = HTTPServer(("0.0.0.0", HTTP_PORT), _HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logging.info("Servidor HTTP de health-check ouvindo na porta %s.", HTTP_PORT)


def self_ping():
    if not SELF_PING_URL:
        return
    try:
        urllib.request.urlopen(SELF_PING_URL, timeout=15).read()
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

    job()  # executa uma vez imediatamente ao subir o robô
    schedule.every(CHECK_INTERVAL_MINUTES).minutes.do(job)
    if SELF_PING_URL:
        schedule.every(SELF_PING_MINUTES).minutes.do(self_ping)

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
