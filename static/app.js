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
  const risco = r["Risco de Atraso"] || "";
  if (risco === "No destino" || risco.startsWith("Encerrada") || risco.startsWith("Chegou")) return pts.length - 1;
  if (!g.veiculo) return null;
  const i = pontoMaisProximo(pts, g.veiculo);
  return distanciaKm(pts[i], g.veiculo) <= DIST_MAX_CORTE_KM ? i : null;
}

function tamanhoIcone() {
  const z = mapa.getZoom();
  return z <= 5 ? 22 : z <= 8 ? 26 : z <= 11 ? 32 : 38;
}

// Caminhão visto de cima, apontando para o norte (cabine em cima).
// Risco de Atraso: prazo vencido sem chegada é fato, não previsão.
const RISCO_ATRASADO = ["Atrasada", "Chegou com atraso", "Encerrada com atraso"];
const RISCO_NO_PRAZO = ["No prazo", "Chegou no prazo", "Encerrada no prazo"];
const classeRisco = (risco) => risco === "Atraso provável" ? "atraso"
  : RISCO_ATRASADO.includes(risco) ? "atrasada" : RISCO_NO_PRAZO.includes(risco) ? "prazo" : "";

function iconeCaminhao(cor, { atraso, vencido, cidade, graus }) {
  const t = tamanhoIcone();
  const detalhe = t >= 24
    ? '<rect x="8" y="4.5" width="8" height="3" rx="1" fill="#fff" opacity=".85"/>'
    : "";
  const svg = `<svg viewBox="0 0 24 40" width="${t * 0.6}" height="${t}"
      style="transform: rotate(${graus ?? 0}deg)" aria-hidden="true">
    <rect x="4.5" y="13" width="15" height="25" rx="2" fill="${cor}" stroke="#fff" stroke-width="1.6"/>
    <rect x="6" y="2" width="12" height="10" rx="3.5" fill="${cor}" stroke="#fff" stroke-width="1.6"/>
    ${detalhe}</svg>`;
  const classes = ["caminhao", vencido ? "atrasada" : atraso ? "atraso" : "", cidade ? "cidade" : ""].join(" ");
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

// Alertas de chuva do INMET (vêm com as rotas a cada atualização), desligados por padrão.
const NIVEL_CLIMA = { 1: "potencial", 2: "perigo", 3: "grande" };
const TEXTO_CHUVA = { 1: "Chuva", 2: "Chuva · perigo", 3: "Chuva · grande perigo" };
mapa.createPane("clima").style.zIndex = 380;
const camadaClima = L.layerGroup().addTo(mapa);
let alertasClima = [];
let erroClima = "";
let climaLidoEm = "";
let climaLigado = false;

function desenharClima() {
  camadaClima.clearLayers();
  const aviso = document.getElementById("avisoClima");
  if (aviso) {
    const lido = climaLidoEm ? ` Leitura do INMET de ${climaLidoEm.slice(0, 16)}.` : "";
    aviso.textContent = !climaLigado ? ""
      : erroClima && climaLidoEm ? `A última consulta ao INMET falhou; mostrando a leitura de ${climaLidoEm.slice(0, 16)}. Nova tentativa em poucos minutos.`
      : erroClima ? `O INMET não respondeu (${erroClima}). Nova tentativa em poucos minutos.`
      : alertasClima.length ? lido.trim() : `Nenhum alerta de chuva vigente.${lido}`;
  }
  if (!climaLigado) return;
  const estilo = getComputedStyle(document.documentElement);
  for (const a of alertasClima) {
    const cor = estilo.getPropertyValue(`--clima-${NIVEL_CLIMA[a.nivel] || "potencial"}`).trim();
    const dica = [`${a.rotulo} · ${a.evento}`, a.inicio && `de ${a.inicio}`, a.fim && `até ${a.fim}`]
      .filter(Boolean).join(" ");
    L.polygon(a.poligono, { pane: "clima", color: cor, weight: 1, fillColor: cor, fillOpacity: 0.18,
      bubblingMouseEvents: false, interactive: true })
      .bindTooltip(escHtml(a.area ? `${dica}\n${a.area}` : dica), { sticky: true, className: "dica-clima" })
      .addTo(camadaClima);
  }
}

const ControleCamadas = L.Control.extend({
  options: { position: "topleft" },
  onAdd() {
    const box = el("details", { class: "leaflet-bar controle-estacoes" },
      el("summary", { title: "Mostrar estações da Shopee e alertas de chuva no mapa" }, "Camadas"));
    L.DomEvent.disableClickPropagation(box);
    L.DomEvent.disableScrollPropagation(box);
    const opcoes = el("div", { class: "opcoes" }, el("b", { class: "grupo" }, "Estações da Shopee"));
    for (const [tipo, rotulo, cls] of TIPOS_ESTACAO) {
      const cb = el("input", { type: "checkbox" });
      cb.checked = tiposLigados.has(tipo);
      cb.addEventListener("change", () => alternarTipoEstacao(tipo, cb.checked));
      opcoes.append(el("label", {}, cb, el("i", { class: `estacao amostra ${cls}` }, el("span")), rotulo));
    }
    opcoes.append(el("span", { class: "aviso", id: "avisoEstacoes", "aria-live": "polite" }));
    const cbClima = el("input", { type: "checkbox" });
    cbClima.checked = climaLigado;
    cbClima.addEventListener("change", () => { climaLigado = cbClima.checked; desenharClima(); });
    opcoes.append(el("b", { class: "grupo" }, "Clima"),
      el("label", {}, cbClima, el("i", { class: "amostra-clima" }), "Alertas de chuva (INMET)"),
      el("span", { class: "aviso", id: "avisoClima", "aria-live": "polite" }));
    box.append(opcoes);
    if (tiposLigados.size || climaLigado) box.open = true;
    return box;
  },
});
const chuvaNoCaminho = (r) => ((r._geo || {}).clima || []).length > 0;

// Rotogramas da Angellira, por trecho "ORIGEM__DESTINO" (vêm com as rotas).
let rotogramas = {};
const chaveTrecho = (r) => `${String(r["Origin Station Code"] || "").toUpperCase()}__${String(r["Destination Station Code"] || "").toUpperCase()}`;
const rotogramaDe = (r) => rotogramas[chaveTrecho(r)] || null;
const alertasRotograma = (r) => (((r._geo || {}).rotograma || {}).alertas || []);
let soChuva = false;

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

// O % da Shopee só conta a partir do início da janela; antes disso o 100% não diz nada.
function janelaNaoIniciada(r) {
  const ini = parseBr(r["Início da Contagem"]);
  return !!ini && ini > new Date();
}
function textoPct(r) {
  if (janelaNaoIniciada(r)) return "janela não iniciada";
  if (!r["% Espelhamento"]) return "– espelhado";
  const agora = r["% Até Agora"];
  return agora && agora !== r["% Espelhamento"] ? `${agora} até agora (${r["% Espelhamento"]} final)` : `${r["% Espelhamento"]} espelhado`;
}

function gravidade(r) {
  if (cancelada(r)) return 4;
  if (acidenteMotorista(r)) return -1;
  if (r["Espelhamento Atual"] === "NOK") return 0;
  if (r["Alerta Saída"]) return 0.5;
  if (statusDe(r) === "NOK") return 1;
  if (acidenteDe(r)) return 1.5;
  if (r["Risco de Atraso"] === "Atrasada") return 2;
  if (r["Risco de Atraso"] === "Atraso provável") return 3;
  return 3.5;
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
    sel.replaceChildren(el("option", { value: "" }, "Trecho"),
      ...itens.map(([k, v]) => el("option", { value: k }, `${v.rotulo} (${v.n})`)));
  }
  sel.value = cont.has(desejado) ? desejado : "";
}

