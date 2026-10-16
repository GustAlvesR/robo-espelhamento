"""Navegador (Edge via Playwright) e tela ROTA EXTRA: carregar, extrair cards, aceitar e salvar dumps."""
from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from playwright.sync_api import BrowserContext, Page, Playwright
from playwright.sync_api import Error as PWError
from playwright.sync_api import TimeoutError as PWTimeout

from .address_lookup import JS_DOIS_FRAMES, JS_PRELUDIO
from .parser import Rota, normalizar
from .regras import ResultadoAceite
from .seletores import SELETORES

if TYPE_CHECKING:
    from .config import Config

log = logging.getLogger(__name__)

# Sem essas flags o Chromium/Edge reduz timers e renderização de janelas minimizadas ou cobertas,
# o que atrasa o AppSheet quando a janela não está em primeiro plano.
ARGS_EDGE = [
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
    "--disable-features=CalculateNativeWinOcclusion",
]


class ErroSessao(Exception):
    """Tela de login/SSO detectada: a sessão expirou."""


class ErroCarregamento(Exception):
    """A tela de rotas não carregou no tempo esperado."""


@dataclass
class Extracao:
    cards: list[dict] = field(default_factory=list)
    total_botoes: int = 0
    labels_total: int = 0
    botoes_sem_card: int = 0


# --------------------------------------------------------------------------------------------
# JavaScript executado na página (recebe SELETORES como argumento, nada fica fixo aqui)
# --------------------------------------------------------------------------------------------

JS_CARDS = JS_PRELUDIO + r"""
const botoesAceitar = (sel) => {
  for (const s of sel.aceitar) { const l = [...document.querySelectorAll(s)]; if (l.length) return l; }
  return [...document.querySelectorAll('[role="button"]')].filter(b => norm(b.textContent) === sel.aceitar_texto);
};
const cardDe = (botao, botoes, sel) => {
  let comDesc = null, comAcoes = null, comLabel = null;
  for (let el = botao.parentElement; el && el !== document.body; el = el.parentElement) {
    let n = 0;
    for (const b of botoes) if (el.contains(b) && ++n > 1) break;
    if (n > 1) break;
    if (!el.querySelector(sel.card_label)) continue;
    if (el.querySelector(sel.card_descricao)) { comDesc = el; break; }
    if (!comAcoes && sel.card_acoes.some(s => el.querySelector(s))) comAcoes = el;
    if (!comLabel) comLabel = el;
  }
  return comDesc || comAcoes || comLabel;
};
const textoDe = e => (e.innerText || e.textContent || '').trim();
const secaoDe = (card, botoes) => {
  let el = card;
  for (let d = 0; el && el !== document.body && d < 8; d++, el = el.parentElement) {
    for (let s = el.previousElementSibling; s; s = s.previousElementSibling) {
      if (botoes.some(b => s.contains(b))) continue;
      const t = textoDe(s);
      if (t && t.length <= 60 && !t.includes('\n')) return t;
    }
  }
  return null;
};
const dadosCard = (card, sel, botoes) => {
  const labels = [...card.querySelectorAll(sel.card_label)].map(textoDe).filter(Boolean);
  const descs = [...card.querySelectorAll(sel.card_descricao)].map(textoDe).filter(Boolean);
  const lbl = card.querySelector(sel.card_label);
  return {label: labels[0] || '', labels, descricao: descs.join('\n'), texto: textoDe(card),
          secao: secaoDe(card, botoes), chave_dom: lbl ? lbl.id.replace(/^label/, '') : null};
};
"""

JS_EXTRAIR = "(sel) => {" + JS_CARDS + r"""
  const botoes = botoesAceitar(sel);
  const cards = [];
  let semCard = 0;
  for (const b of botoes) {
    const c = cardDe(b, botoes, sel);
    if (!c) { semCard++; continue; }
    cards.push(dadosCard(c, sel, botoes));
  }
  return {cards, total_botoes: botoes.length, labels_total: document.querySelectorAll(sel.card_label).length,
          botoes_sem_card: semCard};
}"""

JS_ROLAR_CARDS = "([sel, topo]) => {" + JS_CARDS + r"""
  const bs = botoesAceitar(sel);
  const el = (bs.length ? rolavel(bs[bs.length - 1], 'y') : null) || document.scrollingElement;
  if (topo) { el.scrollTop = 0; return {moveu: true, altura: el.scrollHeight, botoes: bs.length}; }
  const antes = el.scrollTop;
  el.scrollTop = antes + Math.max(200, el.clientHeight * 0.8);
  return {moveu: el.scrollTop > antes + 1, altura: el.scrollHeight, botoes: bs.length};
}"""

