"""Extensão do Chrome carregada de verdade no Chromium, contra a página que imita o AppSheet.

Precisa de um Chromium que aceite --load-extension (Chrome estável não aceita mais): PW_EXECUTAVEL=<chromium>.
"""
import json
import os
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from conftest import FIX

EXT = FIX.parents[1] / "extensao_chrome"


class Webhook(BaseHTTPRequestHandler):
    recebidos: list = []

    def do_POST(self):
        corpo = self.rfile.read(int(self.headers["Content-Length"]))
        Webhook.recebidos.append(json.loads(corpo))
        self.send_response(202)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture
def ambiente(tmp_path):
    sync_api = pytest.importorskip("playwright.sync_api")
    if not os.getenv("PW_EXECUTAVEL"):
        pytest.skip("defina PW_EXECUTAVEL com um Chromium que aceite --load-extension")
    ext = tmp_path / "ext"
    shutil.copytree(EXT, ext, ignore=shutil.ignore_patterns("testes"))
    manifest = json.loads((ext / "manifest.json").read_text(encoding="utf-8"))
    manifest["host_permissions"].append("http://127.0.0.1/*")
    (ext / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    Webhook.recebidos = []
    srv = HTTPServer(("127.0.0.1", 0), Webhook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    with sync_api.sync_playwright() as pw:
        ctx = pw.chromium.launch_persistent_context(
            str(tmp_path / "perfil"), executable_path=os.environ["PW_EXECUTAVEL"], headless=True,
            args=[f"--disable-extensions-except={ext}", f"--load-extension={ext}"])
        html = (FIX / "rotas.html").read_text(encoding="utf-8")
        ctx.route("https://www.appsheet.com/**", lambda r: r.fulfill(status=200, content_type="text/html", body=html))
        sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=15000)
        yield ctx, sw, f"http://127.0.0.1:{srv.server_port}/hook"
        ctx.close()
    srv.shutdown()


def _esperar(cond, timeout=60):
    fim = time.time() + timeout
    while time.time() < fim:
        if cond():
            return True
        time.sleep(0.5)
    return False


def _rotas():
    return [p["attachments"][0]["content"]["body"][0]["text"] for p in Webhook.recebidos
            if p["attachments"][0]["content"]["body"][0]["text"].startswith("🚚")]


def test_extensao_le_avisa_e_nao_repete(ambiente):
    ctx, sw, webhook = ambiente
    sw.evaluate("cfg => chrome.storage.local.set({config: cfg})", {
        "webhookUrl": webhook, "urlRotas": "",
        "intervaloS": 30, "veiculos": "TOCO, TRUCK", "origens": "BRSP21", "notificacaoLocal": False,
        "heartbeatHora": "23:59", "janelaOfertaMin": 30, "esquecerAposMin": 10, "etaMinimaMin": 0,
        "regexNome": r"^BR[A-Z]{2}\d+(-[A-Z0-9]+)+(\.[A-Z]+)+$"})

    # o operador abre a tela ROTA EXTRA; a extensão adota a aba, lê os cards e avisa:
    # 2x BRSP21 (mesmo nome, ETA/CPT diferentes) + o nome fora do padrão; BRSP10 e BRMG03 são filtradas
    aba = ctx.new_page()
    aba.goto("https://www.appsheet.com/start/teste#view=ROTA%20EXTRA")
    assert _esperar(lambda: len(_rotas()) >= 3), Webhook.recebidos
    titulos = _rotas()
    assert sum("Rota extra" in t and "BRSP21-BRSP11-SSP34.SOW.VEN" in t for t in titulos) == 2
    assert any("nomenclatura fora do padrão" in t for t in titulos)

    estado = sw.evaluate("() => chrome.storage.local.get('estado').then(r => r.estado)")
    assert [r["status"] for r in estado["rotas"].values()].count("filtrada") == 2
    leitura_1 = estado["ultimaLeitura"]

    assert aba.evaluate("window.cliques || 0") == 0

    # nova leitura (pela página de opções): nada repetido
    opcoes = ctx.new_page()
    opcoes.goto(sw.url.replace("background.js", "opcoes.html"))
    opcoes.evaluate("() => chrome.runtime.sendMessage({tipo: 'ciclo_agora'})")
    assert _esperar(lambda: sw.evaluate(
        "() => chrome.storage.local.get('estado').then(r => r.estado.ultimaLeitura)") > leitura_1)
    assert len(_rotas()) == 3
    assert "Aba monitorada" in opcoes.inner_text("#status")
