"use strict";

const COR = { OK: "var(--ok)", NOK: "var(--nok)", Pendente: "var(--pend)" };
const corCss = (nome) => getComputedStyle(document.documentElement)
  .getPropertyValue(nome.replace(/var\((.*)\)/, "$1")).trim();

const mapa = L.map("mapa", { zoomControl: true }).setView([-19.5, -45.5], 5);
const sistemaEscuro = matchMedia("(prefers-color-scheme: dark)");
const CHAVE_TEMA = "torreLH.tema";
const temaEscuro = () => {
  const t = document.documentElement.dataset.theme;
  return t ? t === "escuro" : sistemaEscuro.matches;
};
const urlTiles = () => `/tiles/{z}/{x}/{y}.png?tema=${temaEscuro() ? "escuro" : "claro"}`;
const fundo = L.tileLayer(urlTiles(), {
  maxZoom: 18, attribution: "© TomTom",
}).addTo(mapa);

const camadaVeiculos = L.layerGroup().addTo(mapa);
const camadaSel = L.layerGroup().addTo(mapa);

let dados = [];
let selecionada = null;
let semSinal = false;
let ajustouVisao = false;
let visaoAntes = null;

const escHtml = (t) => String(t ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// Rumo (graus a partir do norte, sentido horário) de a para b.
function rumo(a, b) {
  const rad = Math.PI / 180;
  const [la1, lo1, la2, lo2] = [a[0] * rad, a[1] * rad, b[0] * rad, b[1] * rad];
  const y = Math.sin(lo2 - lo1) * Math.cos(la2);
  const x = Math.cos(la1) * Math.sin(la2) - Math.sin(la1) * Math.cos(la2) * Math.cos(lo2 - lo1);
  return (Math.atan2(y, x) / rad + 360) % 360;
}

function distanciaKm(a, b) {
  const rad = Math.PI / 180;
  const h = Math.sin((b[0] - a[0]) * rad / 2) ** 2
    + Math.cos(a[0] * rad) * Math.cos(b[0] * rad) * Math.sin((b[1] - a[1]) * rad / 2) ** 2;
  return 12742 * Math.asin(Math.sqrt(h));
}

function pontoMaisProximo(pts, v) {
  let melhor = 0, dMin = Infinity;
  for (let i = 0; i < pts.length; i++) {
    const d = (pts[i][0] - v[0]) ** 2 + (pts[i][1] - v[1]) ** 2;
    if (d < dMin) { dMin = d; melhor = i; }
  }
  return melhor;
}

// Não há rumo real no relatório da Angellira: aproxima pela direção do trajeto
// previsto no ponto mais próximo do caminhão; sem trajeto, aponta para o destino.
function rumoEstimado(g) {
  const v = g.veiculo;
  const pts = g.pontos || [];
  if (pts.length >= 2) {
    const i = Math.min(pontoMaisProximo(pts, v), pts.length - 2);
    return rumo(pts[i], pts[i + 1]);
  }
  return g.destino ? rumo(v, g.destino) : null;
}

// Também não há trajeto real: o "percorrido" é o trajeto previsto cortado no ponto
// mais próximo do caminhão. Longe demais do trajeto, o corte não diz nada.
const DIST_MAX_CORTE_KM = 30;
function indiceDeCorte(r, g) {
  const pts = g.pontos;
  if (["No destino", "Encerrada"].includes(r["Risco de Atraso"])) return pts.length - 1;
  if (!g.veiculo) return null;
  const i = pontoMaisProximo(pts, g.veiculo);
  return distanciaKm(pts[i], g.veiculo) <= DIST_MAX_CORTE_KM ? i : null;
}

function tamanhoIcone() {
  const z = mapa.getZoom();
  return z <= 5 ? 22 : z <= 8 ? 26 : z <= 11 ? 32 : 38;
}

// Caminhão visto de cima, apontando para o norte (cabine em cima).
function iconeCaminhao(cor, { atraso, cidade, graus }) {
  const t = tamanhoIcone();
  const detalhe = t >= 24
    ? '<rect x="8" y="4.5" width="8" height="3" rx="1" fill="#fff" opacity=".85"/>'
    : "";
  const svg = `<svg viewBox="0 0 24 40" width="${t * 0.6}" height="${t}"
      style="transform: rotate(${graus ?? 0}deg)" aria-hidden="true">
    <rect x="4.5" y="13" width="15" height="25" rx="2" fill="${cor}" stroke="#fff" stroke-width="1.6"/>
    <rect x="6" y="2" width="12" height="10" rx="3.5" fill="${cor}" stroke="#fff" stroke-width="1.6"/>
    ${detalhe}</svg>`;
  const classes = ["caminhao", atraso ? "atraso" : "", cidade ? "cidade" : ""].join(" ");
  return L.divIcon({ className: "", html: `<div class="${classes}" style="width:${t + 8}px;height:${t + 8}px">${svg}</div>`,
    iconSize: [t + 8, t + 8], iconAnchor: [(t + 8) / 2, (t + 8) / 2] });
}

function el(tag, attrs = {}, ...filhos) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const f of filhos) if (f !== null && f !== undefined && f !== "") n.append(f);
  return n;
}

