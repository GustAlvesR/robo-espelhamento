"""Tabela de endereços: leitura no AppSheet, agrupamento de células em linhas, cache e cruzamento com a origem.

As funções de agrupamento são puras (testáveis sem navegador). A estratégia de linhas fica isolada
em `agrupar_linhas` para ser ajustada depois da saída do --discover-table.
"""
from __future__ import annotations

import json
import logging
import os
import re
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from .parser import normalizar
from .seletores import SELETORES, TEXTOS

if TYPE_CHECKING:
    from playwright.sync_api import Page

    from .config import Config, HubCfg, TabelaCfg

log = logging.getLogger(__name__)

COL_ID = "ID"
COL_ENDERECO = "Endereço"
COL_MICRO = "MICRO REGIÃO"
COL_LOCALIZACAO = "Localização Fixa"
COL_POSSUI = "POSSUI RESTRIÇÃO"
COL_PERFIL = "PERFIL RESTRIÇÃO"
COL_MOTIVO = "MOTIVO RESTRIÇÃO"
COL_RESTRICAO_APP = "Restrição APP Sheet"
_COLS_EQUIVALENCIA = (COL_ENDERECO, COL_POSSUI, COL_PERFIL, COL_MOTIVO, COL_RESTRICAO_APP)

SEVERIDADE = {"OK": 0, "VERIFICAR": 1, "ALERTA": 2, "INCOMPATIVEL": 3}


def norm_titulo(texto: str | None) -> str:
    """Normalização usada para comparar títulos de coluna (ignora acento, caixa e '?'/':' finais)."""
    return normalizar(texto).rstrip("?:").strip()


# --------------------------------------------------------------------------------------------
# Agrupamento de células (puro)
# --------------------------------------------------------------------------------------------

@dataclass
class Celula:
    texto: str
    x: float
    x2: float
    vy: float
    y: float
    h: float = 0.0
    ordem: int = 0
    linha_y: float | None = None

    @property
    def cx(self) -> float:
        return (self.x + self.x2) / 2


@dataclass
class Cabecalho:
    titulo: str
    x: float
    x2: float
    vy: float

    @property
    def cx(self) -> float:
        return (self.x + self.x2) / 2


@dataclass
class LinhaTabela:
    y: float
    valores: dict[str, str | None]
    problemas: list[str] = field(default_factory=list)

    def preenchidas(self) -> int:
        return sum(1 for v in self.valores.values() if v is not None)


def escolher_cabecalhos(candidatos: list[Cabecalho], tol: float) -> dict[str, Cabecalho]:
    """Escolhe, entre os textos que batem com títulos, os que estão na faixa Y com mais títulos distintos."""
    if not candidatos:
        return {}
    melhor_faixa, melhor_qtd = candidatos[0].vy, 0
    for c in candidatos:
        qtd = len({o.titulo for o in candidatos if abs(o.vy - c.vy) <= tol})
        if qtd > melhor_qtd:
            melhor_faixa, melhor_qtd = c.vy, qtd
    escolhidos: dict[str, Cabecalho] = {}
    for c in sorted(candidatos, key=lambda c: (abs(c.vy - melhor_faixa), c.x)):
        if abs(c.vy - melhor_faixa) <= tol and c.titulo not in escolhidos:
            escolhidos[c.titulo] = c
    return escolhidos


def agrupar_linhas(celulas: list[Celula], estrategia: str, tol_y: float, n_colunas: int) -> list[list[Celula]]:
    """Agrupa células em linhas. Estratégias: auto | coordenadas | ancestral | blocos.

    - ancestral: usa o Y do elemento de linha (seletor_linha) que contém a célula;
    - coordenadas: agrupa pelo centro Y com tolerância;
    - blocos: fatias de n_colunas na ordem do DOM (só serve se não houver células vazias sem span).
    """
    if not celulas:
        return []
    if estrategia == "auto":
        com_linha = sum(1 for c in celulas if c.linha_y is not None)
        estrategia = "ancestral" if com_linha >= 0.9 * len(celulas) else "coordenadas"
    if estrategia == "blocos":
        ordenadas = sorted(celulas, key=lambda c: c.ordem)
        return [ordenadas[i:i + n_colunas] for i in range(0, len(ordenadas), n_colunas)]
    if estrategia == "ancestral":
        grupos: dict[int, list[Celula]] = {}
        soltas = []
        for c in celulas:
            if c.linha_y is None:
                soltas.append(c)
            else:
                grupos.setdefault(round(c.linha_y), []).append(c)
        resultado = [grupos[k] for k in sorted(grupos)]
        if soltas:
            resultado += agrupar_linhas(soltas, "coordenadas", tol_y, n_colunas)
        return resultado

    resultado = []
    atual: list[Celula] = []
    ref = 0.0
    for c in sorted(celulas, key=lambda c: (c.y, c.x)):
        if atual and abs(c.y - ref) > tol_y:
            resultado.append(atual)
            atual = []
        atual.append(c)
        ref = statistics.fmean(a.y for a in atual)
    if atual:
        resultado.append(atual)
    return resultado


