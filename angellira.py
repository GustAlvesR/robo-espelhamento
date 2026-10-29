# -*- coding: utf-8 -*-
"""Baixa o relatório de rastreamento da Angellira e grava na aba Rastreamento_Angellira.

Faz no navegador (Chromium sem janela, via Playwright) o mesmo que a pessoa faz:
login em auth.angellira.com.br, Tracking, atualiza a página, Exportar colunas,
"Todas as colunas", Exportar. Usuário e senha vêm das variáveis ANGELLIRA_USUARIO e
ANGELLIRA_SENHA; nunca ficam no código nem na planilha.

Rodando dentro do robô, repete a cada ANGELLIRA_INTERVALO_MIN (15) numa thread própria.
O navegador roda num processo à parte, com prioridade baixa de CPU e prazo
(ANGELLIRA_LIMITE_MIN, 8): travou, é encerrado e o robô segue.
Para testar no PC:  python angellira.py --uma-vez [--visivel]
Só gravar um arquivo já baixado:  python angellira.py --arquivo relatorio.xlsx"""

import argparse
import csv
import importlib.util
import io
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from datetime import date, datetime
from urllib.parse import urlparse

import memoria

URL_LOGIN = os.getenv("ANGELLIRA_URL_LOGIN", "https://auth.angellira.com.br/")
URL_HOME = os.getenv("ANGELLIRA_URL_HOME", "https://logistica.angellira.com.br/liralog/home")
URL_TRACKING = os.getenv("ANGELLIRA_URL_TRACKING", "https://logistica.angellira.com.br/tracking/home")
HOST_AUTH = urlparse(URL_LOGIN).netloc
HOST_SISTEMA = urlparse(URL_TRACKING).netloc
INTERVALO_MIN = int(os.getenv("ANGELLIRA_INTERVALO_MIN", "15"))
TIMEOUT_MS = int(os.getenv("ANGELLIRA_TIMEOUT_S", "60")) * 1000
LIMITE_RODADA_S = int(float(os.getenv("ANGELLIRA_LIMITE_MIN", "8")) * 60)
ABA = os.getenv("TRACKING_WORKSHEET_NAME", "Rastreamento_Angellira")
# Domínios que a tela do Tracking carrega e o relatório não usa (mapa, métricas, chat).
# Bloqueados, o navegador disputa menos CPU com o robô; se o Tracking não abrir sem
# eles, o robô volta sozinho ao modo completo.
HOSTS_DISPENSAVEIS = ("maps.googleapis.com", "maps.gstatic.com", "google-analytics.com",
                      "googletagmanager.com", "doubleclick.net", "hotjar.com", "clarity.ms",
                      "tawk.to", "zdassets.com", "intercom.io", "crisp.chat", "jivosite.com",
                      "octadesk.com", "movidesk.com", "hs-scripts.com", "hubspot.com",
                      # Aviso de cookies: fica por cima do botão de exportar e pesa na memória.
                      "politicacookies.angellira.com.br")
# Sem estas colunas o robô não usa o relatório (ver carregar_rastreamento): não grava.
OBRIGATORIAS = ("PROCEDIMENTO DE EMBARQUE", "VEICULO", "DATA POSICAO")


