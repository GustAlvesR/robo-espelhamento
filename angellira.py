# -*- coding: utf-8 -*-
"""Baixa o relatório de rastreamento da Angellira e grava na aba Rastreamento_Angellira.

Faz no navegador (Chromium sem janela, via Playwright) o mesmo que a pessoa faz:
login em auth.angellira.com.br, Tracking, atualiza a página, Exportar colunas,
"Todas as colunas", Exportar. Usuário e senha vêm das variáveis ANGELLIRA_USUARIO e
ANGELLIRA_SENHA; nunca ficam no código nem na planilha.

Rodando dentro do robô, repete a cada ANGELLIRA_INTERVALO_MIN (15) numa thread própria.
Para testar no PC:  python angellira.py --uma-vez [--visivel]
Só gravar um arquivo já baixado:  python angellira.py --arquivo relatorio.xlsx"""

import argparse
import csv
import importlib.util
import io
import logging
import os
import re
import tempfile
import threading
import time
import unicodedata
from datetime import date, datetime
from urllib.parse import urlparse

URL_LOGIN = os.getenv("ANGELLIRA_URL_LOGIN", "https://auth.angellira.com.br/")
URL_HOME = os.getenv("ANGELLIRA_URL_HOME", "https://logistica.angellira.com.br/liralog/home")
URL_TRACKING = os.getenv("ANGELLIRA_URL_TRACKING", "https://logistica.angellira.com.br/tracking/home")
HOST_AUTH = urlparse(URL_LOGIN).netloc
HOST_SISTEMA = urlparse(URL_TRACKING).netloc
INTERVALO_MIN = int(os.getenv("ANGELLIRA_INTERVALO_MIN", "15"))
TIMEOUT_MS = int(os.getenv("ANGELLIRA_TIMEOUT_S", "60")) * 1000
ABA = os.getenv("TRACKING_WORKSHEET_NAME", "Rastreamento_Angellira")
# Sem estas colunas o robô não usa o relatório (ver carregar_rastreamento): não grava.
OBRIGATORIAS = ("PROCEDIMENTO DE EMBARQUE", "VEICULO", "DATA POSICAO")


class ErroAngellira(RuntimeError):
    pass


def _norm(texto) -> str:
    t = unicodedata.normalize("NFKD", str(texto or "")).encode("ascii", "ignore").decode().upper()
    return " ".join(t.split())


def _url_sem_parametros(url: str) -> str:
    # Na volta do login a URL pode trazer código de autorização: não vai para o log.
    return str(url or "").split("?")[0].split("#")[0]


# =============================================================================
# Navegador
# =============================================================================

def _host(url) -> str:
    return urlparse(str(url or "")).netloc


def _no_tracking(url) -> bool:
    # Compara host e caminho: a tela de login traz o endereço de retorno nos parâmetros.
    u = urlparse(str(url or ""))
    return u.netloc == HOST_SISTEMA and u.path.startswith("/tracking")


SELETOR_EXPORTAR = "svg[data-testid='FileDownloadOutlinedIcon']"


def _aguardar_tela(pagina, timeout_ms=None) -> str:
    """'tracking', 'login' ou 'desconhecida'. O Tracking pode mandar para o login só
    depois de carregar o JavaScript, e no caminho passa por redirecionamentos sem
    formulário; por isso espera até aparecer o botão de exportar ou um campo de login."""
    fim = time.monotonic() + (timeout_ms or TIMEOUT_MS) / 1000
    while time.monotonic() < fim:
        try:
            if pagina.locator(f"{SELETOR_EXPORTAR}:visible").count():
                return "tracking"
            if pagina.locator("input[type='password']:visible").count() or (
                    _host(pagina.url) == HOST_AUTH and pagina.locator("input:visible").count()):
                return "login"
        except Exception:
            pass  # página trocando no meio da consulta
        pagina.wait_for_timeout(500)
    return "desconhecida"


def _texto_da_tela(pagina) -> str:
    try:
        return " ".join(pagina.locator("body").inner_text(timeout=3000).split())[:200]
    except Exception:
        return ""


def _esperar(pagina, segundos=5):
    """Espera a página sossegar. A tela do Tracking atualiza sozinha e pode nunca ficar
    sem tráfego, então o limite é curto e estourar não é erro."""
    try:
        pagina.wait_for_load_state("networkidle", timeout=segundos * 1000)
    except Exception:
        pass