def _coluna_da_celula(cel: Celula, cabecalhos: list[Cabecalho]) -> str | None:
    for h in cabecalhos:
        if h.x <= cel.cx <= h.x2:
            return h.titulo
    for h in cabecalhos:
        if h.x <= cel.x + 2 <= h.x2:
            return h.titulo
    mais_perto = min(cabecalhos, key=lambda h: abs(h.cx - cel.cx))
    if abs(mais_perto.cx - cel.cx) <= 0.75 * max(mais_perto.x2 - mais_perto.x, 1):
        return mais_perto.titulo
    return None


def limpar_valor(texto: str | None, nulos: set[str]) -> str | None:
    """Vazio ou valores configurados como nulos ('Não Aplicável' etc.) viram None."""
    if texto is None:
        return None
    t = re.sub(r"\s+", " ", texto).strip()
    return None if normalizar(t) in nulos else t


def atribuir_colunas(grupo: list[Celula], cabecalhos: dict[str, Cabecalho], colunas: list[str],
                     nulos: set[str]) -> LinhaTabela:
    """Mapeia as células de uma linha para colunas pela posição X dos títulos."""
    valores: dict[str, str | None] = {c: None for c in colunas}
    problemas: list[str] = []
    y = statistics.median(c.linha_y if c.linha_y is not None else c.y for c in grupo)
    ordenadas = sorted(grupo, key=lambda c: c.x)

    if not cabecalhos:
        if len(ordenadas) == len(colunas):
            for col, cel in zip(colunas, ordenadas):
                valores[col] = limpar_valor(cel.texto, nulos)
        else:
            problemas.append(f"sem cabeçalho e {len(ordenadas)} células (esperado {len(colunas)})")
        return LinhaTabela(y=y, valores=valores, problemas=problemas)

    todos_titulos = len(cabecalhos) == len(colunas)
    lista = sorted(cabecalhos.values(), key=lambda h: h.x)
    for cel in ordenadas:
        col = _coluna_da_celula(cel, lista)
        if col is None:
            # Com colunas virtualizadas fora da tela, célula sem título é esperada; ela volta em outra rolagem.
            if todos_titulos:
                problemas.append(f"célula sem coluna: {cel.texto!r} x={cel.x:.0f}")
            continue
        v = limpar_valor(cel.texto, nulos)
        if v is None:
            continue
        if valores[col] is not None and valores[col] != v:
            problemas.append(f"múltiplos valores em '{col}'")
            valores[col] = f"{valores[col]} | {v}"
        else:
            valores[col] = v
    return LinhaTabela(y=y, valores=valores, problemas=problemas)


def processar_snapshot(snapshot: dict, cfg_tab: "TabelaCfg") -> tuple[list[LinhaTabela], dict[str, Cabecalho]]:
    """Converte uma captura do DOM (células + candidatos a título) em linhas."""
    tol = cfg_tab.tolerancia_y_px
    celulas = [Celula(**c) for c in snapshot.get("celulas", [])]
    candidatos = [Cabecalho(**h) for h in snapshot.get("cabecalhos", [])]
    cabecalhos = escolher_cabecalhos(candidatos, tol)
    if cabecalhos:
        faixa = statistics.median(h.vy for h in cabecalhos.values())
        titulos = {norm_titulo(t) for t in cfg_tab.colunas}
        celulas = [c for c in celulas
                   if c.vy >= faixa - tol and not (abs(c.vy - faixa) <= tol and norm_titulo(c.texto) in titulos)]
    nulos = {normalizar(v) for v in cfg_tab.valores_nulos}
    grupos = agrupar_linhas(celulas, cfg_tab.estrategia_linhas, tol, len(cfg_tab.colunas))
    return [atribuir_colunas(g, cabecalhos, cfg_tab.colunas, nulos) for g in grupos], cabecalhos