// Busca sem diferenciar acento nem maiúsculas: "joão" acha "JOAO".
const semAcento = (t) => String(t ?? "").normalize("NFD").replace(/[\u0300-\u036f]/g, "").toUpperCase();
const termoBusca = () => semAcento(document.getElementById("busca").value.trim());
const motoristaBate = (r, q) => !!q && semAcento(r["Motorista"]).includes(q);

function filtrar() {
  const q = termoBusca();
  const fs = document.getElementById("fStatus").value;
  const fr = document.getElementById("fRisco").value;
  const ft = document.getElementById("fTrecho").value;
  return dados.filter((r) => {
    if (semSinal && (r["Espelhamento Atual"] !== "NOK" || cancelada(r))) return false;
    if (soChuva && (!chuvaNoCaminho(r) || cancelada(r))) return false;
    if (soRotograma && (!alertasRotograma(r).length || cancelada(r))) return false;
    if (soAcidente && (!acidenteDe(r) || cancelada(r))) return false;
    if (soAlternativa && (!alternativaDe(r) || cancelada(r))) return false;
    if (soNoticia && (!noticiaAtiva(r) || cancelada(r))) return false;
    if (soSaida && (!r["Alerta Saída"] || cancelada(r))) return false;
    if (soGeral && !soGeral.includes(r["Trip Number"])) return false;
    if (fs && statusDe(r) !== fs) return false;
    if (fr && r["Risco de Atraso"] !== fr) return false;
    if (ft && trechoDe(r) !== ft) return false;
    if (!q) return true;
    return ["Trip Number", "Placa", "Placa 2", "Motorista", "Origin Station Code", "Destination Station Code",
      "Cidade/UF", "_origem_local", "_destino_local", "Acionar"]
      .some((c) => semAcento(r[c]).includes(q));
  }).sort((a, b) => gravidade(a) - gravidade(b));
}

// Canceladas não pontuam: ficam fora dos KPIs e ganham um contador próprio.
function desenharKpis() {
  const validas = dados.filter((r) => !cancelada(r));
  const cont = (f) => validas.filter(f).length;
  const kpis = [
    ["Rotas", validas.length, "", () => aplicarFiltro()],
    ["NOK", cont((r) => statusDe(r) === "NOK"), "nok", () => aplicarFiltro("fStatus", "NOK")],
    ["Atrasadas", cont((r) => r["Risco de Atraso"] === "Atrasada"), "nok",
      () => aplicarFiltro("fRisco", "Atrasada")],
    ["Atraso provável", cont((r) => r["Risco de Atraso"] === "Atraso provável"), "alerta",
      () => aplicarFiltro("fRisco", "Atraso provável")],
    ["Sem sinal agora", cont((r) => r["Espelhamento Atual"] === "NOK"), "nok",
      () => aplicarFiltro(null, null, true)],
  ];
  const nAlt = cont((r) => !!alternativaDe(r));
  if (nAlt) kpis.splice(1, 0, ["Rota alternativa p/ GR", nAlt, "desvio", () => aplicarFiltro(null, null, false, false, false, false, true)]);
  const nAcid = cont((r) => !!acidenteDe(r));
  if (nAcid) kpis.splice(1, 0, ["Acidente no caminho", nAcid, "nok", () => aplicarFiltro(null, null, false, false, false, true)]);
  const nSaida = cont((r) => !!r["Alerta Saída"]);
  if (nSaida) kpis.push(["Saída / SM pendente", nSaida, "nok", () => aplicarFiltro(null, null, false, false, false, false, false, false, true)]);
  const nRot = cont((r) => alertasRotograma(r).length > 0);
  if (nRot) kpis.push(["Alerta rotograma", nRot, "nok", () => aplicarFiltro(null, null, false, false, true)]);
  const nNot = cont(noticiaAtiva);
  if (nNot) kpis.push(["Notícia no caminho", nNot, "noticia", () => aplicarFiltro(null, null, false, false, false, false, false, true)]);
  const nChuva = cont(chuvaNoCaminho);
  if (nChuva) kpis.push(["Chuva no caminho", nChuva, "chuva", () => aplicarFiltro(null, null, false, true)]);
  const nCanc = dados.length - validas.length;
  if (nCanc) kpis.push(["Canceladas", nCanc, "canc", () => aplicarFiltro("fStatus", "Cancelada")]);
  const alvo = document.getElementById("kpis");
  // Zerado fica apagado, sem sumir: os indicadores não mudam de lugar a cada ciclo.
  alvo.replaceChildren(...kpis.map(([rot, n, cls, fn]) =>
    el("button", { class: `kpi ${cls} ${n ? "" : "zero"}`, onclick: fn }, el("b", {}, String(n)), rot)));
}

let soRotograma = false;
let soAcidente = false;
let soAlternativa = false;
let soNoticia = false;
let soSaida = false;
let soGeral = null;  // rotas da queda geral escolhida na faixa
let quedasGerais = [];
let contatosNacional = [];
// Rota alternativa calculada pelo robô para a torre levar ao GR (grupo Alinhamento GR).
const alternativaDe = (r) => (r._geo || {}).alternativa || null;
// "Alerta Acidente": o aviso do motorista vem primeiro ("Motorista: ..."); o resto é da TomTom.
const acidenteDe = (r) => r["Alerta Acidente"] || "";
const acidenteMotorista = (r) => acidenteDe(r).startsWith("Motorista:");
// Notícias do Google Notícias ligadas à rota pela cidade citada (o robô busca a cada 15 min).
const noticiasDe = (r) => (r._geo || {}).noticias || [];
// "Pista liberada" aparece na rota, mas não conta no indicador.
const noticiaAtiva = (r) => noticiasDe(r).some((n) => n.tipo !== "liberada");
const textoNoticia = (n) => `${n.tipo} em ${n.cidade}, a ${n.km} km${n.desde || ""} (${n.fonte || "notícia"}, ${n.quando}): ${n.titulo}`;
function aplicarFiltro(id, valor, sinal = false, chuva = false, rot = false, acidente = false, alt = false, noticia = false,
  saida = false) {
  soRotograma = rot;
  soAcidente = acidente;
  soAlternativa = alt;
  soNoticia = noticia;
  soSaida = saida;
  soGeral = null;
  for (const f of ["fStatus", "fRisco"]) document.getElementById(f).value = "";
  if (id) document.getElementById(id).value = valor;
  semSinal = sinal;
  soChuva = chuva;
  if (selecionada) selecionar(null);
  else render();
}