// Estações da Shopee (aba Estacoes): nada aparece até alguém ligar um tipo no
// controle do mapa. A lista vem dentro da página, como as rotas, porque há
// redes que bloqueiam as chamadas em segundo plano (fetch).
const TIPOS_ESTACAO = [
  ["HUB", "HUBs (last mile)", "hub"],
  ["FM HUB", "FM HUBs", "fmh"],
  ["SOC", "SOCs", "soc"],
  ["Outros", "Outras estações", "outra"],
];
const CLASSE_ESTACAO = Object.fromEntries(TIPOS_ESTACAO.map(([t, , c]) => [t, c]));
mapa.createPane("estacoes").style.zIndex = 390;
const camadaEstacoes = L.layerGroup().addTo(mapa);
const tiposLigados = new Set();
let estacoes = [];
try {
  estacoes = JSON.parse(document.getElementById("estacoes-iniciais").textContent) || [];
} catch (e) {
  console.error(e);
}

function desenharEstacoes() {
  camadaEstacoes.clearLayers();
  for (const e of estacoes) {
    if (!tiposLigados.has(e.tipo)) continue;
    L.marker([e.lat, e.lon], {
      pane: "estacoes", keyboard: false,
      icon: L.divIcon({ className: `estacao ${CLASSE_ESTACAO[e.tipo] || "outra"}`, html: "<span></span>",
        iconSize: [14, 14], iconAnchor: [7, 7] }),
    }).bindTooltip(escHtml([e.codigo, e.local || e.nome].filter(Boolean).join(" · "))).addTo(camadaEstacoes);
  }
}

function alternarTipoEstacao(tipo, ligar) {
  if (ligar) tiposLigados.add(tipo);
  else tiposLigados.delete(tipo);
  document.getElementById("avisoEstacoes").textContent = tiposLigados.size && !estacoes.length
    ? "A planilha de estações não respondeu. Recarregue a página em alguns minutos." : "";
  desenharEstacoes();
}

const ControleEstacoes = L.Control.extend({
  options: { position: "topleft" },
  onAdd() {
    const box = el("details", { class: "leaflet-bar controle-estacoes" },
      el("summary", { title: "Mostrar estações da Shopee no mapa" }, "Estações"));
    L.DomEvent.disableClickPropagation(box);
    L.DomEvent.disableScrollPropagation(box);
    const opcoes = el("div", { class: "opcoes" });
    for (const [tipo, rotulo, cls] of TIPOS_ESTACAO) {
      const cb = el("input", { type: "checkbox" });
      cb.checked = tiposLigados.has(tipo);
      cb.addEventListener("change", () => alternarTipoEstacao(tipo, cb.checked));
      opcoes.append(el("label", {}, cb, el("i", { class: `estacao amostra ${cls}` }, el("span")), rotulo));
    }
    opcoes.append(el("span", { class: "aviso", id: "avisoEstacoes", "aria-live": "polite" }));
    box.append(opcoes);
    if (tiposLigados.size) box.open = true;
    return box;
  },
});

const statusDe = (r) => r["Status Geral"] || "Pendente";
const classeStatus = (r) => ({ OK: "ok", NOK: "nok", Cancelada: "canc" }[statusDe(r)] || "pend");
const cancelada = (r) => statusDe(r) === "Cancelada";

function parseBr(texto) {
  const m = /^(\d{2})\/(\d{2})\/(\d{4}) (\d{2}):(\d{2})/.exec(texto || "");
  return m ? new Date(+m[3], +m[2] - 1, +m[1], +m[4], +m[5]) : null;
}

