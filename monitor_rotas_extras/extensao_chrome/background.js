// Agenda os ciclos, recarrega a aba ROTA EXTRA, deduplica as rotas e avisa no Teams.
import * as N from './nucleo.js';

const ALARME = 'ciclo';
const ICONE = 'icone128.png';

// O service worker é único; esta fila evita que alarme e leitura gravem o estado ao mesmo tempo.
let fila = Promise.resolve();
const emSerie = fn => { const p = fila.then(fn, fn); fila = p.catch(() => {}); return p; };

async function carregar() {
  const {config = {}, estado} = await chrome.storage.local.get(['config', 'estado']);
  return {cfg: {...N.CONFIG_PADRAO, ...config}, est: estado || N.estadoInicial()};
}

const salvarEstado = est => chrome.storage.local.set({estado: est});

async function agendar() {
  const {cfg} = await carregar();
  await chrome.alarms.clear(ALARME);
  await chrome.alarms.create(ALARME, {periodInMinutes: Math.max(0.5, Number(cfg.intervaloS) / 60), delayInMinutes: 0.1});
}

chrome.runtime.onInstalled.addListener(agendar);
chrome.runtime.onStartup.addListener(agendar);
chrome.storage.onChanged.addListener((mud, area) => { if (area === 'local' && mud.config) agendar(); });
chrome.action.onClicked.addListener(() => chrome.runtime.openOptionsPage());
chrome.alarms.onAlarm.addListener(a => { if (a.name === ALARME) emSerie(ciclo).catch(e => console.error(e)); });

chrome.runtime.onMessage.addListener((msg, sender, responder) => {
  if (msg.tipo === 'leitura' && sender.tab) {
    emSerie(() => processarLeitura(sender.tab.id, msg)).then(() => responder({ok: true}),
      e => { console.error(e); responder({ok: false, erro: String(e)}); });
    return true;
  }
  if (msg.tipo === 'teste') {
    enviarTeste().then(responder);
    return true;
  }
  if (msg.tipo === 'ciclo_agora') {
    emSerie(ciclo).then(() => responder({ok: true}), e => responder({ok: false, erro: String(e)}));
    return true;
  }
  return false;
});

const ehAppSheet = url => /^https:\/\/([a-z0-9-]+\.)*appsheet\.com\//i.test(url || '');

async function abaMonitorada(est) {
  if (est.abaId !== null) {
    try { return await chrome.tabs.get(est.abaId); } catch (e) { est.abaId = null; }
  }
  const abas = await chrome.tabs.query({url: 'https://*.appsheet.com/*'});
  const rota = abas.find(a => { try { return N.normalizar(decodeURIComponent(a.url)).includes('VIEW=ROTA EXTRA'); } catch (e) { return false; } });
  if (rota) { est.abaId = rota.id; return rota; }
  return null;
}

async function adotarAba(est, abaId, agora) {
  est.abaId = abaId;
  try { await chrome.tabs.update(abaId, {autoDiscardable: false}); } catch (e) { /* aba fechou */ }
  N.registrarEvento(est, agora, 'Aba ROTA EXTRA reconhecida e monitorada');
}

async function ciclo() {
  const {cfg, est} = await carregar();
  const agora = Date.now();
  let aba = await abaMonitorada(est);
  if (!aba && cfg.urlRotas) {
    aba = await chrome.tabs.create({url: cfg.urlRotas, pinned: true, active: false});
    await adotarAba(est, aba.id, agora);
  } else if (!aba) {
    await alertar(est, cfg, 'sem_aba', 'nenhuma aba ROTA EXTRA aberta',
      'Abra MENU > ROTA EXTRA no Chrome (ou preencha a URL nas opções da extensão).', 60);
  } else if (!ehAppSheet(aba.url)) {
    await alertar(est, cfg, 'sessao', 'sessão do AppSheet expirada',
      'A aba monitorada saiu do AppSheet (provável tela de login). Faça login nela; o monitor volta sozinho.');
  } else {
    await chrome.tabs.reload(aba.id);
  }
  const limite = Math.max(4 * Number(cfg.intervaloS), 180) * 1000;
  if (aba && est.ultimaLeitura && agora - est.ultimaLeitura > limite) {
    await alertar(est, cfg, 'sem_leitura', 'sem leituras recentes',
      `A última leitura da tela foi às ${N.fmtHora(est.ultimaLeitura)}. A aba pode estar travada ou fora da tela ROTA EXTRA.`);
  }
  await heartbeat(est, cfg, agora);
  await salvarEstado(est);
}