function tagAcidente(r) {
  const texto = acidenteDe(r);
  if (!texto || cancelada(r)) return null;
  const motorista = acidenteMotorista(r);
  const rotulo = motorista ? "Acidente" : /TomTom: acidente/.test(texto) ? "Acidente (TomTom)" : "Interdição (TomTom)";
  return el("span", { class: `tag acidente ${motorista ? "motorista" : "tomtom"}`, title: texto.split("; ").join("\n") }, rotulo);
}

function tagAlternativa(r) {
  const a = alternativaDe(r);
  if (!a || cancelada(r)) return null;
  return el("span", { class: "tag desvio", title: `${a.motivo}\nETA trajeto atual: ${a.chegada_ref}\nETA alternativa: ${a.chegada_alt}` },
    a.economia_min > 0 ? `Desvio −${a.economia_min} min` : "Contorno");
}

// Causa do NOK e quem acionar (robô, pendencias.py): o texto completo fica na dica e no detalhe.
const ROTULO_CAUSA = [[/^Queda geral/, "Queda geral", "geral"], [/^Integração/, "NOK: integração", "integracao"],
  [/^Rastreador/, "NOK: rastreador", "rastreador"], [/^SM no procedimento/, "NOK: SM errada", "sm"],
  [/^Sem SM/, "NOK: sem SM", "sm"]];
function tagCausa(r) {
  const causa = r["Causa NOK"] || "";
  const m = ROTULO_CAUSA.find(([re]) => re.test(causa));
  if (!m || cancelada(r)) return null;
  return el("span", { class: `tag causa ${m[2]}`, title: causa + (r["Acionar"] ? `\nAcionar: ${r["Acionar"]}` : "") }, m[1]);
}

function tagSaida(r) {
  const texto = r["Alerta Saída"] || "";
  if (!texto || cancelada(r)) return null;
  const rotulo = /^CPT/.test(texto) ? "Saída atrasada" : /^Não chegou/.test(texto) ? "Não chegou na origem"
    : /^SM no procedimento/.test(texto) ? "SM errada" : "Sem SM";
  return el("span", { class: "tag saida", title: texto.split("; ").join("\n") + (r["Acionar"] ? `\nAcionar: ${r["Acionar"]}` : "") },
    rotulo);
}

function tagNoticia(r) {
  const ns = noticiasDe(r);
  if (!ns.length || cancelada(r)) return null;
  const ativa = noticiaAtiva(r);
  return el("span", { class: `tag noticia ${ativa ? "" : "liberada"}`, title: ns.map(textoNoticia).join("\n") },
    ativa ? "Notícia" : "Pista liberada");
}

function tagRotograma(r) {
  const alertas = alertasRotograma(r);
  if (!alertas.length || cancelada(r)) return null;
  return el("span", { class: "tag rotograma", title: alertas.join("\n") }, "Rotograma");
}

function tagChuva(r) {
  const avisos = (r._geo || {}).clima || [];
  if (!avisos.length || cancelada(r)) return null;
  const a = avisos.reduce((m, x) => ((x.nivel || 1) > (m.nivel || 1) ? x : m));
  return el("span", { class: `tag chuva ${NIVEL_CLIMA[a.nivel] || "potencial"}`,
    title: avisos.map((x) => `${x.rotulo}: ${x.evento}${x.fim ? ` até ${x.fim}` : ""}`).join("\n") },
    TEXTO_CHUVA[a.nivel] || "Chuva");
}

// Na lista vão as duas etiquetas mais graves; as outras viram "+N" (todas estão no detalhe).
function tagsResumidas(tags) {
  const todas = tags.filter(Boolean);
  if (todas.length <= 2) return todas;
  const resto = todas.slice(2);
  return [...todas.slice(0, 2), el("span", { class: "tag mais",
    title: resto.map((t) => t.textContent + (t.title ? `: ${t.title}` : "")).join("\n") }, `+${resto.length}`)];
}