function minutosDesde(texto) {
  const d = parseBr(texto);
  return d ? Math.round((Date.now() - d.getTime()) / 60000) : null;
}

// PRE VIAGEM no status_corrigido do DBLH; sem status, vale o horário de início previsto.
function aguardandoInicio(r) {
  const st = String(r["Status Viagem"] || "").normalize("NFD").replace(/[^A-Za-z]/g, "").toUpperCase();
  if (st) return st === "PREVIAGEM";
  const ini = parseBr(r["Início Previsto"]);
  return ini !== null && Date.now() < ini.getTime();
}

function gravidade(r) {
  if (cancelada(r)) return 4;
  if (r["Espelhamento Atual"] === "NOK") return 0;
  if (statusDe(r) === "NOK") return 1;
  if (r["Risco de Atraso"] === "Atraso provável") return 2;
  return 3;
}

function idade(min) {
  if (min === null || min === undefined) return "";
  if (min < 60) return `${min} min`;
  const h = Math.floor(min / 60);
  return h < 48 ? `${h}h${String(min % 60).padStart(2, "0")}` : `${Math.floor(h / 24)} dias`;
}

const trechoDe = (r) => `${r["Origin Station Code"] || "?"} → ${r["Destination Station Code"] || "?"}`;
const estacaoComLocal = (codigo, local) => (local ? `${codigo || "?"} (${local})` : codigo || "?");

function rotuloTrecho(r) {
  const o = r._origem_local, d = r._destino_local;
  return o || d ? `${o || r["Origin Station Code"] || "?"} → ${d || r["Destination Station Code"] || "?"} · ${trechoDe(r)}`
    : trechoDe(r);
}

// Opções de trecho saem das rotas carregadas. Só remonta quando o conjunto muda,
// para não fechar a lista aberta a cada atualização.
let assinaturaTrechos = "";
let trechoPendente = "";
function desenharTrechos() {
  const sel = document.getElementById("fTrecho");
  const cont = new Map();
  for (const r of dados) {
    const k = trechoDe(r);
    if (!cont.has(k)) cont.set(k, { rotulo: rotuloTrecho(r), n: 0 });
    cont.get(k).n += 1;
  }
  const itens = [...cont.entries()].sort((a, b) => a[1].rotulo.localeCompare(b[1].rotulo, "pt-BR"));
  const assinatura = itens.map(([k, v]) => `${k}:${v.n}`).join("|");
  const desejado = trechoPendente || sel.value;
  trechoPendente = "";
  if (assinatura !== assinaturaTrechos) {
    assinaturaTrechos = assinatura;
    sel.replaceChildren(el("option", { value: "" }, "Trecho: todos"),
      ...itens.map(([k, v]) => el("option", { value: k }, `${v.rotulo} (${v.n})`)));
  }
  sel.value = cont.has(desejado) ? desejado : "";
}

function filtrar() {
  const q = document.getElementById("busca").value.trim().toUpperCase();
  const fs = document.getElementById("fStatus").value;
  const fr = document.getElementById("fRisco").value;
  const ft = document.getElementById("fTrecho").value;
  return dados.filter((r) => {
    if (semSinal && (r["Espelhamento Atual"] !== "NOK" || cancelada(r))) return false;
    if (fs && statusDe(r) !== fs) return false;
    if (fr && r["Risco de Atraso"] !== fr) return false;
    if (ft && trechoDe(r) !== ft) return false;
    if (!q) return true;
    return ["Trip Number", "Placa", "Placa 2", "Origin Station Code", "Destination Station Code", "Cidade/UF",
      "_origem_local", "_destino_local"]
      .some((c) => String(r[c] || "").toUpperCase().includes(q));
  }).sort((a, b) => gravidade(a) - gravidade(b));
}