def baixar_relatorio(usuario: str, senha: str, pasta: str, estado=None, visivel=False):
    """Devolve (caminho do arquivo baixado, estado da sessão para reaproveitar o login)."""
    from playwright.sync_api import sync_playwright

    passo, pagina = "abrindo o navegador", None
    with sync_playwright() as pw:
        navegador = pw.chromium.launch(
            headless=not visivel, executable_path=os.getenv("ANGELLIRA_CHROMIUM") or None,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
        try:
            contexto = navegador.new_context(storage_state=estado, accept_downloads=True,
                                             locale="pt-BR", viewport={"width": 1600, "height": 1000})
            # Fontes e vídeos não fazem falta e pesam na memória do servidor.
            contexto.route("**/*", lambda rota: rota.abort()
                           if rota.request.resource_type in ("font", "media") else rota.continue_())
            pagina = contexto.new_page()
            pagina.set_default_timeout(TIMEOUT_MS)

            passo = "abrindo o Tracking"
            pagina.goto(URL_TRACKING, wait_until="domcontentloaded")
            if _aguardar_tela(pagina) == "login":
                passo = "fazendo login"
                pagina = _login(pagina, usuario, senha)
                passo = "abrindo o Tracking depois do login"
                _abrir_tracking(pagina)

            passo = "atualizando a página do Tracking"
            pagina.reload(wait_until="domcontentloaded")
            tela = _aguardar_tela(pagina)
            if tela != "tracking":
                motivo = "voltou para a tela de login" if tela == "login" else "o botão de exportar não apareceu"
                raise ErroAngellira(f"o Tracking não abriu: {motivo} (página: {_url_sem_parametros(pagina.url)}; "
                                    f"tela: {_texto_da_tela(pagina)!r})")

            passo = "clicando em exportar colunas"
            icone = pagina.locator(SELETOR_EXPORTAR).first
            botao = pagina.locator(f"button:has({SELETOR_EXPORTAR})")
            (botao.first if botao.count() else icone).click()

            passo = "marcando 'Todas as colunas'"
            rotulo = pagina.locator("label", has_text="Todas as colunas").first
            rotulo.wait_for(state="visible")
            caixa = rotulo.locator("input")
            if caixa.count():
                caixa.first.check(force=True)  # marcar, não alternar: se já estiver marcada, fica
            else:
                pagina.get_by_text("Todas as colunas", exact=True).first.click()

            passo = "clicando em Exportar e esperando o arquivo"
            dialogo = pagina.locator("[role='dialog']")
            exportar = (dialogo if dialogo.count() else pagina).get_by_role("button", name="Exportar", exact=True)
            with pagina.expect_download(timeout=TIMEOUT_MS * 3) as espera:
                exportar.first.click()
            download = espera.value
            nome = download.suggested_filename or "relatorio.xlsx"
            caminho = os.path.join(pasta, re.sub(r"[^\w.-]", "_", nome))
            download.save_as(caminho)
            return caminho, contexto.storage_state()
        except ErroAngellira:
            raise
        except Exception as e:
            url = _url_sem_parametros(pagina.url) if pagina else ""
            detalhe = _motivo_playwright(e)
            if passo == "fazendo login" and pagina:
                detalhe += f"; {_raio_x_login(pagina)}"
            raise ErroAngellira(f"falhou {passo} (página: {url}): {type(e).__name__}: {detalhe}") from None
        finally:
            navegador.close()


def _login(pagina, usuario: str, senha: str):
    """Preenche usuário e senha e devolve a página que chegou ao sistema."""
    if _host(pagina.url) != HOST_AUTH:
        pagina.goto(URL_LOGIN, wait_until="domcontentloaded")
    _fechar_avisos(pagina)
    campo_usuario = pagina.locator("input:not([type='password']):not([type='hidden']):not([type='checkbox']):visible").first
    campo_usuario.wait_for(state="visible")
    campo_usuario.fill(usuario)
    senha_campo = pagina.locator("input[type='password']")
    if senha_campo.count() and senha_campo.first.is_visible():
        senha_campo.first.fill(senha)
        _continuar(pagina, senha_campo.first)
    else:
        # Login em duas telas: usuário, continuar, depois a senha.
        _continuar(pagina, campo_usuario)
        senha_campo.first.wait_for(state="visible")
        senha_campo.first.fill(senha)
        _continuar(pagina, senha_campo.first)
    return _concluir_login(pagina)


PASSA_ETAPA = re.compile(r"^\s*(continuar|autorizar|permitir|acessar|entrar|prosseguir|confirmar|avan[cç]ar)", re.I)


def _concluir_login(pagina):
    """Depois da senha a Angellira passa por auth.../grant antes do sistema, que pode
    abrir na mesma aba ou em outra. Espera a ida sozinha (a pessoa vê uns 10 s) e,
    se não acontecer, fecha o aviso de cookies e clica no botão de seguir."""
    inicio = time.monotonic()
    fim, cliques = inicio + TIMEOUT_MS / 1000, 0
    while time.monotonic() < fim:
        for aba in pagina.context.pages:
            if _host(aba.url) == HOST_SISTEMA:
                if aba is not pagina:
                    pagina.close()
                    aba.set_default_timeout(TIMEOUT_MS)
                return aba
        if time.monotonic() - inicio > 12 and cliques < 3 and not pagina.locator("input[type='password']:visible").count():
            _fechar_avisos(pagina)
            botao = pagina.locator("button:visible, [role='button']:visible, a:visible").filter(has_text=PASSA_ETAPA)
            if not botao.count():
                botao = pagina.locator("img[alt*='tracking' i]:visible, img[alt*='liralog' i]:visible")
            if botao.count():
                try:
                    rotulo = (botao.first.inner_text(timeout=2000) or botao.first.get_attribute("alt") or "").strip()[:30]
                    caminho = urlparse(pagina.url).path
                    botao.first.click(timeout=5000)
                    cliques += 1
                    logging.info("Angellira: cliquei em %r em %s.", rotulo, caminho)
                except Exception:
                    pass
        pagina.wait_for_timeout(2000)
    raise ErroAngellira(f"login não saiu da tela de autenticação (página: {_url_sem_parametros(pagina.url)}; "
                        f"{_raio_x_login(pagina)}; abas: {len(pagina.context.pages)}; "
                        f"tela: {_texto_da_tela(pagina)!r})")


def _fechar_avisos(pagina):
    """Aviso de cookies ou LGPD por cima do formulário impede o clique em Continuar."""
    avisos = pagina.locator("button:visible, a:visible").filter(
        has_text=re.compile(r"^\s*(aceitar|aceito|concordo|entendi|ok|fechar|prosseguir)", re.I))
    for i in range(min(avisos.count(), 3)):
        try:
            avisos.nth(i).click(timeout=2000)
        except Exception:
            pass


def _continuar(pagina, campo):
    """Clica em Continuar. Procura primeiro pelo texto, depois pelo estilo do botão,
    sempre entre os visíveis (um botão escondido antes no HTML travava o clique).
    Se o clique não acontecer, envia o formulário com Enter no campo."""
    candidatos = (
        pagina.locator("button:visible, [role='button']:visible, input[type='submit']:visible").filter(
            has_text=re.compile(r"continuar|entrar|acessar|login|avan[cç]ar", re.I)),
        pagina.locator(".mdc-button--raised:visible, .mdc-button--unelevated:visible"),
        pagina.locator("button[type='submit']:visible"),
    )
    botao = next((c.first for c in candidatos if c.count()), None)
    if botao is not None:
        try:
            botao.click(timeout=15000)
            return
        except Exception as e:
            logging.info("Angellira: clique em Continuar não aconteceu (%s); tentando Enter.", _motivo_playwright(e))
    campo.press("Enter")


def _raio_x_login(pagina) -> str:
    """Botões visíveis e sinais de captcha, para entender pelo log por que o login travou.
    Não lê o conteúdo dos campos."""
    try:
        return pagina.evaluate("""() => {
            const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
            const botoes = [...document.querySelectorAll('button, [role=button], input[type=submit]')].filter(vis)
                .slice(0, 6).map(b => `${(b.innerText || b.value || '').trim().slice(0, 25) || '?'}` +
                    `${b.disabled || b.getAttribute('aria-disabled') === 'true' ? ' (desabilitado)' : ''}`);
            const captcha = !!document.querySelector("iframe[src*='captcha'], iframe[src*='turnstile'], .g-recaptcha, .h-captcha, .cf-turnstile");
            const outros = [...document.querySelectorAll('a, img[alt]')].filter(vis).slice(0, 6)
                .map(e => (e.tagName === 'IMG' ? 'img:' + e.alt : (e.innerText || '').trim()).slice(0, 25)).filter(Boolean);
            return `botões: ${botoes.join(', ') || 'nenhum'}; links: ${outros.join(', ') || 'nenhum'}; ` +
                `captcha: ${captcha ? 'sim' : 'não'}`;
        }""")
    except Exception:
        return "botões: não foi possível ler"


def _motivo_playwright(e) -> str:
    """Primeira linha do erro e, do registro do Playwright, a pista do que travou."""
    linhas = [l.strip(" -") for l in str(e).splitlines() if l.strip()]
    if not linhas:
        return ""
    pistas = [l for l in linhas[1:] if re.search(r"intercepts|not enabled|not visible|not stable|disabled|outside", l)]
    return "; ".join(dict.fromkeys([linhas[0]] + pistas[-1:]))[:400]


def _abrir_tracking(pagina):
    """Depois do login cai na home do Liralog: tenta o Tracking direto e, se não abrir,
    clica no ícone TRACKING (que pode abrir em outra aba)."""
    _esperar(pagina)
    pagina.goto(URL_TRACKING, wait_until="domcontentloaded")
    tela = _aguardar_tela(pagina, 30000)
    if tela == "tracking":
        return
    if tela == "login":
        raise ErroAngellira(f"depois do login o Tracking pediu login de novo (tela: {_texto_da_tela(pagina)!r})")
    pagina.goto(URL_HOME, wait_until="domcontentloaded")
    icone = pagina.locator("img[alt='TRACKING']").first
    icone.wait_for(state="attached")
    try:
        with pagina.context.expect_page(timeout=5000) as nova:
            icone.click()
        aba = nova.value
        aba.wait_for_load_state("domcontentloaded")
        pagina.goto(aba.url, wait_until="domcontentloaded")
        aba.close()
    except Exception:
        pagina.wait_for_url(_no_tracking, timeout=TIMEOUT_MS)


# =============================================================================
# Arquivo -> linhas -> planilha
# =============================================================================

def _texto_celula(v):
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v if isinstance(v, (int, float)) else str(v).strip()


def ler_arquivo(caminho: str) -> list:
    """Linhas do relatório (cabeçalho primeiro), de .xlsx ou .csv."""
    if caminho.lower().endswith((".xlsx", ".xlsm")):
        import openpyxl
        ws = openpyxl.load_workbook(caminho, read_only=True, data_only=True).worksheets[0]
        linhas = [[_texto_celula(v) for v in linha] for linha in ws.iter_rows(values_only=True)]
    else:
        bruto = open(caminho, "rb").read()
        for codificacao in ("utf-8-sig", "latin-1"):
            try:
                texto = bruto.decode(codificacao)
                break
            except UnicodeDecodeError:
                continue
        dialeto = csv.Sniffer().sniff(texto[:5000], delimiters=";,\t")
        linhas = [[c.strip() for c in l] for l in csv.reader(io.StringIO(texto), dialeto)]
    # Linhas vazias no fim e colunas vazias à direita do cabeçalho não interessam.
    while linhas and not any(str(c).strip() for c in linhas[-1]):
        linhas.pop()
    if not linhas:
        raise ErroAngellira("o arquivo exportado veio vazio")
    largura = max(i + 1 for i, c in enumerate(linhas[0]) if str(c).strip())
    return [(list(l) + [""] * largura)[:largura] for l in linhas]


def validar(linhas: list) -> None:
    cab = {_norm(c) for c in linhas[0]}
    faltam = [c for c in OBRIGATORIAS if c not in cab]
    if faltam:
        raise ErroAngellira(f"o relatório veio sem as colunas {faltam}; a aba não foi alterada "
                            f"(cabeçalho recebido: {[str(c) for c in linhas[0][:8]]}...)")


def _letra(n: int) -> str:
    letra = ""
    while n:
        n, r = divmod(n - 1, 26)
        letra = chr(65 + r) + letra
    return letra


def gravar_na_aba(planilha, linhas: list) -> None:
    """Grava o relatório novo por cima e depois limpa o que sobrou do anterior (linhas
    abaixo e colunas à direita). Fica igual a limpar e colar, mas a aba nunca fica
    vazia enquanto o robô pode estar lendo."""
    ws = planilha.worksheet(ABA)
    n, largura = len(linhas), len(linhas[0])
    if ws.row_count < n:
        ws.add_rows(n - ws.row_count)
    if ws.col_count < largura:
        ws.add_cols(largura - ws.col_count)
    ws.update(values=linhas, range_name="A1", raw=True)
    sobras = []
    if ws.row_count > n:
        sobras.append(f"A{n + 1}:{_letra(ws.col_count)}{ws.row_count}")
    if ws.col_count > largura:
        sobras.append(f"{_letra(largura + 1)}1:{_letra(ws.col_count)}{n}")
    if sobras:
        ws.batch_clear(sobras)


# =============================================================================
# Ciclo
# =============================================================================

class AtualizadorAngellira:
    def __init__(self, get_client, spreadsheet_id: str):
        self._get_client = get_client
        self._spreadsheet_id = spreadsheet_id
        self._estado = None        # cookies do login, só em memória
        self.ultimo_ok = None
        self.ultimo_erro = ""

    def atualizar(self, visivel=False) -> int:
        usuario, senha = os.getenv("ANGELLIRA_USUARIO", ""), os.getenv("ANGELLIRA_SENHA", "")
        if not usuario or not senha:
            raise ErroAngellira("defina ANGELLIRA_USUARIO e ANGELLIRA_SENHA")
        inicio = time.monotonic()
        with tempfile.TemporaryDirectory() as pasta:
            try:
                caminho, self._estado = baixar_relatorio(usuario, senha, pasta, self._estado, visivel)
            except ErroAngellira:
                self._estado = None  # na próxima, login do zero
                raise
            linhas = ler_arquivo(caminho)
        validar(linhas)
        gravar_na_aba(self._get_client().open_by_key(self._spreadsheet_id), linhas)
        self.ultimo_ok, self.ultimo_erro = datetime.now(), ""
        logging.info("Angellira: relatório com %s linha(s) e %s coluna(s) gravado na aba %s (%.0f s).",
                     len(linhas) - 1, len(linhas[0]), ABA, time.monotonic() - inicio)
        return len(linhas) - 1

    def loop(self):
        time.sleep(60)  # deixa o primeiro ciclo do robô começar antes
        while True:
            try:
                self.atualizar()
            except Exception as e:
                self.ultimo_erro = str(e)[:700]
                logging.warning("Angellira: %s. Nova tentativa em %s min.", self.ultimo_erro, INTERVALO_MIN)
            time.sleep(INTERVALO_MIN * 60)


def iniciar_em_segundo_plano(get_client, spreadsheet_id: str):
    """Liga a atualização automática se houver usuário e senha. Sem o Playwright
    instalado, só avisa no log; o robô segue normal."""
    if os.getenv("ANGELLIRA_ATIVO", "1") == "0" or not os.getenv("ANGELLIRA_USUARIO") or not os.getenv("ANGELLIRA_SENHA"):
        logging.info("Angellira: atualização automática desligada (sem ANGELLIRA_USUARIO/ANGELLIRA_SENHA).")
        return None
    if importlib.util.find_spec("playwright") is None:
        logging.warning("Angellira: Playwright não instalado; atualização automática desligada.")
        return None
    atualizador = AtualizadorAngellira(get_client, spreadsheet_id)
    threading.Thread(target=atualizador.loop, daemon=True, name="angellira").start()
    logging.info("Angellira: atualização automática ligada, a cada %s min.", INTERVALO_MIN)
    return atualizador


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uma-vez", action="store_true", help="baixa e grava uma vez e sai")
    ap.add_argument("--visivel", action="store_true", help="abre o navegador com janela (para acompanhar)")
    ap.add_argument("--arquivo", help="só grava este arquivo já baixado, sem abrir o navegador")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
    import monitor_espelhamento as robo
    if args.arquivo:
        linhas = ler_arquivo(args.arquivo)
        validar(linhas)
        gravar_na_aba(robo.get_gspread_client().open_by_key(robo.TRACKING_SPREADSHEET_ID), linhas)
        logging.info("Arquivo gravado: %s linha(s).", len(linhas) - 1)
        return
    atualizador = AtualizadorAngellira(robo.get_gspread_client, robo.TRACKING_SPREADSHEET_ID)
    if args.uma_vez:
        atualizador.atualizar(visivel=args.visivel)
    else:
        atualizador.loop()


if __name__ == "__main__":
    main()