# Lista no fim: espera a carga lazy aumentar a altura ou o número de botões.
JS_LISTA_CRESCEU = "([sel, altura, botoes]) => {" + JS_CARDS + r"""
  const bs = botoesAceitar(sel);
  const el = (bs.length ? rolavel(bs[bs.length - 1], 'y') : null) || document.scrollingElement;
  return el.scrollHeight > altura + 1 || bs.length > botoes;
}"""

# Marca o botão ACEITAR do card que tem o nome e as datas da rota; o clique é feito pelo Playwright.
JS_MARCAR_ALVO = "([sel, alvo]) => {" + JS_CARDS + r"""
  const botoes = botoesAceitar(sel);
  const achados = [];
  for (const b of botoes) {
    const c = cardDe(b, botoes, sel);
    if (!c) continue;
    const d = dadosCard(c, sel, botoes);
    const tudo = norm(d.descricao + ' ' + d.texto);
    if (!d.labels.some(l => norm(l) === norm(alvo.nome))) continue;
    if (alvo.eta && !tudo.includes(norm(alvo.eta))) continue;
    if (alvo.cpt && !tudo.includes(norm(alvo.cpt))) continue;
    achados.push(b);
  }
  if (!achados.length) return {status: 'nao_encontrada', n: 0};
  achados[0].setAttribute('data-monitor-alvo', alvo.token);
  achados[0].scrollIntoView({block: 'center'});
  return {status: 'ok', n: achados.length};
}"""

# Após o clique: 'dialogo' se abriu confirmação, 'sumiu' se o card não tem mais ACEITAR disponível.
JS_POS_CLIQUE = "([sel, alvo, ignorarDialogo]) => {" + JS_CARDS + r"""
  const dlg = [...document.querySelectorAll(sel.dialogo)].find(d => d.getBoundingClientRect().height > 0);
  if (dlg && !ignorarDialogo) return 'dialogo';
  const botoes = botoesAceitar(sel);
  for (const b of botoes) {
    const c = cardDe(b, botoes, sel);
    if (!c) continue;
    const d = dadosCard(c, sel, botoes);
    const tudo = norm(d.descricao + ' ' + d.texto);
    if (d.labels.some(l => norm(l) === norm(alvo.nome)) && (!alvo.eta || tudo.includes(norm(alvo.eta)))
        && (!alvo.cpt || tudo.includes(norm(alvo.cpt)))) return null;
  }
  return 'sumiu';
}"""


# --------------------------------------------------------------------------------------------
# Navegador
# --------------------------------------------------------------------------------------------

def abrir_contexto(pw: Playwright, cfg: "Config", forcar_visivel: bool = False) -> BrowserContext:
    """Abre o Edge instalado com perfil persistente (mantém o login SSO entre execuções)."""
    nav = cfg.navegador
    perfil = cfg.caminho(nav.perfil_dir)
    perfil.mkdir(parents=True, exist_ok=True)
    kwargs: dict = dict(
        channel=nav.canal or None, headless=False if forcar_visivel else nav.headless, args=ARGS_EDGE,
        viewport={"width": nav.largura, "height": nav.altura}, locale="pt-BR", timezone_id=cfg.monitor.fuso,
        accept_downloads=False,
    )
    if nav.proxy:
        kwargs["proxy"] = {"server": nav.proxy}
    log.info("Abrindo navegador (canal=%s, headless=%s, perfil=%s)", nav.canal, kwargs["headless"], perfil)
    ctx = pw.chromium.launch_persistent_context(str(perfil), **kwargs)
    ctx.set_default_timeout(nav.timeout_carregamento_s * 1000)
    ctx.set_default_navigation_timeout(nav.timeout_navegacao_s * 1000)
    return ctx


def pagina_principal(ctx: BrowserContext) -> Page:
    return ctx.pages[0] if ctx.pages else ctx.new_page()


def detectar_login(page: Page, cfg: "Config") -> bool:
    """True se a página atual parece uma tela de login/SSO."""
    url = page.url.lower()
    if any(ind.lower() in url for ind in cfg.monitor.indicadores_login_url):
        return True
    if page.locator(SELETORES["campos_login"]).count() > 0:
        return True
    textos = [normalizar(t) for t in cfg.monitor.indicadores_login_texto]
    corpo = normalizar(page.evaluate("() => document.body ? document.body.innerText.slice(0, 5000) : ''"))
    return any(t and t in corpo for t in textos)