// Canceladas não pontuam: ficam fora dos KPIs e ganham um contador próprio.
function desenharKpis() {
  const validas = dados.filter((r) => !cancelada(r));
  const cont = (f) => validas.filter(f).length;
  const kpis = [
    ["Rotas", validas.length, "", () => aplicarFiltro()],
    ["NOK", cont((r) => statusDe(r) === "NOK"), "nok", () => aplicarFiltro("fStatus", "NOK")],
    ["Atraso provável", cont((r) => r["Risco de Atraso"] === "Atraso provável"), "alerta",
      () => aplicarFiltro("fRisco", "Atraso provável")],
    ["Sem sinal agora", cont((r) => r["Espelhamento Atual"] === "NOK"), "nok",
      () => aplicarFiltro(null, null, true)],
  ];
  const nCanc = dados.length - validas.length;
  if (nCanc) kpis.push(["Canceladas", nCanc, "canc", () => aplicarFiltro("fStatus", "Cancelada")]);
  const alvo = document.getElementById("kpis");
  alvo.replaceChildren(...kpis.map(([rot, n, cls, fn]) =>
    el("button", { class: `kpi ${cls}`, onclick: fn }, el("b", {}, String(n)), rot)));
}

function aplicarFiltro(id, valor, sinal = false) {
  for (const f of ["fStatus", "fRisco"]) document.getElementById(f).value = "";
  if (id) document.getElementById(id).value = valor;
  semSinal = sinal;
  if (selecionada) selecionar(null);
  else render();
}

function desenharLista(lista) {
  document.getElementById("contagem").textContent =
    lista.length === dados.length ? `${dados.length} rotas` : `${lista.length} de ${dados.length} rotas`;
  document.getElementById("exportar").disabled = !lista.length;
  const ul = document.getElementById("lista");
  if (!lista.length) { ul.replaceChildren(el("li", { class: "vazio" }, "Nenhuma rota com esses filtros.")); return; }
  ul.replaceChildren(...lista.map((r) => {
    const risco = r["Risco de Atraso"] || "";
    const tagCls = risco === "Atraso provável" ? "atraso" : risco === "No prazo" ? "prazo" : "";
    const tag = cancelada(r) ? el("span", { class: "tag", title: r["Cancelamento"] || "" }, "Cancelada")
      : risco ? el("span", { class: `tag ${tagCls}` }, risco) : null;
    return el("li", {
      class: `${classeStatus(r)} ${selecionada === r["Trip Number"] ? "sel" : ""}`,
      onclick: () => selecionar(r["Trip Number"]),
    },
      el("div", { class: "l1" }, el("span", {}, r["Trip Number"]), tag),
      el("div", { class: "l2" },
        `${r["Placa"] || "sem placa"} · ${r["Origin Station Code"] || "?"} → ${r["Destination Station Code"] || "?"}`
        + ` · ${r["% Espelhamento"] || "–"} espelhado`),
    );
  }));
}

// Com uma rota selecionada, só ela fica no mapa. O trajeto só aparece na rota
// selecionada: desenhado para todas, os trechos repetidos viravam uma faixa.
function desenharMapa(lista) {
  camadaVeiculos.clearLayers();
  document.getElementById("verTodos").hidden = !selecionada;
  const visiveis = selecionada ? dados.filter((r) => r["Trip Number"] === selecionada)
    : lista.filter((r) => !cancelada(r));
  const limites = [];
  for (const r of visiveis) {
    const g = r._geo || {};
    const cor = corCss(COR[statusDe(r)] || COR.Pendente);
    const ponto = g.veiculo || g.origem;
    if (!ponto) continue;
    limites.push(ponto);
    const atraso = r["Risco de Atraso"] === "Atraso provável";
    const dica = `${escHtml(r["Trip Number"])} · ${escHtml(r["Placa"])}`;
    let marcador;
    if (g.veiculo) {
      marcador = L.marker(ponto, {
        icon: iconeCaminhao(cor, { atraso, cidade: g.precisao === "cidade", graus: rumoEstimado(g) }),
        riseOnHover: true, keyboard: false,
      });
    } else {
      // Sem posição da Angellira: marca a origem, sem fingir que é o caminhão.
      marcador = L.circleMarker(ponto, {
        radius: 5, color: atraso ? corCss("var(--alerta)") : cor, weight: atraso ? 3 : 2,
        fillColor: cor, fillOpacity: 0.15, bubblingMouseEvents: false,
      });
    }
    marcador.addTo(camadaVeiculos)
      .bindTooltip(g.veiculo ? dica : `${dica} · sem posição (na origem)`)
      .on("click", () => selecionar(r["Trip Number"]));
  }
  if (!ajustouVisao && !selecionada && limites.length) {
    mapa.fitBounds(limites, { padding: [40, 40], maxZoom: 8, animate: false });
    ajustouVisao = true;
  }
}