def mesclar_linhas(acumulado: list[LinhaTabela], novas: list[LinhaTabela], tol: float) -> None:
    """Junta linhas de várias rolagens (mesma linha = mesmo Y de conteúdo) sem duplicar."""
    for nova in novas:
        existente = next((a for a in acumulado if abs(a.y - nova.y) <= tol), None)
        if existente is None:
            acumulado.append(nova)
            continue
        for col, v in nova.valores.items():
            if v is None:
                continue
            atual = existente.valores.get(col)
            if atual is None:
                existente.valores[col] = v
            elif atual != v:
                existente.problemas.append(f"conflito em '{col}': {atual!r} x {v!r}")
        for p in nova.problemas:
            if p not in existente.problemas:
                existente.problemas.append(p)


def validar_linhas(linhas: list[LinhaTabela], cfg_tab: "TabelaCfg") -> list[LinhaTabela]:
    """Descarta linhas com poucas células, loga as suspeitas e a distribuição de colunas preenchidas."""
    n = len(cfg_tab.colunas)
    validas, distribuicao = [], {}
    for ln in sorted(linhas, key=lambda l: l.y):
        qtd = ln.preenchidas()
        if qtd < cfg_tab.min_celulas_linha:
            log.debug("Linha descartada (só %d células) y=%.0f: %s", qtd, ln.y, ln.valores)
            continue
        distribuicao[qtd] = distribuicao.get(qtd, 0) + 1
        if ln.problemas:
            log.warning("Linha suspeita y=%.0f (%d/%d colunas): %s | %s", ln.y, qtd, n,
                        "; ".join(ln.problemas), {k: v for k, v in ln.valores.items() if v})
        validas.append(ln)
    resumo = ", ".join(f"{k}/{n}: {v}" for k, v in sorted(distribuicao.items(), reverse=True))
    log.info("Tabela de endereço: %d linhas válidas (colunas preenchidas -> linhas: %s)", len(validas), resumo)
    return validas


# --------------------------------------------------------------------------------------------
# Cruzamento origem x hub (puro)
# --------------------------------------------------------------------------------------------

@dataclass
class ResultadoHub:
    status: str  # OK | VERIFICAR | ALERTA | INCOMPATIVEL | SEM_MATCH | AMBIGUO | SEM_TABELA
    mensagens: list[str] = field(default_factory=list)
    linha: dict | None = None
    coluna_match: str | None = None
    candidatos: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == "OK"


def valor(linha: dict, coluna: str) -> str | None:
    """Lê uma coluna da linha comparando o título normalizado."""
    if coluna in linha:
        return linha[coluna]
    alvo = norm_titulo(coluna)
    for k, v in linha.items():
        if norm_titulo(k) == alvo:
            return v
    return None


def _tokens(texto: str | None) -> set[str]:
    return {t for t in re.split(r"[^A-Z0-9]+", normalizar(texto)) if t}


def veiculos_no_texto(texto: str | None, conhecidos: list[str]) -> list[str]:
    """Lista os tipos de veículo conhecidos citados no texto (palavra inteira)."""
    n = normalizar(texto)
    achados = []
    for v in conhecidos:
        vn = normalizar(v)
        if re.search(rf"(?<![A-Z0-9]){re.escape(vn)}(?![A-Z0-9])", n):
            achados.append(vn)
    return achados


