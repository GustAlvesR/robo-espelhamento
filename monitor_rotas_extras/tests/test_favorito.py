"""Favorito (bookmarklet) rodando na página que imita o AppSheet, com o código tirado do HTML gerado."""
import html
import json
import re
import time
from urllib.parse import unquote

import pytest
from conftest import FIX

FAVORITO = FIX.parents[1] / "favorito"
WEBHOOK = "https://prod-00.westus.logic.azure.com/workflows/teste/triggers/manual/paths/invoke?sig=x"
URL_APP = "https://www.appsheet.com/start/teste#view=ROTA%20EXTRA"


def _codigo_do_html() -> str:
    pagina = (FAVORITO / "instalar_favorito.html").read_text(encoding="utf-8")
    href = html.unescape(re.search(r'href="(javascript:[^"]+)"', pagina).group(1))
    return unquote(href[len("javascript:"):])


def test_html_gerado_esta_atualizado():
    import importlib.util
    spec = importlib.util.spec_from_file_location("gerar", FAVORITO / "gerar.py")
    gerar = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gerar)
    assert _codigo_do_html() == gerar.montar_codigo(), "rode: python favorito/gerar.py"


@pytest.fixture
def pagina(navegador):
    ctx = navegador.new_context(viewport={"width": 1300, "height": 900})
    recebidos = []
    html_app = (FIX / "rotas.html").read_text(encoding="utf-8")
    ctx.route("https://www.appsheet.com/**", lambda r: r.fulfill(status=200, content_type="text/html", body=html_app))
    cors = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "content-type"}

    def webhook(route):
        if route.request.method == "POST":
            recebidos.append(json.loads(route.request.post_data))
        route.fulfill(status=202, headers=cors, body="")

    ctx.route("https://prod-00.westus.logic.azure.com/**", webhook)
    outros = []

    def whatsapp(route):
        outros.append(("whatsapp", route.request.url))
        route.fulfill(status=200, body="Message queued")

    def telegram(route):
        outros.append(("telegram", route.request.post_data))
        route.fulfill(status=200, headers={"Access-Control-Allow-Origin": "*"}, content_type="application/json",
                      body='{"ok": true}')

    ctx.route("https://api.callmebot.com/**", whatsapp)
    ctx.route("https://api.telegram.org/**", telegram)
    page = ctx.new_page()
    page.outros = outros
    page.on("dialog", lambda d: d.accept())
    page.goto(URL_APP)
    yield page, recebidos
    ctx.close()


def _rotas(recebidos):
    return [p["attachments"][0]["content"]["body"][0]["text"] for p in recebidos
            if p["attachments"][0]["content"]["body"][0]["text"].startswith("🚚")]


def _esperar(page, cond, timeout=60):
    # page.wait_for_timeout (e não time.sleep) para o Playwright processar as interceptações do webhook.
    fim = time.time() + timeout
    while time.time() < fim:
        if cond():
            return True
        page.wait_for_timeout(500)
    return False


def test_favorito_le_no_quadro_avisa_e_nao_repete(pagina):
    page, recebidos = pagina
    page.evaluate("cfg => localStorage.setItem('monitorRotasExtras.v1', JSON.stringify({config: cfg}))", {
        "webhookUrl": WEBHOOK, "intervaloS": 300, "veiculos": "TOCO, TRUCK", "origens": "BRSP21",
        "heartbeatHora": "23:59"})
    page.evaluate(_codigo_do_html())

    assert _esperar(page, lambda: len(_rotas(recebidos)) >= 3), recebidos
    titulos = _rotas(recebidos)
    assert sum("BRSP21-BRSP11-SSP34.SOW.VEN" in t for t in titulos) == 2
    assert any("nomenclatura fora do padrão" in t for t in titulos)
    assert "1 - TOCO" in json.dumps(recebidos, ensure_ascii=False)

    estado = page.evaluate("() => JSON.parse(localStorage.getItem('monitorRotasExtras.v1')).estado")
    assert [r["status"] for r in estado["rotas"].values()].count("filtrada") == 2
    assert page.frame_locator('[data-id="quadro"]').locator('[aria-label="ACEITAR"]').count() >= 1
    quadro = next(f for f in page.frames if f != page.main_frame)
    assert quadro.evaluate("window.cliques || 0") == 0

    # nova leitura (recarrega o quadro): nada repetido; clicar no favorito de novo não duplica o monitor
    leitura_1 = estado["ultimaLeitura"]
    page.evaluate(_codigo_do_html())
    page.evaluate("() => window.__monitorRotasFavorito.ciclo()")
    assert _esperar(page, lambda: page.evaluate(
        "() => JSON.parse(localStorage.getItem('monitorRotasExtras.v1')).estado.ultimaLeitura") > leitura_1)
    assert len(_rotas(recebidos)) == 3
    assert page.locator("#monitor-rotas-extras").count() == 1
    assert "card(s)" in page.inner_text('[data-id="status"]')


def test_favorito_sem_canal_abre_configuracao(pagina):
    page, recebidos = pagina
    page.evaluate(_codigo_do_html())
    assert page.locator('[data-id="config"]').is_visible()
    page.fill('[data-campo="webhookUrl"]', WEBHOOK)
    page.click('[data-acao="salvar"]')
    cfg = page.evaluate("() => JSON.parse(localStorage.getItem('monitorRotasExtras.v1')).config")
    assert cfg["webhookUrl"] == WEBHOOK
    page.click('[data-acao="teste"]')
    assert _esperar(page, lambda: any("Teste do monitor" in json.dumps(p, ensure_ascii=False) for p in recebidos), 20)


def test_favorito_envia_para_whatsapp_e_telegram_sem_teams(pagina):
    from urllib.parse import parse_qs, urlsplit
    page, recebidos = pagina
    page.evaluate("cfg => localStorage.setItem('monitorRotasExtras.v1', JSON.stringify({config: cfg}))", {
        "webhookUrl": "", "intervaloS": 300, "veiculos": "TOCO", "origens": "BRSP21", "heartbeatHora": "23:59",
        "whatsappDestinos": "+55 (11) 99999-8888, 123456\n5511888887777;654321",
        "telegramToken": "123:ABC", "telegramChatIds": "-1001"})
    page.evaluate(_codigo_do_html())
    # BRSP21 TOCO + nome fora do padrão (TOCO) = 2 rotas, cada uma em 2 WhatsApp + 1 Telegram
    assert _esperar(page, lambda: len(page.outros) >= 6), page.outros
    zaps = [parse_qs(urlsplit(u).query) for canal, u in page.outros if canal == "whatsapp"]
    assert {z["phone"][0] for z in zaps} == {"+5511999998888", "5511888887777"}
    # datas da página de teste já passaram, então o título vem "(com alertas)"
    assert any(z["text"][0].startswith("*🚚 Rota extra ofertada") and "BRSP21-BRSP11-SSP34.SOW.VEN*" in z["text"][0]
               for z in zaps)
    assert any("⚠ ETA" in z["text"][0] for z in zaps)
    assert all("Veículo: 1 - TOCO" in z["text"][0] for z in zaps)
    tele = [parse_qs(c) for canal, c in page.outros if canal == "telegram"]
    assert tele[0]["chat_id"] == ["-1001"] and "BRSP" in tele[0]["text"][0] + tele[-1]["text"][0]
    assert not recebidos
    estado = page.evaluate("() => JSON.parse(localStorage.getItem('monitorRotasExtras.v1')).estado")
    assert [r["status"] for r in estado["rotas"].values()].count("notificada") == 2