class ErroAngellira(RuntimeError):
    estado = None  # sessão já logada quando a falha veio depois do login


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
ESPERA_LINHAS_S = int(os.getenv("ANGELLIRA_ESPERA_LINHAS_S", "90"))
# Retrato da tela antes de exportar: o site mostra em /diagnostico/angellira (só logado).
TELA_DIAGNOSTICO = os.path.join(tempfile.gettempdir(), "torre_angellira_tela.png")
ARQ_DIAGNOSTICO = "diagnostico.txt"  # na pasta da rodada: o processo do navegador escreve, o robô lê
# Linhas da tabela do Tracking com dado (5 células ou mais): a linha de "sem registros" e
# cabeçalhos não contam. O Exportar monta o arquivo com o que já carregou.
_JS_LINHAS = """() => [...document.querySelectorAll("tr, [role=row]")].filter(r => {
    const b = r.getBoundingClientRect();
    return b.width > 0 && b.height > 0 && r.querySelectorAll("td, [role=cell], [role=gridcell]").length >= 5;
}).length"""
_JS_DIAGNOSTICO = """seletorExportar => {
    const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
    const linhas = (%s)();
    const vazio = [...document.querySelectorAll("body *")].find(e => vis(e) && !e.children.length &&
        /nenhum|sem (dados|registros|resultados)|no rows|não há|nao ha/i.test(e.innerText || ""));
    const camadas = [...document.querySelectorAll(".MuiModal-root, .MuiPopover-root, .MuiBackdrop-root, [aria-modal=true]")]
        .filter(vis).map(m => (String(m.className).split(" ")[0] || m.tagName) + ": " +
            ((m.innerText || "").trim().replace(/\\s+/g, " ").slice(0, 80) || "sem texto"));
    const icone = document.querySelector(seletorExportar);
    let porCima = "";
    if (icone) {
        const r = icone.getBoundingClientRect();
        const e = document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2);
        if (e && !icone.contains(e) && !e.contains(icone)) porCima = e.outerHTML.slice(0, 160);
    }
    return `linhas com dado: ${linhas}; aviso de vazio: ${vazio ? vazio.innerText.trim().slice(0, 80) : "nenhum"}; ` +
        `camadas abertas: ${camadas.join(" | ") || "nenhuma"}; por cima do exportar: ${porCima || "nada"}`;
}""" % _JS_LINHAS


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


def _aguardar_linhas(pagina) -> None:
    """Espera a tabela do Tracking ter linhas (até ESPERA_LINHAS_S). Sem linhas no prazo,
    segue: o relatório vazio é recusado depois, sem apagar o anterior."""
    try:
        pagina.wait_for_function(f"({_JS_LINHAS})() > 0", timeout=ESPERA_LINHAS_S * 1000, polling=1000)
    except Exception:
        logging.info("Angellira: a tabela do Tracking não mostrou linhas em %s s; exportando assim mesmo.",
                     ESPERA_LINHAS_S)


def _retrato(pagina, pasta: str) -> None:
    """Print da tela e diagnóstico (linhas, camadas por cima, aviso de vazio) antes de
    exportar. O diagnóstico vai para o log quando o relatório vem vazio."""
    try:
        pagina.screenshot(path=TELA_DIAGNOSTICO, timeout=10000)
    except Exception as e:
        logging.info("Angellira: print da tela não saiu (%s).", _motivo_playwright(e))
    try:
        texto = f"{pagina.evaluate(_JS_DIAGNOSTICO, SELETOR_EXPORTAR)}; {_raio_x(pagina)}"
    except Exception as e:
        texto = f"diagnóstico não saiu ({_motivo_playwright(e)})"
    with open(os.path.join(pasta, ARQ_DIAGNOSTICO), "w", encoding="utf-8") as f:
        f.write(texto)


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


def _dispensavel(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in HOSTS_DISPENSAVEIS)