function linha(rotulo, valor) {
  if (valor === "" || valor === null || valor === undefined) return [];
  return [el("dt", {}, rotulo), el("dd", {}, String(valor))];
}

function desenharDetalhe() {
  const box = document.getElementById("detalhe");
  camadaSel.clearLayers();
  const r = dados.find((x) => x["Trip Number"] === selecionada);
  if (!r) { box.hidden = true; return; }
  const g = r._geo || {};
  desenharTrajeto(r, g);
  const marco = { radius: 6, color: corCss("var(--texto)"), fillOpacity: 1, bubblingMouseEvents: false };
  if (g.origem) L.circleMarker(g.origem, marco).bindTooltip("Origem").addTo(camadaSel);
  if (g.destino) L.circleMarker(g.destino, { ...marco, fillColor: "#fff" }).bindTooltip("Destino").addTo(camadaSel);

  const desvio = r["Desvio vs Fim Previsto (min)"];
  box.replaceChildren(
    el("h3", {}, r["Trip Number"], el("button", { class: "fechar", "aria-label": "Fechar", onclick: () => selecionar(null) }, "×")),
    el("dl", {},
      ...linha("Placa", [r["Placa"], r["Placa 2"]].filter(Boolean).join(" / ")),
      ...linha("Cancelamento", r["Cancelamento"]),
      ...linha("Status viagem", r["Status Viagem"] || (aguardandoInicio(r) ? "Antes do início previsto" : "")),
      ...linha("Origem", estacaoComLocal(r["Origin Station Code"], r._origem_local)),
      ...linha("Destino", estacaoComLocal(r["Destination Station Code"], r._destino_local)),
      ...linha("Início previsto", r["Início Previsto"]),
      ...linha("Fim previsto", r["Fim Previsto"]),
    ),
    el("div", { class: "sec" }, "Espelhamento"),
    el("dl", {},
      ...linha("Status geral", statusDe(r)),
      ...linha("Agora", r["Espelhamento Atual"]),
      ...linha("SLA 1h30", r["SLA 1h30 Cumprido"]),
      ...linha("% espelhado", r["% Espelhamento"]),
      ...linha("Tempo sem sinal", r["Tempo Sem Sinal"]),
      ...linha("Quedas", r["Qtd. Quedas de Sinal"]),
    ),
    el("div", { class: "sec" }, "Rastreamento (Angellira)"),
    el("dl", {},
      ...linha("Situação", r["Rastreamento"]),
      ...linha("Posição", r["Posição Atual"]),
      ...linha("Cidade/UF", r["Cidade/UF"]),
      ...linha("Data da posição", r["Data da Posição"]),
      ...linha("Idade da posição", idade(g.idade_pos_min)),
      ...linha("Precisão no mapa", g.precisao),
      ...linha("Ignição", r["Ignição"]),
      ...linha("Velocidade", r["Velocidade (km/h)"] !== "" ? `${r["Velocidade (km/h)"]} km/h` : ""),
      ...linha("Última mensagem", r["Última Mensagem"]),
    ),
    el("div", { class: "sec" }, "Previsão TomTom"),
    el("dl", {},
      ...linha("Risco", r["Risco de Atraso"]),
      ...linha("Chegada prevista", r["Previsão TomTom"]),
      ...linha("Desvio", desvio !== "" && desvio !== undefined ? `${desvio > 0 ? "+" : ""}${desvio} min` : ""),
      ...linha("Rota TomTom", g.rota_tomtom_min ? idade(g.rota_tomtom_min) : ""),
      ...linha("Planejado", g.planejado_min ? idade(g.planejado_min) : ""),
    ),
  );
  box.hidden = false;
}

function desenharTrajeto(r, g) {
  const pts = g.pontos || [];
  if (pts.length < 2) return;
  const base = { weight: 5, bubblingMouseEvents: false };
  const acento = corCss("var(--acento)");
  const tracar = (trecho, estilo, dica) =>
    L.polyline(trecho, { ...base, ...estilo }).bindTooltip(dica, { sticky: true }).addTo(camadaSel);
  if (aguardandoInicio(r)) {
    tracar(pts, { color: acento, dashArray: "10 8" }, "Trajeto previsto (viagem não iniciada)");
    return;
  }
  const corte = indiceDeCorte(r, g);
  if (corte === null) {
    tracar(pts, { color: acento }, "Trajeto previsto");
    return;
  }
  if (corte > 0) tracar(pts.slice(0, corte + 1), { color: corCss("var(--pend)"), opacity: 0.8 }, "Já percorrido (estimado)");
  if (corte < pts.length - 1) tracar(pts.slice(corte), { color: acento }, "Falta percorrer");
}