def avaliar_restricao(linha: dict, veiculo: str | None, cfg: "HubCfg") -> tuple[str, list[str]]:
    """Classifica a compatibilidade do hub com o veículo da rota."""
    possui = normalizar(valor(linha, COL_POSSUI))
    perfil = valor(linha, COL_PERFIL)
    motivo = valor(linha, COL_MOTIVO)
    rest_app = valor(linha, COL_RESTRICAO_APP)
    texto_total = normalizar(" ".join(filter(None, [possui, perfil, motivo, rest_app])))

    status, msgs = "OK", []

    def piorar(novo: str) -> None:
        nonlocal status
        if SEVERIDADE[novo] > SEVERIDADE[status]:
            status = novo

    termos_no_perfil = False
    for termo in cfg.termos_alerta:
        if normalizar(termo) and normalizar(termo) in texto_total:
            piorar("ALERTA")
            msgs.append(f"Hub marcado como '{termo}'")
            termos_no_perfil = termos_no_perfil or normalizar(termo) in normalizar(perfil)

    tem_restricao = possui.startswith("SIM") or (not possui and bool(perfil))
    if not tem_restricao:
        if status == "OK":
            msgs.append("Hub sem restrição")
        return status, msgs

    citados = veiculos_no_texto(perfil, cfg.veiculos_conhecidos)
    if not citados:
        if not termos_no_perfil:
            piorar("VERIFICAR")
            msgs.append(f"Restrição sem veículo identificável (perfil: {perfil or 'vazio'})")
        return status, msgs
    if veiculo is None:
        piorar("VERIFICAR")
        msgs.append(f"Veículo da rota não identificado; hub com restrição: {', '.join(citados)}")
        return status, msgs

    v = normalizar(veiculo)
    proibido = v in citados if cfg.perfil_indica == "proibidos" else v not in citados
    if proibido:
        piorar("INCOMPATIVEL")
        msgs.append(f"Veículo {v} INCOMPATÍVEL com a restrição do hub ({', '.join(citados)})")
    else:
        msgs.append(f"Restrição do hub ({', '.join(citados)}) não afeta {v}: compatível")
    return status, msgs


def buscar_hub(origem: str | None, veiculo: str | None, linhas: list[dict], cfg: "HubCfg") -> ResultadoHub:
    """Cruza a origem com Localização Fixa / Micro Região / Operação (ou mapeamento manual)."""
    if not linhas:
        return ResultadoHub("SEM_TABELA", ["Tabela de endereço ainda não carregada"])
    if not origem:
        return ResultadoHub("SEM_MATCH", ["Origem não identificada no nome da rota"])
    o = normalizar(origem)
    manual = {normalizar(k): v for k, v in cfg.mapeamento_manual.items()}
    if o in manual:
        alvo = normalizar(manual[o])
        achadas = [ln for ln in linhas if normalizar(valor(ln, COL_ID)) == alvo]
        coluna = f"{COL_ID} (mapeamento manual)"
        if not achadas:
            return ResultadoHub("SEM_MATCH", [f"Mapeamento manual {o} -> ID {manual[o]} não existe na tabela"])
    else:
        achadas, coluna = [], None
        for col in cfg.colunas_busca:
            achadas = [ln for ln in linhas if o in _tokens(valor(ln, col))]
            if achadas:
                coluna = col
                break
        if not achadas:
            return ResultadoHub("SEM_MATCH", [f"Origem {o} sem correspondência na tabela de endereço "
                                              f"({', '.join(cfg.colunas_busca)})"])

    distintas: dict[tuple, dict] = {}
    for ln in achadas:
        distintas.setdefault(tuple(normalizar(valor(ln, c)) for c in _COLS_EQUIVALENCIA), ln)
    if len(distintas) > 1:
        return ResultadoHub("AMBIGUO", [f"Origem {o} bate com {len(distintas)} hubs diferentes em '{coluna}'; "
                                        "não foi possível decidir"], coluna_match=coluna,
                            candidatos=list(distintas.values()))
    linha = next(iter(distintas.values()))
    status, msgs = avaliar_restricao(linha, veiculo, cfg)
    if linha.get("_problemas"):
        msgs.append("Linha da tabela marcada como suspeita na leitura; confira")
    return ResultadoHub(status, msgs, linha=linha, coluna_match=coluna)


# --------------------------------------------------------------------------------------------
# Cache em disco
# --------------------------------------------------------------------------------------------