def _mesmo_documento(a: str, b: str) -> bool:
    return a.split("#")[0].rstrip("/") == b.split("#")[0].rstrip("/")


def carregar_tela_rotas(page: Page, cfg: "Config") -> None:
    """Recarrega a tela ROTA EXTRA e espera os cards (ou a tela vazia) aparecerem."""
    nav = cfg.navegador
    url = cfg.app.url_rotas
    if _mesmo_documento(page.url, url):
        if page.url != url:
            page.goto(url, wait_until="domcontentloaded")
        page.reload(wait_until="domcontentloaded")
    else:
        page.goto(url, wait_until="domcontentloaded")

    if detectar_login(page, cfg):
        raise ErroSessao(f"Tela de login detectada em {page.url.split('?')[0]}")
    pronto = f'{SELETORES["aceitar"][0]}, {SELETORES["breadcrumb_atual"]}, {SELETORES["card_label"]}'
    try:
        page.wait_for_selector(pronto, timeout=nav.timeout_carregamento_s * 1000)
    except PWTimeout as e:
        if detectar_login(page, cfg):
            raise ErroSessao(f"Tela de login detectada em {page.url.split('?')[0]}") from e
        raise ErroCarregamento(f"Tela de rotas não carregou em {nav.timeout_carregamento_s}s") from e
    try:
        page.wait_for_load_state("networkidle", timeout=nav.espera_rede_s * 1000)
    except PWTimeout:
        log.debug("Rede não ficou ociosa; seguindo com o que já carregou")
    try:
        page.wait_for_selector(SELETORES["aceitar"][0], timeout=nav.espera_cards_s * 1000)
    except PWTimeout:
        log.debug("Nenhum botão ACEITAR após %ss (lista possivelmente vazia)", nav.espera_cards_s)


def _aguardar_render(page: Page) -> None:
    page.evaluate(JS_DOIS_FRAMES)
    try:
        page.wait_for_load_state("networkidle", timeout=1000)
    except PWTimeout:
        pass


def _rolar_lista(page: Page, cfg: "Config") -> bool:
    """Rola a lista um passo. No fim, só segue se a carga lazy trouxer mais conteúdo."""
    r = page.evaluate(JS_ROLAR_CARDS, [SELETORES, False])
    if not r["moveu"]:
        try:
            page.wait_for_function(JS_LISTA_CRESCEU, arg=[SELETORES, r["altura"], r["botoes"]],
                                   timeout=cfg.navegador.espera_lazy_ms)
        except PWTimeout:
            return False
    _aguardar_render(page)
    return True


def extrair_cards(page: Page, cfg: "Config") -> Extracao:
    """Rola a lista para renderizar cards lazy/virtualizados e junta tudo sem duplicar."""
    vistos: dict[str, dict] = {}
    ext = Extracao()
    for _ in range(cfg.navegador.max_rolagens):
        res = page.evaluate(JS_EXTRAIR, SELETORES)
        ext.total_botoes = max(ext.total_botoes, res["total_botoes"])
        ext.labels_total = max(ext.labels_total, res["labels_total"])
        ext.botoes_sem_card = max(ext.botoes_sem_card, res["botoes_sem_card"])
        for c in res["cards"]:
            vistos.setdefault(f'{normalizar(c["label"])}|{normalizar(c["descricao"])}', c)
        if not _rolar_lista(page, cfg):
            break
    page.evaluate(JS_ROLAR_CARDS, [SELETORES, True])
    ext.cards = list(vistos.values())
    if ext.botoes_sem_card:
        log.warning("%d botão(ões) ACEITAR sem card identificável (layout mudou?)", ext.botoes_sem_card)
    return ext


# --------------------------------------------------------------------------------------------
# Auto-aceite
# --------------------------------------------------------------------------------------------