function enquadrarSelecionada() {
  const r = dados.find((x) => x["Trip Number"] === selecionada);
  const g = (r && r._geo) || {};
  const pts = [...(g.pontos || []), g.veiculo, g.origem, g.destino].filter(Boolean);
  if (!pts.length) return;
  // No computador o detalhe cobre a direita do mapa; no celular, a parte de baixo.
  const box = document.getElementById("detalhe");
  const celular = innerWidth <= 800;
  mapa.fitBounds(pts, {
    paddingTopLeft: [40, 40],
    paddingBottomRight: celular ? [20, box.offsetHeight + 20] : [box.offsetWidth + 40, 40],
    maxZoom: 12,
    // Um zoom animado em andamento sobrescreve este ao terminar.
    animate: false,
  });
}

function selecionar(trip) {
  if (trip && !selecionada) visaoAntes = { centro: mapa.getCenter(), zoom: mapa.getZoom() };
  selecionada = trip;
  render();
  if (trip) {
    enquadrarSelecionada();
    // No celular o mapa fica acima da lista: leva o mapa à tela, não o item.
    const alvo = innerWidth <= 800 ? document.querySelector(".mapa-area") : document.querySelector("#lista li.sel");
    if (alvo) alvo.scrollIntoView({ block: innerWidth <= 800 ? "start" : "nearest" });
  } else if (visaoAntes) {
    mapa.setView(visaoAntes.centro, visaoAntes.zoom);
    visaoAntes = null;
  } else {
    // Seleção restaurada de um recarregamento: não há visão anterior, enquadra todas.
    ajustouVisao = false;
    desenharMapa(filtrar());
  }
}