class CacheTabela:
    """Linhas da tabela de endereço persistidas em JSON."""

    def __init__(self, caminho: Path):
        self.caminho = caminho
        self.linhas: list[dict] = []
        self.atualizada_em: datetime | None = None

    def carregar(self) -> None:
        if not self.caminho.is_file():
            return
        try:
            dados = json.loads(self.caminho.read_text(encoding="utf-8"))
            self.linhas = dados["linhas"]
            self.atualizada_em = datetime.fromisoformat(dados["atualizada_em"])
            log.info("Cache da tabela de endereço: %d linhas de %s", len(self.linhas), self.atualizada_em)
        except (OSError, ValueError, KeyError) as e:
            log.warning("Cache da tabela de endereço ilegível (%s); será relido", e)
            self.linhas, self.atualizada_em = [], None

    def salvar(self, linhas: list[dict], agora: datetime) -> None:
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.caminho.with_suffix(".tmp")
        tmp.write_text(json.dumps({"atualizada_em": agora.isoformat(), "linhas": linhas},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.caminho)
        self.linhas, self.atualizada_em = linhas, agora

    def desatualizada(self, agora: datetime, horas: float) -> bool:
        return self.atualizada_em is None or agora - self.atualizada_em > timedelta(hours=horas)


# --------------------------------------------------------------------------------------------
# Navegador (Playwright)
# --------------------------------------------------------------------------------------------

JS_PRELUDIO = r"""
const norm = s => (s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/\s+/g, ' ').trim().toUpperCase();
const normT = s => norm(s).replace(/[?:]+$/, '').trim();
const rolavel = (el, eixo) => {
  for (let e = el && el.parentElement; e && e !== document.body && e !== document.documentElement; e = e.parentElement) {
    const cs = getComputedStyle(e);
    const ov = eixo === 'x' ? cs.overflowX : cs.overflowY;
    if (!/(auto|scroll|overlay)/.test(ov)) continue;
    if (eixo === 'x' ? e.scrollWidth > e.clientWidth + 1 : e.scrollHeight > e.clientHeight + 1) return e;
  }
  return null;
};
const maisComum = (lista, f) => {
  const m = new Map();
  for (const x of lista) { const k = f(x); m.set(k, (m.get(k) || 0) + 1); }
  let melhor = null, n = -1;
  for (const [k, v] of m) if (v > n) { melhor = k; n = v; }
  return melhor;
};
const spansCelula = (sel) => {
  let s = [...document.querySelectorAll(sel.celula_tabela)];
  if (!s.length) s = [...document.querySelectorAll(sel.celula_tabela_fallback)];
  return s;
};
const rolagens = (sel) => {
  const spans = spansCelula(sel);
  return {spans, vs: maisComum(spans, s => rolavel(s, 'y')), hs: maisComum(spans, s => rolavel(s, 'x'))};
};
"""

JS_SNAPSHOT = "([sel, titulos, seletorLinha]) => {" + JS_PRELUDIO + r"""
  const {spans, vs, hs} = rolagens(sel);
  const vOff = vs ? vs.scrollTop - vs.getBoundingClientRect().top : window.scrollY;
  const celulas = [];
  spans.forEach((s, i) => {
    if (vs && !vs.contains(s)) return;
    const r = s.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return;
    let linhaY = null;
    if (seletorLinha) {
      const l = s.closest(seletorLinha);
      if (l) linhaY = l.getBoundingClientRect().top + vOff;
    }
    celulas.push({texto: (s.textContent || '').trim(), x: r.left, x2: r.right, vy: r.top + r.height / 2,
                  y: r.top + r.height / 2 + vOff, h: r.height, ordem: i, linha_y: linhaY});
  });
  const alvo = new Map(titulos.map(t => [normT(t), t]));
  const cabecalhos = [];
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  while (w.nextNode()) {
    const t = normT(w.currentNode.textContent);
    if (!t || !alvo.has(t)) continue;
    let el = w.currentNode.parentElement;
    while (el.parentElement && el.parentElement !== document.body && normT(el.parentElement.textContent) === t) el = el.parentElement;
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    cabecalhos.push({titulo: alvo.get(t), x: r.left, x2: r.right, vy: r.top + r.height / 2});
  }
  const se = document.scrollingElement;
  return {
    celulas, cabecalhos,
    v: vs ? {pos: vs.scrollTop, max: vs.scrollHeight - vs.clientHeight, cliente: vs.clientHeight}
          : {pos: se.scrollTop, max: se.scrollHeight - innerHeight, cliente: innerHeight},
    h: hs ? {pos: hs.scrollLeft, max: hs.scrollWidth - hs.clientWidth, cliente: hs.clientWidth} : null,
  };
}"""

JS_ROLAR_TABELA = "([sel, eixo, pos]) => {" + JS_PRELUDIO + r"""
  const {vs, hs} = rolagens(sel);
  const el = (eixo === 'x' ? hs : vs) || document.scrollingElement;
  if (eixo === 'x') el.scrollLeft = pos; else el.scrollTop = pos;
  return eixo === 'x' ? el.scrollLeft : el.scrollTop;
}"""

JS_DOIS_FRAMES = "() => new Promise(r => requestAnimationFrame(() => requestAnimationFrame(() => r(true))))"

JS_DESCOBRIR = "([sel, alvos]) => {" + JS_PRELUDIO + r"""
  const cadeia = (el) => {
    const out = [];
    for (let e = el; e && e !== document.documentElement; e = e.parentElement) {
      const r = e.getBoundingClientRect();
      const cs = getComputedStyle(e);
      out.push({
        tag: e.tagName.toLowerCase(), role: e.getAttribute('role'), data_testid: e.getAttribute('data-testid'),
        aria_label: e.getAttribute('aria-label'), id_prefixo: (e.id || '').slice(0, 30) || null,
        classes_estaveis: [...e.classList].filter(c => !/jss/i.test(c)).slice(0, 6),
        filhos: e.children.length, spans_celula: e.querySelectorAll(sel.celula_tabela).length,
        texto: (e.innerText || e.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 90),
        x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height),
        overflow: cs.overflowX + '/' + cs.overflowY, display: cs.display,
        rola: e.scrollHeight > e.clientHeight + 1 || e.scrollWidth > e.clientWidth + 1,
      });
    }
    return out;
  };
  const resultado = {alvos: {}};
  for (const alvo of alvos) {
    const n = normT(alvo);
    const achados = [];
    const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    while (w.nextNode()) if (normT(w.currentNode.textContent) === n) achados.push(w.currentNode.parentElement);
    resultado.alvos[alvo] = {ocorrencias: achados.length, cadeias: achados.slice(0, 3).map(cadeia)};
  }
  const q = s => document.querySelectorAll(s).length;
  resultado.contagens = {
    spans_celula: q(sel.celula_tabela), spans_celula_fallback: q(sel.celula_tabela_fallback),
    role_row: q('[role="row"]'), role_cell: q('[role="cell"]'), role_gridcell: q('[role="gridcell"]'),
    role_columnheader: q('[role="columnheader"]'), role_grid: q('[role="grid"]'), role_table: q('[role="table"]'),
    tr: q('tr'), th: q('th'), td: q('td'), table: q('table'),
  };
  const {vs, hs} = rolagens(sel);
  resultado.rolagem = {
    vertical: vs ? {tag: vs.tagName, testid: vs.getAttribute('data-testid'), role: vs.getAttribute('role'),
                    scrollHeight: vs.scrollHeight, clientHeight: vs.clientHeight} : 'janela/nenhuma',
    horizontal: hs ? {tag: hs.tagName, testid: hs.getAttribute('data-testid'), role: hs.getAttribute('role'),
                      scrollWidth: hs.scrollWidth, clientWidth: hs.clientWidth} : 'janela/nenhuma',
  };
  const atual = document.querySelector(sel.breadcrumb_atual);
  resultado.breadcrumb_atual = atual ? (atual.innerText || atual.textContent || '').trim() : null;
  return resultado;
}"""

JS_TELA_ATUAL = "(sel) => { const a = document.querySelector(sel.breadcrumb_atual); return a ? (a.innerText || a.textContent || '') : ''; }"

JS_CLICAR_TEXTO = "([sel, textos]) => {" + JS_PRELUDIO + r"""
  const alvos = new Set(textos.map(norm));
  const el = [...document.querySelectorAll(sel.item_navegacao)].find(e => alvos.has(norm(e.innerText || e.textContent)));
  if (!el) return false;
  el.click();
  return true;
}"""


def _aguardar_render(page: "Page") -> None:
    from playwright.sync_api import TimeoutError as PWTimeout

    page.evaluate(JS_DOIS_FRAMES)
    try:
        page.wait_for_load_state("networkidle", timeout=1000)
    except PWTimeout:
        pass


def _na_tabela(page: "Page") -> bool:
    atual = normalizar(page.evaluate(JS_TELA_ATUAL, SELETORES))
    return any(normalizar(t) in atual for t in TEXTOS["tabela_endereco"])


def abrir_tabela(page: "Page", cfg: "Config") -> None:
    """Navega até a TABELA DE ENDEREÇO (URL direta; senão menu/breadcrumb; senão pin do 1º card)."""
    from playwright.sync_api import TimeoutError as PWTimeout

    timeout = cfg.navegador.timeout_carregamento_s * 1000
    sel_cel = f'{SELETORES["celula_tabela"]}, {SELETORES["celula_tabela_fallback"]}'
    if cfg.app.url_tabela_endereco:
        page.goto(cfg.app.url_tabela_endereco, wait_until="domcontentloaded",
                  timeout=cfg.navegador.timeout_navegacao_s * 1000)
        page.reload(wait_until="domcontentloaded", timeout=cfg.navegador.timeout_navegacao_s * 1000)
    else:
        page.goto(cfg.app.url_rotas, wait_until="domcontentloaded", timeout=cfg.navegador.timeout_navegacao_s * 1000)
        page.wait_for_selector(f'{SELETORES["breadcrumb_atual"]}, {SELETORES["aceitar"][0]}', timeout=timeout)
        if page.evaluate(JS_CLICAR_TEXTO, [SELETORES, TEXTOS["tabela_endereco"]]):
            log.info("Tabela de endereço aberta pelo item de navegação")
        else:
            pin = page.locator(", ".join(SELETORES["abrir_endereco"])).first
            try:
                pin.click(timeout=timeout)
            except PWTimeout as e:
                raise RuntimeError("Não achei como abrir a TABELA DE ENDEREÇO: configure app.url_tabela_endereco") from e
            log.warning("Tabela aberta pelo pin ABRIR ENDEREÇO do 1º card; se essa tela vier filtrada, "
                        "a tabela ficará incompleta. Prefira configurar app.url_tabela_endereco.")
    page.wait_for_selector(sel_cel, timeout=timeout)
    if not _na_tabela(page):
        log.warning("Breadcrumb atual não indica TABELA DE ENDEREÇO (%r); seguindo assim mesmo",
                    page.evaluate(JS_TELA_ATUAL, SELETORES))


def ler_tabela(page: "Page", cfg: "Config") -> list[dict]:
    """Lê a tabela inteira rolando vertical e horizontalmente; retorna linhas como dict coluna -> valor."""
    tab = cfg.hub.tabela
    acumulado: list[LinhaTabela] = []
    page.evaluate(JS_ROLAR_TABELA, [SELETORES, "y", 0])
    page.evaluate(JS_ROLAR_TABELA, [SELETORES, "x", 0])
    _aguardar_render(page)

    snap = page.evaluate(JS_SNAPSHOT, [SELETORES, tab.colunas, tab.seletor_linha])
    _, cabecalhos = processar_snapshot(snap, tab)
    posicoes_h = [0]
    if snap["h"] and len(cabecalhos) < len(tab.colunas):
        h = snap["h"]
        passo_h = max(int(h["cliente"] * 0.8), 100)
        posicoes_h = list(range(0, int(h["max"]) + passo_h, passo_h))
        log.info("Colunas fora do DOM (%d/%d títulos); lendo com rolagem horizontal em %d posições",
                 len(cabecalhos), len(tab.colunas), len(posicoes_h))

    titulos_vistos: set[str] = set()
    for _ in range(tab.max_rolagens):
        for left in posicoes_h:
            if len(posicoes_h) > 1:
                page.evaluate(JS_ROLAR_TABELA, [SELETORES, "x", left])
                _aguardar_render(page)
            snap = page.evaluate(JS_SNAPSHOT, [SELETORES, tab.colunas, tab.seletor_linha])
            linhas, cabecalhos = processar_snapshot(snap, tab)
            titulos_vistos |= set(cabecalhos)
            mesclar_linhas(acumulado, linhas, tab.tolerancia_y_px)
        if len(posicoes_h) > 1:
            page.evaluate(JS_ROLAR_TABELA, [SELETORES, "x", 0])
        v = snap["v"]
        if v["pos"] >= v["max"] - 1:
            break
        novo = page.evaluate(JS_ROLAR_TABELA, [SELETORES, "y", v["pos"] + max(v["cliente"] * 0.8, 100)])
        if novo <= v["pos"]:
            break
        _aguardar_render(page)
    else:
        log.warning("Limite de %d rolagens atingido; a tabela pode estar incompleta", tab.max_rolagens)

    faltando = [c for c in tab.colunas if c not in titulos_vistos]
    if faltando:
        log.warning("Títulos de coluna não encontrados na tela: %s", ", ".join(faltando))
    validas = validar_linhas(acumulado, tab)
    if not validas:
        raise RuntimeError("Nenhuma linha válida lida da tabela de endereço (rode --discover-table)")
    return [{**ln.valores, "_problemas": ln.problemas} if ln.problemas else dict(ln.valores) for ln in validas]


def descobrir_tabela(page: "Page", cfg: "Config", alvos: list[str], pasta: Path) -> dict:
    """Diagnóstico: cadeia de ancestrais dos textos-alvo + resultado do agrupamento atual."""
    pasta.mkdir(parents=True, exist_ok=True)
    resultado = page.evaluate(JS_DESCOBRIR, [SELETORES, alvos])
    snap = page.evaluate(JS_SNAPSHOT, [SELETORES, cfg.hub.tabela.colunas, cfg.hub.tabela.seletor_linha])
    linhas, cabecalhos = processar_snapshot(snap, cfg.hub.tabela)
    resultado["cabecalhos_encontrados"] = {t: {"x": round(h.x), "x2": round(h.x2), "vy": round(h.vy)}
                                          for t, h in cabecalhos.items()}
    resultado["titulos_faltando"] = [c for c in cfg.hub.tabela.colunas if c not in cabecalhos]
    resultado["total_celulas_snapshot"] = len(snap["celulas"])
    resultado["celulas_com_linha_ancestral"] = sum(1 for c in snap["celulas"] if c["linha_y"] is not None)
    resultado["linhas_agrupadas_snapshot"] = len(linhas)
    resultado["amostra_linhas"] = [{"y": round(ln.y), "valores": {k: v for k, v in ln.valores.items() if v},
                                    "problemas": ln.problemas} for ln in linhas[:5]]
    resultado["amostra_celulas"] = snap["celulas"][:40]
    (pasta / "discover_table.json").write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    (pasta / "tabela.html").write_text(page.content(), encoding="utf-8")
    page.screenshot(path=str(pasta / "tabela.png"), full_page=True)
    return resultado


def formatar_descoberta(res: dict) -> str:
    """Texto legível da saída do --discover-table."""
    out = [f"Breadcrumb atual: {res.get('breadcrumb_atual')!r}", f"Contagens: {res.get('contagens')}",
           f"Rolagem: {res.get('rolagem')}"]
    for alvo, dados in res.get("alvos", {}).items():
        out.append(f"\n=== Alvo {alvo!r}: {dados['ocorrencias']} ocorrência(s) ===")
        for i, cadeia in enumerate(dados["cadeias"], 1):
            out.append(f"--- ocorrência {i} (do elemento até <body>) ---")
            for nivel, e in enumerate(cadeia):
                out.append(
                    f"{nivel:>2} <{e['tag']}> role={e['role']} testid={e['data_testid']} aria={e['aria_label']} "
                    f"id^={e['id_prefixo']} classes={e['classes_estaveis']} filhos={e['filhos']} "
                    f"spans={e['spans_celula']} pos=({e['x']},{e['y']} {e['w']}x{e['h']}) "
                    f"overflow={e['overflow']} rola={e['rola']} texto={e['texto'][:60]!r}")
    out.append(f"\nCabeçalhos encontrados: {res.get('cabecalhos_encontrados')}")
    out.append(f"Títulos faltando: {res.get('titulos_faltando')}")
    out.append(f"Células no snapshot: {res.get('total_celulas_snapshot')} "
               f"(com ancestral de linha: {res.get('celulas_com_linha_ancestral')})")
    out.append(f"Linhas agrupadas no snapshot: {res.get('linhas_agrupadas_snapshot')}")
    for ln in res.get("amostra_linhas", []):
        out.append(f"  y={ln['y']}: {ln['valores']} {ln['problemas'] or ''}")
    return "\n".join(out)
