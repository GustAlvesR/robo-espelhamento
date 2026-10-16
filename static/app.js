"use strict";

const COR = { OK: "var(--ok)", NOK: "var(--nok)", Pendente: "var(--pend)" };
const corCss = (nome) => getComputedStyle(document.documentElement)
  .getPropertyValue(nome.replace(/var\((.*)\)/, "$1")).trim();

const mapa = L.map("mapa", { zoomControl: true }).setView([-19.5, -45.5], 5);
L.tileLayer("/tiles/{z}/{x}/{y}.png", {
  maxZoom: 18, attribution: "© TomTom",
}).addTo(mapa);

const camadaRotas = L.layerGroup().addTo(mapa);
const camadaVeiculos = L.layerGroup().addTo(mapa);
const camadaSel = L.layerGroup().addTo(mapa);

let dados = [];
let selecionada = null;
let semSinal = false;
let ajustouVisao = false;

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

const statusDe = (r) => r["Status Geral"] || "Pendente";
const classeStatus = (r) => ({ OK: "ok", NOK: "nok" }[statusDe(r)] || "pend");

function idade(min) {
  if (min === null || min === undefined) return "";
  if (min < 60) return `${min} min`;
  const h = Math.floor(min / 60);
  return h < 48 ? `${h}h${String(min % 60).padStart(2, "0")}` : `${Math.floor(h / 24)} dias`;
}

function filtrar() {
  const q = document.getElementById("busca").value.trim().toUpperCase();
  const fs = document.getElementById("fStatus").value;
  const fr = document.getElementById("fRisco").value;
  return dados.filter((r) => {
    if (semSinal && r["Espelhamento Atual"] !== "NOK") return false;
    if (fs && statusDe(r) !== fs) return false;
    if (fr && r["Risco de Atraso"] !== fr) return false;
    if (!q) return true;
    return ["Trip Number", "Placa", "Placa 2", "Origin Station Code", "Destination Station Code", "Cidade/UF"]
      .some((c) => String(r[c] || "").toUpperCase().includes(q));
  });
}

function desenharKpis() {
  const cont = (f) => dados.filter(f).length;
  const kpis = [
    ["Rotas", dados.length, "", () => limparFiltros()],
    ["NOK", cont((r) => statusDe(r) === "NOK"), "nok", () => setFiltro("fStatus", "NOK")],
    ["Atraso provável", cont((r) => r["Risco de Atraso"] === "Atraso provável"), "alerta",
      () => setFiltro("fRisco", "Atraso provável")],
    ["Sem sinal agora", cont((r) => r["Espelhamento Atual"] === "NOK"), "nok", () => {
      limparFiltros(); semSinal = true; render(); }],
  ];
  const alvo = document.getElementById("kpis");
  alvo.replaceChildren(...kpis.map(([rot, n, cls, fn]) =>
    el("button", { class: `kpi ${cls}`, onclick: fn }, el("b", {}, String(n)), rot)));
}

function setFiltro(id, valor) { limparFiltros(); document.getElementById(id).value = valor; render(); }
function limparFiltros() {
  for (const id of ["fStatus", "fRisco"]) document.getElementById(id).value = "";
  semSinal = false;
  render();
}

function desenharLista(lista) {
  const ul = document.getElementById("lista");
  if (!lista.length) { ul.replaceChildren(el("li", { class: "vazio" }, "Nenhuma rota com esses filtros.")); return; }
  ul.replaceChildren(...lista.map((r) => {
    const risco = r["Risco de Atraso"] || "";
    const tagCls = risco === "Atraso provável" ? "atraso" : risco === "No prazo" ? "prazo" : "";
    return el("li", {
      class: `${classeStatus(r)} ${selecionada === r["Trip Number"] ? "sel" : ""}`,
      onclick: () => selecionar(r["Trip Number"], true),
    },
      el("div", { class: "l1" }, el("span", {}, r["Trip Number"]), risco ? el("span", { class: `tag ${tagCls}` }, risco) : null),
      el("div", { class: "l2" },
        `${r["Placa"] || "sem placa"} · ${r["Origin Station Code"] || "?"} → ${r["Destination Station Code"] || "?"}`
        + ` · ${r["% Espelhamento"] || "–"} espelhado`),
    );
  }));
}