async function processarLeitura(abaId, dados) {
  const {cfg, est} = await carregar();
  const agora = Date.now();
  if (est.abaId !== abaId) {
    let atualViva = false;
    if (est.abaId !== null) { try { await chrome.tabs.get(est.abaId); atualViva = true; } catch (e) { /* fechada */ } }
    if (atualViva || !dados.rotaExtra) return;
    await adotarAba(est, abaId, agora);
  }
  if (dados.login) {
    await alertar(est, cfg, 'sessao', 'sessão do AppSheet expirada',
      'A aba monitorada está na tela de login. Faça login nela; o monitor volta sozinho.');
    return salvarEstado(est);
  }
  if (!dados.rotaExtra) {
    await alertar(est, cfg, 'fora_tela', 'aba fora da tela ROTA EXTRA',
      'A aba monitorada não está em MENU > ROTA EXTRA. Volte para essa tela.');
    return salvarEstado(est);
  }
  delete est.alertas.sessao;
  delete est.alertas.fora_tela;
  delete est.alertas.sem_leitura;
  est.ultimaLeitura = agora;
  est.stats.leituras += 1;

  const unicas = new Map();
  for (const bruto of dados.cards) { const r = N.parseCard(bruto); const k = N.chave(r); if (!unicas.has(k)) unicas.set(k, r); }
  await checarLayout(est, cfg, dados, [...unicas.values()]);
  const novas = N.registrarPresentes(est, unicas.values(), agora);

  let canaisFalharam = false;
  for (const [k, rota] of unicas) {
    const reg = est.rotas[k];
    if (reg.status !== 'pendente') continue;
    const av = N.avaliar(rota, Date.now(), cfg);
    if (av.filtro) {
      reg.status = 'filtrada'; reg.motivo = av.filtro; est.stats.filtradas += 1;
      N.registrarEvento(est, agora, `Filtrada (${av.filtro}): ${rota.nome}`);
      continue;
    }
    if (canaisFalharam) continue;
    const msg = N.mensagemRota(rota, av, reg.primeiraVez, Date.now(), cfg);
    if (reg.tentativas === 0) notificarLocal(cfg, msg);
    if (await enviar(cfg, msg)) {
      reg.status = 'notificada'; reg.notificadaEm = Date.now(); est.stats.notificadas += 1;
      N.registrarEvento(est, agora, `Avisada no Teams: ${rota.nome} (ETA ${N.fmtData(rota.eta)})`);
    } else {
      reg.tentativas += 1; est.stats.falhasEnvio += 1; canaisFalharam = true;
      N.registrarEvento(est, agora, `Falha ao avisar ${rota.nome}; nova tentativa no próximo ciclo`);
    }
  }
  N.esquecerAusentes(est, new Set(unicas.keys()), agora, Number(cfg.esquecerAposMin));
  est.ultimoCiclo = {em: agora, cards: dados.cards.length, unicas: unicas.size, novas: novas.length};
  await salvarEstado(est);
}

async function checarLayout(est, cfg, dados, rotas) {
  if (!dados.cards.length) {
    est.ciclosVazios += 1;
    if (dados.labelsTotal > 0 && dados.totalBotoes === 0) {
      await alertar(est, cfg, 'layout_aceitar', 'possível mudança de layout',
        `Há ${dados.labelsTotal} elemento(s) de rota mas nenhum botão ACEITAR reconhecido.`, null);
    } else if (est.ciclosVazios === Number(cfg.ciclosVaziosAlerta)) {
      await alertar(est, cfg, 'vazio', 'nenhum card há muito tempo',
        `0 cards em ${est.ciclosVazios} leituras seguidas. Pode ser só falta de oferta; se houver rotas na tela, o layout mudou.`,
        null, 'alerta');
    }
    return;
  }
  est.ciclosVazios = 0;
  delete est.alertas.vazio;
  delete est.alertas.layout_aceitar;
  const semNome = rotas.filter(r => !r.nome).length;
  if (semNome || dados.semCard) {
    await alertar(est, cfg, 'layout_parse', 'cards com leitura incompleta',
      `${semNome} card(s) sem nome de rota e ${dados.semCard} botão(ões) ACEITAR sem card. O layout pode ter mudado.`);
  }
}