def baixar_relatorio(usuario: str, senha: str, pasta: str, estado=None, visivel=False, leve=True,
                     registrar_rede=False):
    """Devolve (caminho do arquivo baixado, estado da sessão para reaproveitar o login).
    leve: bloqueia os domínios dispensáveis. registrar_rede: põe no log os domínios
    carregados e as chamadas feitas pelo Exportar (sem parâmetros, que podem ter token)."""
    from playwright.sync_api import sync_playwright

    passo, pagina, no_tracking, tempos = "abrindo o navegador", None, False, []

    def marcar(novo):
        """Passo atual: vai para as mensagens de erro, para o log de tempos e para um
        arquivo que o processo pai lê se tiver de encerrar a rodada por prazo."""
        nonlocal passo
        passo = novo
        tempos.append((novo, time.monotonic()))
        try:
            with open(os.path.join(pasta, "passo.txt"), "w", encoding="utf-8") as f:
                f.write(f"{novo}|{time.time():.0f}")
        except OSError:
            pass

    with sync_playwright() as pw:
        navegador = pw.chromium.launch(
            headless=not visivel, executable_path=os.getenv("ANGELLIRA_CHROMIUM") or None,
            # Opções de economia (--single-process, sem zygote, sem isolamento de sites) foram
            # medidas e mudam menos de 5%: o grosso é a própria página do Tracking.
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
        try:
            # Imagens, fontes e vídeos não fazem falta para exportar e pesam na memória:
            # o servidor tem 512 MB para o robô e o navegador juntos.
            hosts, chamadas_exportar, coletando = Counter(), [], [False]

            def rotear(rota):
                req = rota.request
                host = urlparse(req.url).hostname or ""
                hosts[host] += 1
                if coletando[0] and req.resource_type in ("xhr", "fetch"):
                    chamadas_exportar.append(f"{req.method} {host}{urlparse(req.url).path}")
                if req.resource_type in ("image", "font", "media") or (leve and _dispensavel(host)):
                    return rota.abort()
                return rota.continue_()

            def novo_contexto(sessao):
                ctx = navegador.new_context(storage_state=sessao, accept_downloads=True,
                                            locale="pt-BR", viewport={"width": 1280, "height": 720})
                ctx.route("**/*", rotear)
                pg = ctx.new_page()
                pg.set_default_timeout(TIMEOUT_MS)
                return ctx, pg

            contexto, pagina = novo_contexto(estado)
            marcar("abrindo o Tracking")
            # "commit" e não "domcontentloaded": o carregamento do Tracking já passou de
            # 60 s; quem decide se a tela abriu é o _aguardar_tela.
            pagina.goto(URL_TRACKING, wait_until="commit")
            if _aguardar_tela(pagina) == "login":
                marcar("fazendo login")
                pagina = _login(pagina, usuario, senha)
                marcar("abrindo o Tracking depois do login")
                contexto, pagina = _tracking_em_contexto_limpo(contexto, pagina, novo_contexto)

            marcar("atualizando a página do Tracking")
            pagina.reload(wait_until="commit")
            tela = _aguardar_tela(pagina)
            if tela != "tracking":
                motivo = "voltou para a tela de login" if tela == "login" else "o botão de exportar não apareceu"
                raise ErroAngellira(f"o Tracking não abriu: {motivo} (página: {_url_sem_parametros(pagina.url)}; "
                                    f"tela: {_texto_da_tela(pagina)!r})")
            no_tracking = True

            marcar("esperando a tabela carregar")
            _aguardar_linhas(pagina)
            _retrato(pagina, pasta)

            marcar("clicando em exportar colunas")
            rotulo = _abrir_exportacao(pagina)

            marcar("marcando 'Todas as colunas'")
            caixa = rotulo.locator("input")
            if not caixa.count():
                _clicar(pagina.get_by_text("Todas as colunas", exact=True).first)
            elif not caixa.first.is_checked():  # marcar, não alternar: se já estiver marcada, fica
                _clicar(caixa.first, forcar=True)
                if not caixa.first.is_checked():  # clique forçado também cai em camada por cima
                    caixa.first.evaluate("e => e.click()")
                if not caixa.first.is_checked():
                    raise ErroAngellira("'Todas as colunas' não ficou marcada")

            marcar("clicando em Exportar e esperando o arquivo")
            dialogo = pagina.locator("[role='dialog']")
            exportar = (dialogo if dialogo.count() else pagina).get_by_role("button", name="Exportar", exact=True)
            coletando[0] = True
            with pagina.expect_download(timeout=TIMEOUT_MS * 3) as espera:
                _clicar(exportar.first)
            download = espera.value
            if registrar_rede:
                logging.info("Angellira: domínios carregados (%s): %s. O Exportar chamou: %s.",
                             "modo leve" if leve else "modo completo",
                             ", ".join(f"{h}{' [bloqueado]' if leve and _dispensavel(h) else ''} ({n})"
                                       for h, n in hosts.most_common(15)),
                             "; ".join(dict.fromkeys(chamadas_exportar)) or "nada (arquivo montado no navegador)")
            nome = download.suggested_filename or "relatorio.xlsx"
            caminho = os.path.join(pasta, re.sub(r"[^\w.-]", "_", nome))
            download.save_as(caminho)
            marcar("fim")
            logging.info("Angellira: tempo por passo: %s.", _resumo_tempos(tempos))
            return caminho, contexto.storage_state()
        except Exception as e:
            erro = e
            if not isinstance(e, ErroAngellira):
                url = _url_sem_parametros(pagina.url) if pagina else ""
                detalhe = _motivo_playwright(e)
                if pagina and not pagina.is_closed():
                    detalhe += f"; {_raio_x(pagina)}"
                erro = ErroAngellira(f"falhou {passo} (página: {url}): {type(e).__name__}: {detalhe}; "
                                     f"tempos: {_resumo_tempos(tempos + [('', time.monotonic())])}")
            if no_tracking:
                try:
                    erro.estado = contexto.storage_state()
                except Exception:
                    pass
            raise erro from None
        finally:
            navegador.close()


def _resumo_tempos(tempos: list) -> str:
    return ", ".join(f"{p} {t2 - t1:.0f} s" for (p, t1), (_p2, t2) in zip(tempos, tempos[1:]))


def _tracking_em_contexto_limpo(contexto, pagina, novo_contexto):
    """Depois do login a aba vem da home do Liralog, e seguir nela até o Tracking já
    travou com CPU presa até o prazo. Abre o Tracking num navegador limpo só com a
    sessão, como nas rodadas que reaproveitam o login (essas levam ~1 min). Se a
    sessão nova não servir ali, segue na aba do login, do jeito antigo."""
    _esperar(pagina, 8)  # o sistema grava o token logo depois de chegar
    sessao = contexto.storage_state()
    # Página em branco para os scripts da home pararem; a aba fica, porque o que estiver
    # guardado só nela (sessionStorage) é preciso se o caminho limpo não servir.
    pagina.goto("about:blank")
    contexto2, pagina2 = novo_contexto(sessao)
    pagina2.goto(URL_TRACKING, wait_until="commit")
    if _aguardar_tela(pagina2) != "login":
        contexto.close()
        return contexto2, pagina2
    logging.info("Angellira: a sessão do login não abriu o Tracking num navegador limpo; seguindo na aba do login.")
    contexto2.close()
    _abrir_tracking(pagina)
    return contexto, pagina


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
                        f"{_raio_x(pagina)}; abas: {len(pagina.context.pages)}; "
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


def _raio_x(pagina) -> str:
    """Botões visíveis, janelas abertas e sinais de captcha, para entender pelo log onde
    a automação travou. Não lê o conteúdo dos campos."""
    try:
        return pagina.evaluate("""() => {
            const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
            const botoes = [...document.querySelectorAll('button, [role=button], input[type=submit]')].filter(vis)
                .slice(0, 6).map(b => `${(b.innerText || b.value || '').trim().slice(0, 25) || '?'}` +
                    `${b.disabled || b.getAttribute('aria-disabled') === 'true' ? ' (desabilitado)' : ''}`);
            const captcha = !!document.querySelector("iframe[src*='captcha'], iframe[src*='turnstile'], .g-recaptcha, .h-captcha, .cf-turnstile");
            const outros = [...document.querySelectorAll('a, img[alt]')].filter(vis).slice(0, 6)
                .map(e => (e.tagName === 'IMG' ? 'img:' + e.alt : (e.innerText || '').trim()).slice(0, 25)).filter(Boolean);
            // Uma janela do MUI tem várias camadas (fundo, contêiner...); só a [role=dialog] conta.
            const janelas = [...document.querySelectorAll("[role=dialog], [role=alertdialog]")].filter(vis)
                .map(j => (j.innerText || '').trim().replace(/\\s+/g, ' ').slice(0, 80) || '?');
            return `botões: ${botoes.join(', ') || 'nenhum'}; links: ${outros.join(', ') || 'nenhum'}; ` +
                `janelas por cima: ${janelas.length}${janelas.length ? ' (' + janelas.join(' | ') + ')' : ''}; ` +
                `captcha: ${captcha ? 'sim' : 'não'}`;
        }""")
    except Exception:
        return "botões: não foi possível ler"


def _motivo_playwright(e) -> str:
    """Primeira linha do erro e, do registro do Playwright, a pista do que travou."""
    linhas = [l.strip(" -") for l in str(e).splitlines() if l.strip()]
    if not linhas:
        return ""
    pistas = [l for l in linhas[1:] if re.search(r"intercepts|not enabled|not visible|not stable|disabled|outside|detached", l)]
    # Sem pista conhecida, a última linha do registro diz no que ele estava esperando.
    return "; ".join(dict.fromkeys([linhas[0]] + (pistas[-1:] or linhas[-1:])))[:400]


def _clicar(alvo, forcar=False):
    """Clique normal; se algo por cima interceptar, clique via JavaScript no próprio elemento."""
    try:
        alvo.click(timeout=15000, force=forcar)
    except Exception as e:
        logging.info("Angellira: clique normal não pegou (%s); clicando via JavaScript.", _motivo_playwright(e))
        alvo.evaluate("e => e.click()")


FECHA_JANELA = re.compile(r"^\s*(fechar|ok|entendi|ciente|concordo|aceitar|aceito|depois|agora n[aã]o|pular|dispensar)", re.I)


def _fechar_janelas(pagina):
    """Fecha janelas que aparecem por cima do Tracking (comunicados depois do login, por
    exemplo) pelo X, por um botão de dispensar ou com Esc. O texto vai para o log."""
    for _ in range(3):
        janelas = pagina.locator("[role='dialog']:visible, [role='alertdialog']:visible")
        if not janelas.count():
            return
        janela = janelas.first
        try:
            texto = " ".join(janela.inner_text(timeout=3000).split())[:100]
        except Exception:
            texto = "?"
        if "Todas as colunas" in texto:
            return  # é o próprio popup de exportar
        fechar = janela.locator("button[aria-label*='fechar' i], button[aria-label*='close' i], "
                                "button:has(svg[data-testid^='Close'])")
        if not fechar.count():
            fechar = janela.locator("button, [role='button']").filter(has_text=FECHA_JANELA)
        try:
            if fechar.count():
                _clicar(fechar.first)
            else:
                pagina.keyboard.press("Escape")
        except Exception:
            pagina.keyboard.press("Escape")
        logging.info("Angellira: fechei a janela por cima do Tracking: %r.", texto)
        pagina.wait_for_timeout(1000)


def _clicar_exportar_js(pagina) -> bool:
    """Acha o ícone visível e clica no botão dele numa operação só, dentro da página: a
    barra do Tracking é redesenhada o tempo todo e o elemento some entre achar e clicar."""
    return pagina.evaluate("""(seletor) => {
        const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
        const icone = [...document.querySelectorAll(seletor)].find(vis);
        if (!icone) return false;
        const botao = icone.closest("button, [role=button]");
        if (botao) botao.click();
        else icone.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true, view: window }));
        return true;
    }""", SELETOR_EXPORTAR)


def _abrir_exportacao(pagina):
    """Abre o popup de exportar e devolve o rótulo 'Todas as colunas'. Antes fecha
    janelas por cima; tenta o clique normal e, se não abrir, o clique via JavaScript."""
    rotulo = pagina.locator("label:visible", has_text="Todas as colunas").first
    erro = None
    for tentativa in range(3):
        _fechar_janelas(pagina)
        _fechar_avisos(pagina)
        try:
            if tentativa == 0:
                pagina.locator(f"button:has({SELETOR_EXPORTAR}):visible, {SELETOR_EXPORTAR}:visible").first.click(timeout=15000)
            elif not _clicar_exportar_js(pagina):
                raise ErroAngellira("o ícone de exportar não está visível")
            rotulo.wait_for(state="visible", timeout=15000)
            return rotulo
        except Exception as e:
            erro = e
            logging.info("Angellira: popup de exportar não abriu na tentativa %s (%s; %s).",
                         tentativa + 1, _motivo_playwright(e), _raio_x(pagina))
            pagina.keyboard.press("Escape")  # fecha o que tiver aberto por cima antes de tentar de novo
    raise erro


def _abrir_tracking(pagina):
    """Depois do login cai na home do Liralog: tenta o Tracking direto e, se não abrir,
    clica no ícone TRACKING (que pode abrir em outra aba)."""
    _esperar(pagina)
    pagina.goto(URL_TRACKING, wait_until="commit")
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


def validar(linhas: list, detalhe: str = "") -> None:
    # Exportar antes de a tabela carregar devolve só o cabeçalho; gravar isso apagaria as
    # posições de todas as rotas até a próxima rodada.
    if len(linhas) <= 1:
        raise ErroAngellira(f"o relatório veio sem linhas; a aba {ABA} ficou com o relatório anterior"
                            + (f" ({detalhe})" if detalhe else ""))
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

def baixar_em_processo(usuario_env: str, senha_env: str, pasta: str, estado=None, visivel=False, leve=True,
                      registrar_rede=False):
    """Roda baixar_relatorio num processo à parte e devolve o mesmo que ele.
    Depois de um login, a página já ficou consumindo CPU sem parar enquanto o robô
    esperava por ela sem prazo: o ciclo passava de 20 min e o vigia reiniciava tudo.
    Separado, o navegador roda com prioridade baixa e, se passar do prazo, é encerrado.
    Usuário e senha vão pelas variáveis de ambiente, nunca na linha de comando."""
    entrada, saida = os.path.join(pasta, "estado_entrada.json"), os.path.join(pasta, "resultado.json")
    if estado:
        with open(entrada, "w", encoding="utf-8") as f:
            json.dump(estado, f)
    cmd = [sys.executable, os.path.abspath(__file__), "--processo-filho", pasta]
    cmd += ["--visivel"] * visivel + ["--completo"] * (not leve) + ["--registrar-rede"] * registrar_rede
    env = {**os.environ, "ANGELLIRA_USUARIO": usuario_env, "ANGELLIRA_SENHA": senha_env}
    memoria.liberar()
    proc = subprocess.Popen(cmd, env=env, start_new_session=True)
    inicio = time.monotonic()
    pico = {"navegador": 0, "robo": 0, "container": 0}
    while True:
        try:
            proc.wait(timeout=5)
            break
        except subprocess.TimeoutExpired:
            descendentes = _descendentes(proc.pid)
            _baixar_prioridade(descendentes)
            uso, _limite = memoria.container_mb()
            for chave, valor in (("navegador", sum(memoria.processo_mb(p) for p in [proc.pid] + descendentes)),
                                 ("robo", memoria.processo_mb()), ("container", uso or 0)):
                pico[chave] = max(pico[chave], valor)
            if time.monotonic() - inicio > LIMITE_RODADA_S:
                for pid in [proc.pid] + descendentes:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except (OSError, AttributeError):
                        pass
                proc.wait()
                onde = ""
                try:
                    with open(os.path.join(pasta, "passo.txt"), encoding="utf-8") as f:
                        nome_passo, desde = f.read().rsplit("|", 1)
                    onde = f" (parada em: {nome_passo}, há {time.time() - float(desde):.0f} s)"
                except (OSError, ValueError):
                    pass
                erro = ErroAngellira(f"a rodada passou de {LIMITE_RODADA_S / 60:g} min e o navegador foi encerrado{onde}")
                erro.estado = estado
                raise erro from None
    if pico["navegador"]:  # rodada de menos de 5 s não tem amostra
        uso, limite = memoria.container_mb()
        logging.info("Angellira: pico de memória na rodada: navegador %s MB, robô %s MB%s.", pico["navegador"],
                     pico["robo"], f", container {pico['container']} MB" + (f" de {limite} MB" if limite else "")
                     if uso is not None else "")
    try:
        with open(saida, encoding="utf-8") as f:
            resultado = json.load(f)
    except (OSError, ValueError):
        raise ErroAngellira(f"o processo do navegador terminou sem resultado (código {proc.returncode})") from None
    if resultado.get("erro"):
        erro = ErroAngellira(resultado["erro"])
        erro.estado = resultado.get("estado")
        raise erro
    return resultado["caminho"], resultado.get("estado")


PRIORIDADE_NAVEGADOR = 10  # nice: o robô (0) tem preferência na CPU


def _descendentes(pid: int) -> list:
    """Processos abaixo de pid, pelo /proc (Linux). O Chromium abre sessão própria,
    então o grupo de processos do filho não chega até ele."""
    filhos = defaultdict(list)
    try:
        for d in os.listdir("/proc"):
            if d.isdigit():
                try:
                    with open(f"/proc/{d}/stat", encoding="utf-8", errors="replace") as f:
                        filhos[int(f.read().rsplit(")", 1)[1].split()[1])].append(int(d))
                except (OSError, ValueError, IndexError):
                    pass
    except OSError:
        return []
    fila, todos = [pid], []
    while fila:
        for filho in filhos.get(fila.pop(), []):
            todos.append(filho)
            fila.append(filho)
    return todos


def _baixar_prioridade(pids: list):
    """Reaplica a prioridade baixa em cada thread: com permissão para isso, o Chromium
    sobe a prioridade dos próprios processos (no Linux ela vale por thread)."""
    for pid in pids:
        try:
            tids = [int(t) for t in os.listdir(f"/proc/{pid}/task")]
        except OSError:
            continue
        for tid in tids:
            try:
                if os.getpriority(os.PRIO_PROCESS, tid) < PRIORIDADE_NAVEGADOR:
                    os.setpriority(os.PRIO_PROCESS, tid, PRIORIDADE_NAVEGADOR)
            except (OSError, AttributeError):
                pass


def _processo_filho(pasta: str, visivel: bool, leve: bool, registrar_rede: bool):
    if hasattr(os, "nice"):
        try:
            os.nice(PRIORIDADE_NAVEGADOR)  # o Chromium herda; o pai reaplica se ele mudar
        except OSError:
            pass
    entrada = os.path.join(pasta, "estado_entrada.json")
    estado = None
    if os.path.exists(entrada):
        with open(entrada, encoding="utf-8") as f:
            estado = json.load(f)
    try:
        caminho, estado = baixar_relatorio(os.getenv("ANGELLIRA_USUARIO", ""), os.getenv("ANGELLIRA_SENHA", ""),
                                           pasta, estado, visivel, leve, registrar_rede)
        resultado = {"caminho": caminho, "estado": estado}
    except ErroAngellira as e:
        resultado = {"erro": str(e), "estado": e.estado}
    except Exception as e:
        resultado = {"erro": f"{type(e).__name__}: {e}"[:500]}
    with open(os.path.join(pasta, "resultado.json"), "w", encoding="utf-8") as f:
        json.dump(resultado, f)


class AtualizadorAngellira:
    def __init__(self, get_client, spreadsheet_id: str):
        self._get_client = get_client
        self._spreadsheet_id = spreadsheet_id
        self._estado = None        # cookies do login, só em memória
        self._falhas = 0
        self._leve = os.getenv("ANGELLIRA_MODO_LEVE", "1") != "0"
        self._rede_registrada = False
        self.ultimo_ok = None
        self.ultimo_erro = ""

    def atualizar(self, visivel=False) -> int:
        usuario, senha = os.getenv("ANGELLIRA_USUARIO", ""), os.getenv("ANGELLIRA_SENHA", "")
        if not usuario or not senha:
            raise ErroAngellira("defina ANGELLIRA_USUARIO e ANGELLIRA_SENHA")
        inicio = time.monotonic()
        with tempfile.TemporaryDirectory() as pasta:
            try:
                caminho, self._estado = baixar_em_processo(usuario, senha, pasta, self._estado, visivel,
                                                           leve=self._leve, registrar_rede=not self._rede_registrada)
                self._rede_registrada = True
            except ErroAngellira as e:
                if self._leve and "botão de exportar não apareceu" in str(e):
                    self._leve = False
                    logging.warning("Angellira: o Tracking não abriu sem o mapa e os scripts de terceiros; "
                                    "voltando ao modo completo.")
                # Se o login deu certo e a falha foi depois, reaproveita a sessão: cada login
                # novo passa pelo PERMITIR e pesa mais. A cada 3 falhas seguidas, login do
                # zero, caso a própria sessão seja a causa.
                self._falhas += 1
                self._estado = e.estado if e.estado and self._falhas % 3 else None
                raise
            self._falhas = 0
            linhas = ler_arquivo(caminho)
            try:
                with open(os.path.join(pasta, ARQ_DIAGNOSTICO), encoding="utf-8") as f:
                    diagnostico = f.read()
            except OSError:
                diagnostico = "sem diagnóstico"
        validar(linhas, f"tela antes de exportar: {diagnostico}; print em /diagnostico/angellira")
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
    if any(t.name == "angellira" for t in threading.enumerate()):
        logging.warning("Angellira: atualização automática já estava ligada; não abro outra.")
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
    ap.add_argument("--processo-filho", help=argparse.SUPPRESS)
    ap.add_argument("--completo", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--registrar-rede", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(message)s")
    if args.processo_filho:
        _processo_filho(args.processo_filho, args.visivel, not args.completo, args.registrar_rede)
        return
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