function desenharMapa(lista) {
  camadaRotas.clearLayers();
  camadaVeiculos.clearLayers();
  const limites = [];
  for (const r of lista) {
    const g = r._geo || {};
    const cor = corCss(COR[statusDe(r)] || COR.Pendente);
    if (g.pontos && g.pontos.length) {
      L.polyline(g.pontos, { color: cor, weight: 2, opacity: 0.35 }).addTo(camadaRotas)
        .on("click", () => selecionar(r["Trip Number"], false));
    }
    const ponto = g.veiculo || g.origem;
    if (!ponto) continue;
    limites.push(ponto);
    const atraso = r["Risco de Atraso"] === "Atraso provável";
    L.circleMarker(ponto, {
      radius: g.veiculo ? 8 : 5,
      color: atraso ? corCss("var(--alerta)") : cor,
      weight: atraso ? 4 : 2,
      dashArray: g.precisao === "cidade" ? "3 3" : null,
      fillColor: cor, fillOpacity: g.veiculo ? 0.85 : 0.25,
    }).addTo(camadaVeiculos)
      .bindTooltip(`${r["Trip Number"]} · ${r["Placa"] || ""}`)
      .on("click", () => selecionar(r["Trip Number"], false));
  }
  if (!ajustouVisao && limites.length) {
    mapa.fitBounds(limites, { padding: [40, 40], maxZoom: 8 });
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
  if (g.pontos && g.pontos.length) L.polyline(g.pontos, { color: corCss("var(--acento)"), weight: 5 }).addTo(camadaSel);
  if (g.origem) L.circleMarker(g.origem, { radius: 6, color: corCss("var(--texto)"), fillOpacity: 1 }).bindTooltip("Origem").addTo(camadaSel);
  if (g.destino) L.circleMarker(g.destino, { radius: 6, color: corCss("var(--texto)"), fillColor: "#fff", fillOpacity: 1 }).bindTooltip("Destino").addTo(camadaSel);

  const desvio = r["Desvio vs Fim Previsto (min)"];
  box.replaceChildren(
    el("h3", {}, r["Trip Number"], el("button", { class: "fechar", "aria-label": "Fechar", onclick: () => selecionar(null) }, "×")),
    el("dl", {},
      ...linha("Placa", [r["Placa"], r["Placa 2"]].filter(Boolean).join(" / ")),
      ...linha("Trecho", `${r["Origin Station Code"] || "?"} → ${r["Destination Station Code"] || "?"}`),
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

function selecionar(trip, centralizar) {
  selecionada = trip;
  render();
  if (!trip || !centralizar) return;
  const r = dados.find((x) => x["Trip Number"] === trip);
  const g = (r && r._geo) || {};
  if (g.pontos && g.pontos.length) mapa.fitBounds(g.pontos, { padding: [40, 40] });
  else if (g.veiculo || g.origem) mapa.setView(g.veiculo || g.origem, 9);
}

function render() {
  const lista = filtrar();
  desenharKpis();
  desenharLista(lista);
  desenharMapa(lista);
  desenharDetalhe();
}

let falhasSeguidas = 0;
const CHAVE_VISAO = "torreLH.visao";

function idadeCicloMin(texto) {
  const m = /^(\d{2})\/(\d{2})\/(\d{4}) (\d{2}):(\d{2})/.exec(texto || "");
  if (!m) return null;
  const d = new Date(+m[3], +m[2] - 1, +m[1], +m[4], +m[5]);
  return Math.round((Date.now() - d.getTime()) / 60000);
}

function aplicarDados(j) {
  const aviso = document.getElementById("atualizado");
  dados = j.rotas || [];
  const tt = j.tomtom || {};
  const idade = idadeCicloMin(j.atualizado_em);
  // Ciclo normal é a cada 5 min; acima de 15 o robô ficou parado (hibernação ou erro).
  const parado = idade !== null && idade > 15 ? ` · ATENÇÃO: sem ciclo há ${idade} min` : "";
  aviso.textContent = `Ciclo: ${j.atualizado_em || "aguardando o primeiro"}` + parado
    + (tt.ultimo_erro ? ` · TomTom: ${tt.ultimo_erro}` : "");
  aviso.style.color = parado ? "var(--nok)" : "";
  try {
    render();
  } catch (e) {
    console.error(e);
    aviso.textContent += ` · Erro ao desenhar: ${e.message}`;
    aviso.style.color = "var(--nok)";
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
      fRisco: document.getElementById("fRisco").value,
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
    for (const id of ["busca", "fStatus", "fRisco"]) document.getElementById(id).value = v[id] || "";
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

for (const id of ["busca", "fStatus", "fRisco"]) {
  document.getElementById(id).addEventListener("input", () => { if (id !== "busca") semSinal = false; render(); });
}
restaurarVisao();
try {
  aplicarDados(JSON.parse(document.getElementById("dados-iniciais").textContent));
} catch (e) {
  console.error(e);
}
setTimeout(atualizar, 60000);