async function enviar(cfg, msg) {
  if (!cfg.webhookUrl) return false;
  const corpo = JSON.stringify(N.montarPayloadTeams(msg));
  for (let t = 1; t <= 3; t++) {
    try {
      const r = await fetch(cfg.webhookUrl, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: corpo});
      if (r.ok) return true;
      console.warn(`Teams HTTP ${r.status}`, (await r.text()).slice(0, 300));
      if (r.status < 500 && r.status !== 408 && r.status !== 429) return false;
    } catch (e) {
      console.warn('Teams sem conexão:', e);
    }
    if (t < 3) await new Promise(res => setTimeout(res, 2000 * 2 ** (t - 1)));
  }
  return false;
}

function notificarLocal(cfg, msg) {
  if (!cfg.notificacaoLocal) return;
  chrome.notifications.create({type: 'basic', iconUrl: ICONE, title: msg.titulo.slice(0, 120),
    message: msg.resumo.slice(0, 250), priority: 2, requireInteraction: msg.nivel !== 'info'});
}

async function alertar(est, cfg, tipo, titulo, texto, cooldownMin = undefined, nivel = 'erro') {
  const agora = Date.now();
  const cd = cooldownMin === undefined ? Number(cfg.cooldownAlertasMin) : cooldownMin;
  if (!N.podeAlertar(est, tipo, agora, cd)) return;
  const msg = {titulo: `⚙ Monitor de rotas extras: ${titulo}`, resumo: texto, nivel, secoes: [], alertas: [],
    link: cfg.urlRotas || null};
  N.registrarEvento(est, agora, `ALERTA ${titulo}: ${texto}`);
  notificarLocal(cfg, msg);
  if (await enviar(cfg, msg) || !cfg.webhookUrl) est.alertas[tipo] = agora;
}

async function heartbeat(est, cfg, agora) {
  const [h, m] = String(cfg.heartbeatHora || '08:00').split(':').map(Number);
  const d = new Date(agora);
  const hoje = `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`;
  if (d.getHours() * 60 + d.getMinutes() < h * 60 + m || est.heartbeatData === hoje) return;
  const s = est.stats;
  const msg = {titulo: '💓 Monitor de rotas extras ativo', resumo: 'Resumo desde o último heartbeat.', nivel: 'info',
    alertas: [], link: cfg.urlRotas || null, secoes: [[null, [
      ['Leituras da tela', String(s.leituras)], ['Rotas novas', String(s.rotasNovas)],
      ['Avisadas', String(s.notificadas)], ['Filtradas', String(s.filtradas)],
      ['Falhas de envio', String(s.falhasEnvio)], ['Última rota nova', s.ultimaRotaEm ? N.fmtData(s.ultimaRotaEm) : '-'],
      ['Última leitura', est.ultimaLeitura ? N.fmtData(est.ultimaLeitura) : 'nenhuma'],
    ]]]};
  if (await enviar(cfg, msg)) {
    est.heartbeatData = hoje;
    est.stats = {...N.statsZerados(), ultimaRotaEm: s.ultimaRotaEm};
  }
}

async function enviarTeste() {
  const {cfg} = await carregar();
  if (!cfg.webhookUrl) return {ok: false, erro: 'Preencha e salve a URL do webhook primeiro.'};
  const msg = {titulo: '🧪 Teste do monitor de rotas extras', nivel: 'info', link: cfg.urlRotas || null,
    resumo: 'Se você está lendo isto, o aviso no Teams está funcionando.', alertas: ['Exemplo de alerta (pode ignorar)'],
    secoes: [[null, [['Enviado em', N.fmtData(Date.now())]]]]};
  const ok = await enviar(cfg, msg);
  return ok ? {ok} : {ok, erro: 'O Teams não aceitou. Confira a URL do webhook e o console do service worker.'};
}
