import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from rotas_extras.config import Config  # noqa: E402

FIX = Path(__file__).parent / "fixtures"
TZ = ZoneInfo("America/Sao_Paulo")
NOME = "BRSP21-BRSP11-SSP34.SOW.VEN"
DESC = "DATA E HORA ETA 16/10/2026 20:00  |  DATA E HORA CPT 16/10/2026 22:00"
TEXTO = ("1 - TOCO\nCAPTAÇÃO\n" + NOME + "\nTEMPO LIMITE DE RETORNO: 00:18:00\n" + DESC + "\nACEITAR")


@pytest.fixture
def tz():
    return TZ


@pytest.fixture
def agora():
    return datetime(2026, 10, 16, 15, 0, tzinfo=TZ)


@pytest.fixture
def cfg():
    return Config()


@pytest.fixture
def bruto():
    def _bruto(**kw):
        base = {"label": NOME, "labels": [NOME], "descricao": DESC, "texto": TEXTO, "secao": "FULLS SP"}
        base.update(kw)
        return base
    return _bruto


# Testes de integração precisam de um Chromium/Chrome/Edge acessível ao Playwright; sem isso, são pulados.
# No Windows usa o Chrome por padrão; outros: PW_CANAL=msedge ou PW_EXECUTAVEL=<caminho do executável>.
@pytest.fixture(scope="module")
def navegador():
    sync_api = pytest.importorskip("playwright.sync_api")
    kwargs = {}
    if os.getenv("PW_CANAL"):
        kwargs["channel"] = os.getenv("PW_CANAL")
    elif os.getenv("PW_EXECUTAVEL"):
        kwargs["executable_path"] = os.getenv("PW_EXECUTAVEL")
    elif sys.platform == "win32":
        kwargs["channel"] = "chrome"
    with sync_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(**kwargs)
        except sync_api.Error as e:
            pytest.skip(f"navegador indisponível: {e}")
        yield browser
        browser.close()