function desenharLista(lista) {
  document.getElementById("contagem").textContent =
    lista.length === dados.length ? `${dados.length} rotas` : `${lista.length} de ${dados.length} rotas`;
  document.getElementById("exportar").disabled = !lista.length;
  const ul = document.getElementById("lista");
  if (!lista.length) { ul.replaceChildren(el("li", { class: "vazio" }, "Nenhuma rota com esses filtros.")); return; }
  const q = termoBusca();
  ul.replaceChildren(...lista.map((r) => {
    const risco = r["Risco de Atraso"] || "";
    const tagCls = classeRisco(risco);
    const tag = cancelada(r) ? el("span", { class: "tag", title: r["Cancelamento"] || "" }, "Cancelada")
      : risco ? el("span", { class: `tag ${tagCls}` }, risco) : null;
    return el("li", {
      class: `${classeStatus(r)} ${selecionada === r["Trip Number"] ? "sel" : ""}`,
      onclick: () => selecionar(r["Trip Number"]),
    },
      el("div", { class: "l1" }, el("span", {}, r["Trip Number"]),
        el("span", { class: "tags" }, ...tagsResumidas([tagAcidente(r), tagSaida(r), tagCausa(r), tagAlternativa(r), tagCls === "atrasada" || tagCls === "atraso" ? tag : null,
          cancelada(r) ? tag : null, tagRotograma(r), tagNoticia(r), tagChuva(r), tagCls === "atrasada" || tagCls === "atraso" || cancelada(r) ? null : tag]))),
      el("div", { class: "l2" },
        `${r["Placa"] || "sem placa"} · ${r["Origin Station Code"] || "?"} → ${r["Destination Station Code"] || "?"}`
        + ` · ${textoPct(r)}`),
      // O nome só aparece na lista quando foi ele que a busca encontrou.
      motoristaBate(r, q) ? el("div", { class: "l2" }, `Motorista: ${r["Motorista"]}`) : null,
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
    const vencido = r["Risco de Atraso"] === "Atrasada";
    const dica = `${escHtml(r["Trip Number"])} · ${escHtml(r["Placa"])}`;
    let marcador;
    if (g.veiculo) {
      marcador = L.marker(ponto, {
        icon: iconeCaminhao(cor, { atraso, vencido, cidade: g.precisao === "cidade", graus: rumoEstimado(g) }),
        riseOnHover: true, keyboard: false,
      });
    } else {
      // Sem posição da Angellira: marca a origem, sem fingir que é o caminhão.
      marcador = L.circleMarker(ponto, {
        radius: 5, color: vencido ? corCss("var(--nok)") : atraso ? corCss("var(--alerta)") : cor,
        weight: atraso || vencido ? 3 : 2,
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
  const pendencias = (g.pendencias || []).filter((p) => p.texto);
  const par = g.paradas || {};
  const alertasRota = (acidenteDe(r) ? acidenteDe(r).split("; ").length : 0) + alertasRotograma(r).length
    + noticiasDe(r).filter((n) => n.tipo !== "liberada").length + (g.clima || []).length + (alternativaDe(r) ? 1 : 0);
  const abas = [
    { id: "resumo", rotulo: "Resumo", badge: pendencias.length, conteudo: () => [
      ...(pendencias.length ? secaoPendencias(g) : [el("p", { class: "pend-info" }, "Nenhuma pendência nesta rota.")]),
      el("div", { class: "sec" }, "Viagem"),
      el("dl", {},
        ...linha("Motorista", r["Motorista"] && (r["Fonte Motorista"] && r["Fonte Motorista"] !== "Rastreador"
          ? `${r["Motorista"]} (${r["Fonte Motorista"]})` : r["Motorista"])),
        ...linha("Cancelamento", r["Cancelamento"]),
        ...linha("Status viagem", r["Status Viagem"] || (aguardandoInicio(r) ? "Antes do início previsto" : "")),
        ...linha("Origem", estacaoComLocal(r["Origin Station Code"], r._origem_local)),
        ...linha("Destino", estacaoComLocal(r["Destination Station Code"], r._destino_local)),
        ...linha("Início previsto", r["Início Previsto"]),
        ...linha("Fim previsto", r["Fim Previsto"]),
        ...linha("Chegada prevista", r["Previsão TomTom"] && `${r["Previsão TomTom"]} (${r["Risco de Atraso"] || "sem risco"})`),
        ...linha("Espelhamento", textoPct(r)),
        ...linha("Agora", [r["Cidade/UF"], r["Data da Posição"]].filter(Boolean).join(" às ")),
        ...linha("Tempo parado", par.total_min ? `${hhmm(par.total_min)} (${hhmm(par.indevida_min)} fora de ponto autorizado)` : ""),
      ),
    ] },
    { id: "telemetria", rotulo: "Telemetria", badge: r["Espelhamento Atual"] === "NOK" ? "!" : 0, conteudo: () => [
      el("dl", {},
        ...linha("Status geral", statusDe(r)),
        ...linha("Agora", r["Espelhamento Atual"]),
        ...linha("SLA 1h30", r["SLA 1h30 Cumprido"]),
        ...linha("% espelhado", janelaNaoIniciada(r) ? `Janela não iniciada (começa às ${r["Início da Contagem"]})`
          : r["% Espelhamento"] && `${r["% Espelhamento"]} final (melhor caso)`),
        ...linha("% até agora", janelaNaoIniciada(r) ? "" : r["% Até Agora"]),
        ...linha("Tempo sem sinal", janelaNaoIniciada(r) ? "" : r["Tempo Sem Sinal"]),
        ...linha("Sem sinal desde", r["Sem Sinal Desde"] && (janelaNaoIniciada(r)
          ? `${r["Sem Sinal Desde"]} (a contagem começa às ${r["Início da Contagem"]})` : r["Sem Sinal Desde"])),
        ...linha("Quedas", r["Qtd. Quedas de Sinal"]),
        ...linha("Causa", r["Causa NOK"]),
      ),
    ] },
    { id: "rastreamento", rotulo: "Rastreamento", badge: 0, conteudo: () => [
      el("dl", {},
        ...linha("Situação", r["Rastreamento"]),
        ...linha("Posição", r["Posição Atual"]),
        ...linha("Cidade/UF", r["Cidade/UF"]),
        ...linha("Data da posição", r["Data da Posição"]),
        ...linha("Idade da posição", idade(g.idade_pos_min)),
        ...linha("Precisão no mapa", g.precisao),
        ...linha("Posições guardadas", (g.trilha || []).length || ""),
        ...linha("Ignição", r["Ignição"]),
        ...linha("Velocidade", r["Velocidade (km/h)"] !== "" && r["Velocidade (km/h)"] != null ? `${r["Velocidade (km/h)"]} km/h` : ""),
        ...linha("Tempo parado", r["Tempo Parado"]),
        ...linha("Última mensagem", r["Última Mensagem"]),
      ),
    ] },
    { id: "paradas", rotulo: "Paradas", badge: par.indevida_min ? "!" : 0, conteudo: () => secaoParadas(g) },
    { id: "rota", rotulo: "Rota", badge: alertasRota, conteudo: () => [
      el("dl", {},
        ...linha("Risco", r["Risco de Atraso"]),
        ...linha("Chegada prevista", r["Previsão TomTom"]),
        ...linha("Desvio", desvio !== "" && desvio !== undefined ? `${desvio > 0 ? "+" : ""}${desvio} min` : ""),
        ...linha("Rota TomTom", g.rota_tomtom_min ? idade(g.rota_tomtom_min) : ""),
        ...linha("Caminho até o destino", g.rota_atual_km ? `${g.rota_atual_km} km, calculado às ${g.rota_atual_em}` : ""),
        ...linha("Trânsito no caminho", g.transito_min ? `+${g.transito_min} min` : ""),
        ...linha("Planejado", g.planejado_min ? idade(g.planejado_min) : ""),
      ),
      ...secaoAcidente(r, g),
      ...secaoAlternativa(r, g),
      ...secaoRotograma(r, g),
      ...(g.clima && g.clima.length ? [
        el("div", { class: "sec" }, "Chuva no caminho (INMET)"),
        el("dl", {}, ...g.clima.flatMap((a) => linha(a.rotulo,
          [a.evento, a.inicio && `de ${a.inicio}`, a.fim && `até ${a.fim}`].filter(Boolean).join(" ")))),
      ] : []),
      ...secaoNoticias(g),
    ] },
    { id: "contatos", rotulo: "Contatos", badge: 0, conteudo: () => secaoContatos(r, g) },
  ];
  const ativa = abas.find((a) => a.id === abaDetalhe) || abas[0];
  if (ativa.id === "paradas") desenharParadas(par);
  // A cada atualização (1 min) o painel é refeito: na mesma rota e aba, a rolagem fica.
  const rolagem = box.dataset.trip === r["Trip Number"] && box.dataset.aba === ativa.id ? box.scrollTop : 0;
  const barra = el("div", { class: "abas", role: "tablist" }, ...abas.map((a) => el("button", {
    type: "button", role: "tab", class: `aba ${a === ativa ? "ativa" : ""}`, "aria-selected": String(a === ativa),
    onclick: () => trocarAba(a.id) }, a.rotulo, a.badge ? el("span", { class: "aba-badge" }, String(a.badge)) : null)));
  box.replaceChildren(
    el("div", { class: "detalhe-topo" },
      el("h3", {}, r["Trip Number"], el("button", { class: "fechar", "aria-label": "Fechar", onclick: () => selecionar(null) }, "×")),
      el("div", { class: "detalhe-sub" }, [[r["Placa"], r["Placa 2"]].filter(Boolean).join(" / "), trechoDe(r)].filter(Boolean).join(" · ")),
      barra),
    el("div", { class: "aba-conteudo", role: "tabpanel" }, ...ativa.conteudo()),
  );
  box.dataset.trip = r["Trip Number"];
  box.dataset.aba = ativa.id;
  box.hidden = false;
  box.scrollTop = rolagem;
  prepararBarraAbas(barra);
}

// Barra de abas numa linha só: arrasta com o mouse (clicar e puxar), com a rodinha ou pela
// barrinha de rolagem; no celular, desliza com o dedo. Guarda a posição entre as
// atualizações e sempre deixa a aba aberta à vista.
let rolagemAbas = 0;
function prepararBarraAbas(barra) {
  barra.scrollLeft = rolagemAbas;
  const a = barra.querySelector(".aba.ativa");
  if (a && a.offsetLeft < barra.scrollLeft) barra.scrollLeft = a.offsetLeft - 16;
  else if (a && a.offsetLeft + a.offsetWidth > barra.scrollLeft + barra.clientWidth) {
    barra.scrollLeft = a.offsetLeft + a.offsetWidth - barra.clientWidth + 16;
  }
  const bordas = () => {
    rolagemAbas = barra.scrollLeft;
    barra.classList.toggle("mais-esq", barra.scrollLeft > 2);
    barra.classList.toggle("mais-dir", barra.scrollLeft + barra.clientWidth < barra.scrollWidth - 2);
  };
  bordas();
  barra.addEventListener("scroll", bordas, { passive: true });
  barra.addEventListener("wheel", (e) => {
    if (Math.abs(e.deltaY) <= Math.abs(e.deltaX) || barra.scrollWidth <= barra.clientWidth) return;
    barra.scrollLeft += e.deltaY;
    e.preventDefault();
  }, { passive: false });
  let x0 = null, s0 = 0, arrastou = false;
  barra.addEventListener("pointerdown", (e) => {
    if (e.pointerType !== "mouse" || e.button !== 0) return;
    x0 = e.clientX; s0 = barra.scrollLeft; arrastou = false;
  });
  barra.addEventListener("pointermove", (e) => {
    if (x0 === null) return;
    if (!arrastou && Math.abs(e.clientX - x0) > 4) { arrastou = true; barra.classList.add("arrastando"); }
    if (arrastou) barra.scrollLeft = s0 - (e.clientX - x0);
  });
  const soltar = () => { x0 = null; barra.classList.remove("arrastando"); };
  barra.addEventListener("pointerup", soltar);
  barra.addEventListener("pointerleave", soltar);
  // Quem arrastou não quis clicar na aba em que soltou.
  barra.addEventListener("click", (e) => { if (arrastou) { e.stopPropagation(); arrastou = false; } }, true);
}

// Aba aberta no painel da rota: fica a mesma ao trocar de rota e ao recarregar (neste navegador).
const CHAVE_ABA = "torreLH.aba";
let abaDetalhe = (() => { try { return localStorage.getItem(CHAVE_ABA) || "resumo"; } catch (e) { return "resumo"; } })();
function trocarAba(id) {
  abaDetalhe = id;
  try { localStorage.setItem(CHAVE_ABA, id); } catch (e) { /* sem armazenamento: vale até recarregar */ }
  desenharDetalhe();
}

const hhmm = (min) => `${Math.floor(min / 60)}h${String(Math.round(min % 60)).padStart(2, "0")}`;

// Paradas da viagem (robô, paradas.py): da saída da origem até a chegada, de 10 min ou mais.
function secaoParadas(g) {
  const p = g.paradas;
  if (!p || (!(p.lista || []).length && !p.aberta)) {
    return [el("p", { class: "pend-info" }, "Nenhuma parada registrada nesta viagem. Contam as paradas de 10 min ou mais"
      + " entre a saída da origem e a chegada.")];
  }
  return [
    el("dl", {},
      ...linha("Tempo parado total", hhmm(p.total_min)),
      ...linha("Fora de ponto autorizado", hhmm(p.indevida_min)),
      ...linha("Trânsito e acidente (não contam)", hhmm(p.nao_conta_min)),
    ),
    ...(p.aberta ? [
      el("div", { class: "sec" }, "Parado agora"),
      el("dl", {},
        ...linha("Desde", `${p.aberta.inicio} (${hhmm(p.aberta.min)})`),
        ...linha("Motivo provável", p.aberta.tipo),
        ...linha("Local", [p.aberta.local, p.aberta.cidade].filter(Boolean).join(" · ")),
      ),
    ] : []),
    el("div", { class: "sec" }, "Por motivo"),
    el("dl", {}, ...p.por_tipo.flatMap((t) => linha(t.rotulo, `${t.qtd}x · ${hhmm(t.min)}${t.conta ? "" : " (não conta)"}`))),
    el("div", { class: "sec" }, "Paradas"),
    ...p.lista.map((x) => el("div", { class: `parada ${x.tipo === "Fora de ponto autorizado" ? "indevida" : ""} ${x.conta ? "" : "nao-conta"}` },
      el("div", { class: "parada-topo" }, el("b", {}, x.tipo), el("span", {}, hhmm(x.min))),
      el("span", { class: "pend-info" }, `${x.inicio} → ${x.fim.slice(-5)}`),
      el("span", { class: "pend-info" }, [x.local, x.cidade, x.macro].filter(Boolean).join(" · ")))),
  ];
}

function desenharParadas(p) {
  for (const x of p.lista || []) {
    if (x.lat == null || x.lon == null) continue;
    L.marker([x.lat, x.lon], { keyboard: false,
      icon: L.divIcon({ className: `parada-marca ${x.tipo === "Fora de ponto autorizado" ? "indevida" : x.conta ? "" : "nao-conta"}`,
        html: "<span>P</span>", iconSize: [18, 18], iconAnchor: [9, 9] }) })
      .bindTooltip(`${escHtml(`${x.tipo}: ${hhmm(x.min)}`)}<br>${escHtml(`${x.inicio} → ${x.fim.slice(-5)}`)}<br>${escHtml(x.local || "")}`)
      .addTo(camadaSel);
  }
}

// Já percorrido = posições reais recebidas da Angellira, ligadas em linha reta
// (não há o caminho entre uma posição e outra). Falta percorrer = caminho mais
// rápido da TomTom com o trânsito do momento, recalculado a cada 30 min. Sem
// isso, cai no trajeto planejado cortado no ponto mais próximo do caminhão.
function secaoAcidente(r, g) {
  const avisos = acidenteDe(r) ? acidenteDe(r).split("; ") : [];
  const filas = (g.transito || []).filter((t) => t.tipo === "congestionamento");
  if (!avisos.length && !filas.length) return [];
  return [
    el("div", { class: "sec" }, "Acidentes e trânsito no caminho"),
    el("dl", {},
      ...avisos.flatMap((a) => {
        const [fonte, ...resto] = a.split(": ");
        return linha(fonte, resto.join(": "));
      }),
      ...filas.flatMap((t) => linha("Congestionamento", `a ${t.km} km, +${t.atraso_min} min`)),
    ),
  ];
}

function secaoPendencias(g) {
  const ps = (g.pendencias || []).filter((p) => p.texto);
  if (!ps.length) return [];
  return [
    el("div", { class: "sec" }, "Pendências (quem acionar)"),
    el("dl", {}, ...ps.flatMap((p) => [
      el("dt", {}, p.tipo),
      el("dd", {}, p.texto,
        p.acionar ? el("span", { class: "pend-info" }, `Acionar: ${p.acionar}`) : null,
        p.mensagem ? el("span", { class: "botoes-cobranca" },
          botaoWhats("botao-alt whats cobranca", "WhatsApp", "Abre o WhatsApp com a mensagem pronta: escolha o contato ou o grupo",
            p.mensagem),
          el("button", { type: "button", class: "botao-alt cobranca",
            onclick: (e) => copiarTexto(p.mensagem, e.currentTarget) }, "Copiar cobrança")) : null),
    ])),
  ];
}

// Contatos da Shopee (aba Contatos_Shopee): quem está no turno agora aparece primeiro; os
// outros ficam recolhidos. "No turno" sai da escala da base (12x36 sem dia par/ímpar vale
// só pelo horário).
function itemContato(c) {
  const fone = String(c.telefone || "").replace(/\D/g, "");
  return el("div", { class: `contato ${c.no_turno ? "agora" : ""}` },
    el("div", { class: "contato-nome" }, c.nome, c.no_turno ? el("span", { class: "tag no-turno" }, "no turno") : null),
    el("span", { class: "pend-info" }, [c.funcao, c.horario].filter(Boolean).join(" · ")),
    fone ? el("span", { class: "botoes-cobranca contato-acoes" },
      el("a", { class: "botao-alt cobranca", href: `tel:+55${fone}` }, c.telefone),
      botaoWhats("botao-alt whats cobranca", "WhatsApp", `Abre a conversa com ${c.nome}`, "", fone))
      : el("span", { class: "pend-info" }, "Sem telefone na base"));
}

function blocoContatos(titulo, lista) {
  const agora = lista.filter((c) => c.no_turno);
  const outros = lista.filter((c) => !c.no_turno);
  return [
    el("div", { class: "contato-titulo" }, titulo),
    ...(lista.length ? [] : [el("span", { class: "pend-info" }, "Estação fora da base de contatos")]),
    ...agora.map(itemContato),
    ...(outros.length ? [el("details", { class: "contatos-mais" },
      el("summary", {}, `${agora.length ? "Outros turnos" : "Ninguém no turno agora"} (${outros.length})`),
      ...outros.map(itemContato))] : []),
  ];
}

function secaoContatos(r, g) {
  const [origem, destino] = g.contatos || [];
  if (!origem && !contatosNacional.length) return [];
  return [
    el("div", { class: "sec" }, "Contatos Shopee"),
    ...blocoContatos(`Origem · ${estacaoComLocal(r["Origin Station Code"], r._origem_local)}`, (origem || {}).contatos || []),
    ...blocoContatos(`Destino · ${estacaoComLocal(r["Destination Station Code"], r._destino_local)}`,
      (destino || {}).contatos || []),
    ...(contatosNacional.length ? blocoContatos("Reversa / Monitoramento (nacional)", contatosNacional) : []),
  ];
}

function desenharFaixaGeral() {
  const alvo = document.getElementById("faixa-geral");
  alvo.hidden = !quedasGerais.length;
  alvo.replaceChildren(...quedasGerais.map((q) => el("div", { class: "faixa-item" },
    el("span", {}, el("b", {}, "Queda geral do espelhamento"),
      ` desde ${q.desde}: ${q.rotas.length} rotas sem sinal ao mesmo tempo, ${q.com_posicao} delas com posição recente`
      + " na Angellira. Provável falha da Shopee ou da integração, não dos caminhões. Acionar: Shopee / Angellira."),
    el("button", { type: "button", class: "botao-alt", onclick: () => { aplicarFiltro(); soGeral = q.rotas; render(); } },
      "Ver rotas"),
    botaoWhats("botao-alt whats", "WhatsApp", "Abre o WhatsApp com o aviso pronto: escolha o contato ou o grupo", q.mensagem),
    el("button", { type: "button", class: "botao-alt", onclick: (e) => copiarTexto(q.mensagem, e.currentTarget) },
      "Copiar aviso"))));
}

function secaoNoticias(g) {
  const ns = g.noticias || [];
  if (!ns.length) return [];
  return [
    el("div", { class: "sec" }, "Notícias no caminho (Google Notícias)"),
    el("dl", {}, ...ns.flatMap((n) => [
      el("dt", {}, n.tipo.charAt(0).toUpperCase() + n.tipo.slice(1)),
      el("dd", {}, el("a", { href: n.link, target: "_blank", rel: "noopener" }, n.titulo),
        el("span", { class: "noticia-info" },
          [`${n.cidade}, a ${n.km} km${n.desde || ""}`, n.rodovias && n.rodovias[0], n.fonte, n.quando].filter(Boolean).join(" · "))),
    ])),
  ];
}

function secaoAlternativa(r, g) {
  const a = g.alternativa;
  if (!a) return [];
  const resultado = a.economia_min > 0 ? `economia de ~${a.economia_min} min` : `contorno custa ~${-a.economia_min} min a mais`;
  const copiar = el("button", { type: "button", class: "botao-alt", onclick: (e) => copiarTexto(a.mensagem, e.currentTarget) },
    "Copiar mensagem");
  return [
    el("div", { class: "sec" }, "Rota alternativa (para o GR validar)"),
    el("dl", {},
      ...linha("Motivo", a.motivo),
      ...linha("ETA trajeto atual", `${a.chegada_ref} · ${a.km_ref} km (${a.referencia.replace(/^[oa] /, "")}${a.sm ? `, SM ${a.sm}` : ""})`
        + (a.tipo === "motorista" ? " · sem a retenção do acidente" : "")),
      ...linha("ETA alternativa", `${a.chegada_alt} · ${a.km_alt} km`),
      ...linha("Resultado", resultado),
      ...linha("Calculado às", a.calculado_em),
    ),
    el("div", { class: "botoes-alt" },
      el("a", { class: "botao-alt", href: a.link, target: "_blank", rel: "noopener" }, "Abrir no Google Maps"),
      botaoWhats("botao-alt whats", "Enviar ao GR (WhatsApp)", "Abre o WhatsApp com a mensagem pronta: escolha o grupo Alinhamento GR",
        a.mensagem),
      copiar,
    ),
  ];
}

// WhatsApp: no computador abre o WhatsApp Web, sempre na mesma aba (o site só consegue
// reaproveitar a aba que ele mesmo abriu); no celular, o aplicativo. O menu do usuário
// deixa escolher; a escolha fica neste navegador.
const CHAVE_WHATS = "torreLH.whatsapp";
function modoWhats() {
  try {
    const v = localStorage.getItem(CHAVE_WHATS);
    if (v === "web" || v === "app") return v;
  } catch (e) { /* sem armazenamento: automático */ }
  return matchMedia("(pointer: coarse)").matches ? "app" : "web";
}
function urlWhats(texto, fone) {
  const numero = fone ? `55${fone}` : "";
  const texto_ = texto ? encodeURIComponent(texto) : "";
  if (modoWhats() === "web") {
    return "https://web.whatsapp.com/send?" + [numero && `phone=${numero}`, texto_ && `text=${texto_}`].filter(Boolean).join("&");
  }
  return `https://wa.me/${numero}${texto_ ? `?text=${texto_}` : ""}`;
}
function botaoWhats(classe, rotulo, titulo, texto, fone = "") {
  return el("a", { class: classe, href: urlWhats(texto, fone), target: "_blank", rel: "noopener", title: titulo,
    onclick: (e) => {
      if (modoWhats() !== "web") return;
      e.preventDefault();
      const aba = window.open(urlWhats(texto, fone), "torreLH_whatsapp");
      if (aba) aba.focus();
    } }, rotulo);
}

function copiarTexto(texto, botao) {
  const feito = () => { const t = botao.textContent; botao.textContent = "Copiado!"; setTimeout(() => { botao.textContent = t; }, 1500); };
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(texto).then(feito, () => copiarPorSelecao(texto) && feito());
  } else if (copiarPorSelecao(texto)) feito();
}

function copiarPorSelecao(texto) {
  const area = el("textarea", { readonly: "" });
  area.value = texto;
  area.style.position = "fixed"; area.style.opacity = "0";
  document.body.append(area);
  area.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  area.remove();
  return ok;
}

function desenharTransito(g) {
  const ROTULO = { "acidente": "Acidente", "interdição": "Interdição", "congestionamento": "Congestionamento" };
  for (const t of g.transito || []) {
    L.marker([t.lat, t.lon], { keyboard: false,
      icon: L.divIcon({ className: `incidente ${t.tipo === "congestionamento" ? "fila" : t.tipo === "acidente" ? "acidente" : "interdicao"}`,
        html: `<span><b>${t.tipo === "congestionamento" ? "≈" : t.tipo === "acidente" ? "!" : "×"}</b></span>`,
        iconSize: [18, 18], iconAnchor: [9, 9] }) })
      .bindTooltip(escHtml(`${ROTULO[t.tipo] || t.tipo} (TomTom) a ${t.km} km${t.atraso_min ? `, +${t.atraso_min} min` : ""}`))
      .addTo(camadaSel);
  }
}

function desenharNoticias(g) {
  for (const n of g.noticias || []) {
    L.marker([n.lat, n.lon], { keyboard: false,
      icon: L.divIcon({ className: `noticia-marca ${n.tipo === "liberada" ? "liberada" : ""}`, html: "<span>N</span>",
        iconSize: [18, 18], iconAnchor: [9, 9] }) })
      .bindTooltip(`${escHtml(`Notícia: ${n.tipo} em ${n.cidade} (${n.fonte || "notícia"}, ${n.quando})`)}<br>${escHtml(n.titulo)}`)
      .addTo(camadaSel);
  }
}

function secaoRotograma(r, g) {
  const rot = rotogramaDe(r);
  if (!rot) return [];
  const info = g.rotograma || {};
  const km = rot.km_linha ? `${rot.km_linha} km no mapa` + (rot.km_rotograma ? ` (rotograma: ${Math.round(rot.km_rotograma)} km)` : "") : "";
  const situacao = info.alertas && info.alertas.length ? info.alertas.join("; ")
    : info.desvio_km !== null && info.desvio_km !== undefined ? `Na rota (a ${info.desvio_km} km)` : "";
  return [
    el("div", { class: "sec" }, `Rotograma${rot.sm ? ` (SM ${rot.sm})` : ""}`),
    el("dl", {},
      ...linha("Situação", situacao),
      ...linha("Rota", km || (rot.localizando ? `localizando ${rot.localizando} ponto(s)` : "")),
      ...linha("Pontos autorizados", rot.pontos.length || ""),
      ...linha("Áreas de risco", rot.riscos.length || ""),
    ),
  ];
}

function desenharRotograma(rot) {
  const verde = corCss("var(--ok)");
  for (const [lat, lon, nome, cidade, rodovia, precisao] of rot.pontos || []) {
    L.circleMarker([lat, lon], { radius: 3.5, color: verde, weight: 1.5, fillColor: verde,
      fillOpacity: precisao === "referência" ? 0.9 : 0, dashArray: precisao === "referência" ? null : "2 2",
      bubblingMouseEvents: false })
      .bindTooltip(escHtml([`Ponto autorizado: ${nome}`, cidade, rodovia, precisao === "cidade" ? "(posição da cidade)" : ""]
        .filter(Boolean).join(" · "))).addTo(camadaSel);
  }
  for (const [lat, lon, tipo, nome, cidade, obs, precisao] of rot.riscos || []) {
    L.marker([lat, lon], { keyboard: false,
      icon: L.divIcon({ className: `risco ${precisao === "referência" ? "" : "cidade"}`, html: "<span>!</span>",
        iconSize: [16, 16], iconAnchor: [8, 8] }) })
      .bindTooltip(escHtml([`${tipo}: ${nome}`, cidade, obs, precisao === "cidade" ? "(posição da cidade)" : ""]
        .filter(Boolean).join(" · "))).addTo(camadaSel);
  }
}

function desenharTrajeto(r, g) {
  const rot = rotogramaDe(r);
  if (rot) desenharRotograma(rot);
  desenharTransito(g);
  desenharNoticias(g);
  const linhaRot = rot && rot.linha && rot.linha.length >= 2 ? rot.linha : null;
  const pts = g.pontos || [];
  const trilha = g.trilha || [];
  const atual = g.rota_atual || [];
  const base = { weight: 5, bubblingMouseEvents: false };
  const acento = corCss("var(--acento)");
  const cinza = corCss("var(--pend)");
  const tracar = (trecho, estilo, dica) =>
    L.polyline(trecho, { ...base, ...estilo }).bindTooltip(escHtml(dica), { sticky: true }).addTo(camadaSel);
  const violeta = corCss("var(--rotograma)");
  const dicaRot = `Rota do rotograma${rot && rot.sm ? ` (SM ${rot.sm})` : ""}`;
  if (aguardandoInicio(r)) {
    if (linhaRot) tracar(linhaRot, { color: violeta, dashArray: "10 8" }, `${dicaRot}, viagem não iniciada`);
    else if (pts.length >= 2) tracar(pts, { color: acento, dashArray: "10 8" }, "Trajeto previsto (viagem não iniciada)");
    return;
  }
  if (linhaRot) {
    tracar(linhaRot, { color: violeta, weight: 4, opacity: 0.55 }, dicaRot);
  } else if ((trilha.length || atual.length >= 2) && pts.length >= 2) {
    tracar(pts, { color: cinza, weight: 3, opacity: 0.45, dashArray: "2 7" }, "Trajeto planejado (origem → destino)");
  }
  if (trilha.length) {
    const caminho = [g.origem, ...trilha.map((p) => [p[0], p[1]])].filter(Boolean);
    if (caminho.length >= 2) tracar(caminho, { color: cinza, opacity: 0.9 }, "Já percorrido: posições recebidas da Angellira");
    for (const [lat, lon, desde, ate, precisao] of trilha) {
      const quando = desde === ate ? desde : `${desde} até ${ate}`;
      L.circleMarker([lat, lon], { radius: 3.5, color: "#fff", weight: 1, fillColor: cinza, fillOpacity: 1,
        bubblingMouseEvents: false }).bindTooltip(escHtml(precisao ? `${quando} · ${precisao}` : quando)).addTo(camadaSel);
    }
  }
  if (atual.length >= 2) {
    tracar(atual, { color: acento }, `Melhor caminho agora (TomTom com trânsito, calculado às ${g.rota_atual_em || "?"})`);
    // Por cima da azul: na sugestão da TomTom as duas coincidem.
    if (g.alternativa && (g.alternativa.pontos || []).length >= 2) {
      tracar(g.alternativa.pontos, { color: corCss("var(--desvio)"), weight: 6, dashArray: "10 7" },
        `Rota alternativa para o GR validar (${g.alternativa.economia_min > 0 ? `economia de ~${g.alternativa.economia_min} min`
          : `contorno +${-g.alternativa.economia_min} min`})`);
    }
    return;
  }
  if (pts.length < 2) return;
  const corte = indiceDeCorte(r, g);
  if (corte === null) {
    if (!trilha.length) tracar(pts, { color: acento }, "Trajeto previsto");
    return;
  }
  if (corte > 0 && !trilha.length) tracar(pts.slice(0, corte + 1), { color: cinza, opacity: 0.8 }, "Já percorrido (estimado)");
  if (corte < pts.length - 1) tracar(pts.slice(corte), { color: acento }, "Falta percorrer (trajeto previsto)");
}

function enquadrarSelecionada() {
  const r = dados.find((x) => x["Trip Number"] === selecionada);
  const g = (r && r._geo) || {};
  const rot = r && rotogramaDe(r);
  const pts = [...(g.pontos || []), ...(g.rota_atual || []), ...(g.trilha || []).map((p) => [p[0], p[1]]),
    ...((g.alternativa && g.alternativa.pontos) || []),
    ...((rot && rot.linha) || []),
    g.veiculo, g.origem, g.destino].filter(Boolean);
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
  alertasClima = j.clima || [];
  rotogramas = j.rotogramas || {};
  quedasGerais = j.queda_geral || [];
  contatosNacional = j.contatos_nacional || [];
  desenharFaixaGeral();
  erroClima = j.clima_erro || "";
  climaLidoEm = j.clima_lido_em || "";
  desenharClima();
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
      estacoes: [...tiposLigados], clima: climaLigado, soChuva, soRotograma, soAcidente, soAlternativa,
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
    climaLigado = !!v.clima;
    soChuva = !!v.soChuva;
    soRotograma = !!v.soRotograma;
    soAcidente = !!v.soAcidente;
    soAlternativa = !!v.soAlternativa;
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
  desenharClima();
}
sistemaEscuro.addEventListener("change", aplicarTema);
// Menu do usuário fecha ao clicar fora; a legenda lembra se ficou aberta.
const menuUsuario = document.querySelector(".menu-usuario");
document.addEventListener("click", (e) => {
  if (menuUsuario && menuUsuario.open && !menuUsuario.contains(e.target)) menuUsuario.open = false;
});
const legenda = document.getElementById("legenda");
try { legenda.open = localStorage.getItem("torreLH.legenda") === "aberta"; } catch (e) { /* sem armazenamento */ }
legenda.addEventListener("toggle", () => {
  try { localStorage.setItem("torreLH.legenda", legenda.open ? "aberta" : "fechada"); } catch (e) { /* idem */ }
});

const seletorWhats = document.getElementById("whats");
try { seletorWhats.value = localStorage.getItem(CHAVE_WHATS) || ""; } catch (e) { /* automático */ }
seletorWhats.addEventListener("change", () => {
  try {
    if (seletorWhats.value) localStorage.setItem(CHAVE_WHATS, seletorWhats.value);
    else localStorage.removeItem(CHAVE_WHATS);
  } catch (e) { /* sem armazenamento: vale até recarregar */ }
  render();
});

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
  document.getElementById(id).addEventListener("input", () => {
    if (id !== "busca") semSinal = soChuva = soRotograma = soAcidente = soAlternativa = false;
    render();
  });
}
restaurarVisao();
new ControleCamadas().addTo(mapa);
if (tiposLigados.size) alternarTipoEstacao([...tiposLigados][0], true);
try {
  aplicarDados(JSON.parse(document.getElementById("dados-iniciais").textContent));
} catch (e) {
  console.error(e);
}
setTimeout(atualizar, 60000);