function celulaCsv(v) {
  if (v === null || v === undefined) return "";
  let s = String(v);
  // Texto começando com = + - @ vira fórmula no Excel.
  if (typeof v === "string" && /^[=+\-@\t\r]/.test(s) && isNaN(Number(s))) s = "'" + s;
  return /[";\r\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
}

function exportarCsv() {
  const lista = filtrar();
  if (!lista.length) return;
  const colunas = [...new Set(lista.flatMap((r) => Object.keys(r)))].filter((c) => !c.startsWith("_"));
  const linhas = [colunas, ...lista.map((r) => colunas.map((c) => r[c]))]
    .map((l) => l.map(celulaCsv).join(";"));
  // BOM + ";" para o Excel em português abrir com acentos e colunas certos.
  const blob = new Blob(["\ufeff" + linhas.join("\r\n")], { type: "text/csv;charset=utf-8" });
  const d = new Date();
  const p2 = (n) => String(n).padStart(2, "0");
  const a = el("a", {
    href: URL.createObjectURL(blob),
    download: `torre-lh-${d.getFullYear()}${p2(d.getMonth() + 1)}${p2(d.getDate())}-${p2(d.getHours())}${p2(d.getMinutes())}.csv`,
  });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
}

function render() {
  desenharTrechos();
  const lista = filtrar();
  desenharKpis();
  desenharLista(lista);
  desenharMapa(lista);
  desenharDetalhe();
}

let falhasSeguidas = 0;
const CHAVE_VISAO = "torreLH.visao";

const ANGELLIRA_MAX_MIN = 60;

function mostrarAngellira(texto) {
  const span = document.getElementById("angellira");
  const min = minutosDesde(texto);
  const velho = min === null || min > ANGELLIRA_MAX_MIN;
  span.textContent = min === null ? "Angellira: sem relatório"
    : `Angellira: há ${idade(Math.max(0, min))}` + (velho ? " · desatualizado" : "");
  span.title = texto ? `Posição mais recente no relatório: ${texto}` : "";
  span.classList.toggle("velho", velho);
}

function aplicarDados(j) {
  const aviso = document.getElementById("atualizado");
  dados = j.rotas || [];
  if (selecionada && !dados.some((r) => r["Trip Number"] === selecionada)) selecionada = null;
  const tt = j.tomtom || {};
  const idadeCiclo = minutosDesde(j.atualizado_em);
  // Ciclo normal é a cada 5 min; acima de 15 o robô ficou parado (hibernação ou erro).
  const parado = idadeCiclo !== null && idadeCiclo > 15 ? ` · ATENÇÃO: sem ciclo há ${idadeCiclo} min` : "";
  aviso.textContent = `Ciclo: ${j.atualizado_em || "aguardando o primeiro"}` + parado
    + (tt.ultimo_erro ? ` · TomTom: ${tt.ultimo_erro}` : "");
  aviso.classList.toggle("parado", !!parado);
  mostrarAngellira(j.angellira_em);
  try {
    render();
  } catch (e) {
    console.error(e);
    aviso.textContent += ` · Erro ao desenhar: ${e.message}`;
    aviso.classList.add("parado");
  }
}

// Guarda zoom, posição, rota aberta e filtros para sobreviver a um recarregamento.
function salvarVisao() {
  try {
    const c = mapa.getCenter();
    sessionStorage.setItem(CHAVE_VISAO, JSON.stringify({
      lat: c.lat, lng: c.lng, zoom: mapa.getZoom(), sel: selecionada,
      busca: document.getElementById("busca").value,
      fStatus: document.getElementById("fStatus").value,
      fRisco: document.getElementById("fRisco").value, semSinal,
      fTrecho: document.getElementById("fTrecho").value,
      estacoes: [...tiposLigados],
    }));
  } catch (e) { /* armazenamento indisponível: recarrega sem restaurar */ }
}

function restaurarVisao() {
  try {
    const v = JSON.parse(sessionStorage.getItem(CHAVE_VISAO) || "null");
    if (!v) return;
    mapa.setView([v.lat, v.lng], v.zoom);
    ajustouVisao = true;
    selecionada = v.sel || null;
    semSinal = !!v.semSinal;
    for (const id of ["busca", "fStatus", "fRisco"]) document.getElementById(id).value = v[id] || "";
    for (const t of v.estacoes || []) if (CLASSE_ESTACAO[t]) tiposLigados.add(t);
    trechoPendente = v.fTrecho || "";
  } catch (e) { /* ignora estado inválido */ }
}

async function atualizar() {
  try {
    const resp = await fetch("/api/rotas", { credentials: "same-origin", cache: "no-store" });
    if (resp.status === 401) { location.href = "/login"; return; }
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    falhasSeguidas = 0;
    aplicarDados(await resp.json());
    setTimeout(atualizar, 60000);
  } catch (e) {
    console.error(e);
    falhasSeguidas += 1;
    if (falhasSeguidas >= 2) {
      // Rede que bloqueia chamadas em segundo plano: atualiza recarregando a página,
      // que já traz os dados embutidos.
      salvarVisao();
      setTimeout(() => location.reload(), 60000);
      return;
    }
    setTimeout(atualizar, 15000);
  }
}

mapa.on("zoomend", () => desenharMapa(filtrar()));
mapa.on("click", () => { if (selecionada) selecionar(null); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && selecionada) selecionar(null); });
document.getElementById("verTodos").addEventListener("click", () => selecionar(null));
document.getElementById("exportar").addEventListener("click", exportarCsv);
function aplicarTema() {
  fundo.setUrl(urlTiles());
  render();
}
sistemaEscuro.addEventListener("change", aplicarTema);
const seletorTema = document.getElementById("tema");
seletorTema.value = document.documentElement.dataset.theme || "";
seletorTema.addEventListener("change", () => {
  const t = seletorTema.value;
  if (t) document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
  try {
    if (t) localStorage.setItem(CHAVE_TEMA, t);
    else localStorage.removeItem(CHAVE_TEMA);
  } catch (e) { /* sem armazenamento: vale só nesta aba */ }
  aplicarTema();
});

for (const id of ["busca", "fStatus", "fRisco", "fTrecho"]) {
  document.getElementById(id).addEventListener("input", () => { if (id !== "busca") semSinal = false; render(); });
}
restaurarVisao();
new ControleEstacoes().addTo(mapa);
if (tiposLigados.size) alternarTipoEstacao([...tiposLigados][0], true);
try {
  aplicarDados(JSON.parse(document.getElementById("dados-iniciais").textContent));
} catch (e) {
  console.error(e);
}
setTimeout(atualizar, 60000);