def aceitar_rota(page: Page, rota: Rota, cfg: "Config", pasta_evidencias: Path) -> ResultadoAceite:
    """Clica em ACEITAR no card exato (nome + ETA + CPT), reavaliando o DOM antes e depois do clique."""
    alvo = {"nome": rota.nome, "eta": rota.eta_texto, "cpt": rota.cpt_texto, "token": uuid.uuid4().hex[:12]}
    seletor_alvo = f'[data-monitor-alvo="{alvo["token"]}"]'

    ultimo_erro = ""
    for _ in range(3):
        if not _marcar_alvo(page, alvo, cfg):
            return ResultadoAceite("nao_encontrada", "card não encontrado no DOM atual")
        try:
            # Timeout no click = nenhum clique foi disparado (o Playwright só clica após as checagens).
            page.locator(seletor_alvo).click(timeout=3000)
            break
        except PWError as e:
            ultimo_erro = str(e).splitlines()[0][:150]
            log.info("ACEITAR marcado ficou indisponível (re-render do app?); remarcando: %s", ultimo_erro)
    else:
        return ResultadoAceite("erro", f"botão ACEITAR não ficou clicável após 3 tentativas: {ultimo_erro}")
    log.warning("AUTO-ACEITE: clique em ACEITAR para %s", rota.chave)
    try:
        res = _confirmar_aceite(page, alvo, cfg)
    except PWError as e:
        res = ResultadoAceite("nao_confirmada", f"erro após o clique: {str(e)[:200]}", True)
    return _com_evidencia(page, pasta_evidencias, rota, res)


def _marcar_alvo(page: Page, alvo: dict, cfg: "Config") -> bool:
    page.evaluate(JS_ROLAR_CARDS, [SELETORES, True])
    for _ in range(cfg.navegador.max_rolagens):
        marcado = page.evaluate(JS_MARCAR_ALVO, [SELETORES, alvo])
        if marcado["status"] == "ok":
            if marcado["n"] > 1:
                log.warning("Há %d cards idênticos para %s; aceitando o primeiro", marcado["n"], alvo["nome"])
            _aguardar_render(page)
            return True
        if not _rolar_lista(page, cfg):
            return False
    return False


def _confirmar_aceite(page: Page, alvo: dict, cfg: "Config") -> ResultadoAceite:
    aa = cfg.auto_aceite
    timeout = aa.timeout_confirmacao_s * 1000
    try:
        estado = page.wait_for_function(JS_POS_CLIQUE, arg=[SELETORES, alvo, False], timeout=timeout).json_value()
    except PWTimeout:
        return ResultadoAceite("nao_confirmada", "card continua com ACEITAR após o clique", True)
    if estado == "dialogo":
        if not aa.confirmar_dialogo:
            return ResultadoAceite("dialogo_pendente",
                                   "AppSheet abriu confirmação e confirmar_dialogo=false; conclua manualmente", True)
        nomes = "|".join(re.escape(t) for t in aa.textos_confirmacao)
        botao = page.locator(SELETORES["dialogo"]).get_by_role("button", name=re.compile(rf"^\s*({nomes})\s*$", re.I))
        try:
            botao.first.click(timeout=5000)
            page.wait_for_function(JS_POS_CLIQUE, arg=[SELETORES, alvo, True], timeout=timeout)
        except PWTimeout:
            return ResultadoAceite("nao_confirmada", "diálogo não confirmado ou card ainda ativo", True)
    return ResultadoAceite("aceita_provavel", "card saiu da lista após o clique (confira no app)", True)


def _com_evidencia(page: Page, pasta: Path, rota: Rota, res: ResultadoAceite) -> ResultadoAceite:
    try:
        pasta.mkdir(parents=True, exist_ok=True)
        nome = re.sub(r"[^A-Za-z0-9._-]+", "_", f"{datetime.now():%Y%m%d_%H%M%S}_{rota.nome}")[:120]
        caminho = pasta / f"{nome}.png"
        page.screenshot(path=str(caminho), full_page=True)
        res.screenshot = str(caminho)
    except Exception:
        log.exception("Não consegui salvar a evidência do auto-aceite")
    return res


# --------------------------------------------------------------------------------------------
# Diagnóstico
# --------------------------------------------------------------------------------------------

def salvar_dump(page: Page, pasta: Path, extracao: Extracao | None = None) -> Path:
    """Salva HTML, screenshot e cards extraídos do ciclo atual."""
    destino = pasta / datetime.now().strftime("%Y%m%d_%H%M%S")
    destino.mkdir(parents=True, exist_ok=True)
    (destino / "pagina.html").write_text(page.content(), encoding="utf-8")
    page.screenshot(path=str(destino / "pagina.png"), full_page=True)
    if extracao is not None:
        (destino / "cards.json").write_text(json.dumps(extracao.__dict__, ensure_ascii=False, indent=2),
                                            encoding="utf-8")
    log.info("Dump salvo em %s", destino)
    return destino
