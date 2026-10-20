// Monitor de rotas extras como FAVORITO (bookmarklet): roda na aba do AppSheet em que você já está logado.
// O app é carregado num quadro interno (iframe do mesmo endereço) que é recarregado a cada ciclo; o script
// fica na página de fora e sobrevive aos recarregamentos. Só LÊ a tela: nunca clica em ACEITAR nem recusar.
// Depende de criarLeitor (leitor.js) e das funções de nucleo.js, concatenados pelo gerar.py.

const CHAVE_LS = 'monitorRotasExtras.v1';

if (window.__monitorRotasFavorito) {
  window.__monitorRotasFavorito.mostrar();
  return;
}

const armazenado = (() => { try { return JSON.parse(localStorage.getItem(CHAVE_LS)) || {}; } catch (e) { return {}; } })();
const cfg = {...CONFIG_PADRAO, ...(armazenado.config || {})};
const est = armazenado.estado || estadoInicial();
let modo = 'quadro';
let timer = null;
let ocupado = false;
let lidoNoQuadro = false;

const salvar = () => localStorage.setItem(CHAVE_LS, JSON.stringify({config: cfg, estado: est}));

// ---------------- interface ----------------

const raiz = document.createElement('div');
raiz.id = 'monitor-rotas-extras';
raiz.style.cssText = 'position:fixed;inset:0;z-index:2147483647;display:flex;flex-direction:column;background:#fff;font:13px "Segoe UI",Arial,sans-serif;color:#1f2328';
raiz.innerHTML = `
  <div style="display:flex;align-items:center;gap:8px;padding:6px 10px;background:#0b3d91;color:#fff;flex-wrap:wrap">
    <b style="font-size:14px">Monitor de Rotas Extras</b>
    <span data-id="status" style="flex:1;min-width:200px">iniciando...</span>
    <button data-acao="ler">Ler agora</button>
    <button data-acao="config">Configurar</button>
    <button data-acao="teste">Testar envio</button>
    <button data-acao="alerta">Ativar alerta no PC</button>
    <button data-acao="ocultar">Esconder eventos</button>
    <button data-acao="parar">Parar</button>
  </div>
  <div data-id="config" style="display:none;padding:10px 12px;border-bottom:1px solid #ccc;background:#f6f8fa">
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:8px">
      <label>URL do webhook do Teams (vazio = não usa)<br><input data-campo="webhookUrl" style="width:100%"></label>
      <label>WhatsApp via CallMeBot: um por linha, telefone,apikey<br>
        <textarea data-campo="whatsappDestinos" rows="3" style="width:100%" placeholder="+5511999998888,123456"></textarea></label>
      <label>Telegram: token do bot<br><input data-campo="telegramToken" style="width:100%"></label>
      <label>Telegram: chat id(s), separados por vírgula<br><input data-campo="telegramChatIds" style="width:100%"></label>
      <label>E-mail: URL do app da web (Apps Script, termina em /exec)<br><input data-campo="emailUrl" style="width:100%"></label>
      <label>E-mail: token (o mesmo da propriedade TOKEN do script)<br><input data-campo="emailToken" style="width:100%"></label>
      <label>Intervalo (s, mín. 30)<br><input data-campo="intervaloS" type="number" min="30" style="width:100%"></label>
      <label>Veículos permitidos (vazio = todos)<br><input data-campo="veiculos" style="width:100%"></label>
      <label>Origens permitidas (prefixos)<br><input data-campo="origens" style="width:100%"></label>
      <label>Ignorar ETA em menos de (min)<br><input data-campo="etaMinimaMin" type="number" min="0" style="width:100%"></label>
      <label>Resumo diário às (HH:MM)<br><input data-campo="heartbeatHora" style="width:100%"></label>
    </div>
    <p style="margin:8px 0 0"><button data-acao="salvar">Salvar</button> <span data-id="msgcfg"></span></p>
  </div>
  <div data-id="eventos" style="max-height:110px;overflow:auto;padding:4px 12px;font-size:12px;border-bottom:1px solid #ddd;background:#fbfbfb"></div>
  <iframe data-id="quadro" style="flex:1;border:0;width:100%"></iframe>`;
document.body.appendChild(raiz);

const $ = s => raiz.querySelector(`[data-id="${s}"]`);
const quadro = $('quadro');
const status = texto => { $('status').textContent = texto; };

function desenharEventos() {
  $('eventos').innerHTML = '';
  for (const ev of est.eventos.slice(0, 15)) {
    const d = document.createElement('div');
    d.textContent = `${fmtData(ev.em)}  ${ev.texto}`;
    $('eventos').appendChild(d);
  }
}

function evento(texto) {
  registrarEvento(est, Date.now(), texto);
  desenharEventos();
}

function abrirConfig(mostrar = true) {
  $('config').style.display = mostrar ? 'block' : 'none';
  for (const el of raiz.querySelectorAll('[data-campo]')) el.value = cfg[el.dataset.campo];
}

function erroConfig(novo) {
  if (novo.webhookUrl && !/^https:\/\//.test(novo.webhookUrl)) return 'A URL do webhook deve começar com https://';
  if (!(novo.intervaloS >= 30)) return 'Intervalo mínimo: 30 s';
  if (!/^\d{2}:\d{2}$/.test(novo.heartbeatHora)) return 'Horário no formato HH:MM';
  const linhasWa = novo.whatsappDestinos.split('\n').filter(l => l.trim()).length;
  if (linhasWa !== destinosWhatsApp(novo.whatsappDestinos).length)
    return 'WhatsApp: cada linha deve ter telefone,apikey (ex.: +5511999998888,123456)';
  if (novo.telegramToken && !lista(novo.telegramChatIds).length) return 'Telegram: informe o chat id';
  if (novo.emailUrl && !/^https:\/\/script\.google\.com\/.+\/exec(\?.*)?$/.test(novo.emailUrl))
    return 'E-mail: a URL deve ser do script.google.com e terminar em /exec (não use a de teste, que termina em /dev)';
  if (novo.emailUrl && !novo.emailToken) return 'E-mail: informe o token';
  if (novo.emailToken && !novo.emailUrl) return 'E-mail: informe a URL do app da web';
  return null;
}

// Salva o formulário; devolve a mensagem de erro (ou null se salvou).
function salvarConfig() {
  const novo = {};
  for (const el of raiz.querySelectorAll('[data-campo]')) novo[el.dataset.campo] = el.value.trim();
  for (const c of ['intervaloS', 'etaMinimaMin']) novo[c] = Number(novo[c]);
  const erro = erroConfig(novo);
  $('msgcfg').textContent = erro || 'Salvo.';
  if (erro) return erro;
  Object.assign(cfg, novo);
  salvar();
  agendar();
  return null;
}

// ---------------- envio ----------------

const temCanal = () => Boolean(cfg.webhookUrl || destinosWhatsApp(cfg.whatsappDestinos).length
  || (cfg.telegramToken && lista(cfg.telegramChatIds).length) || (cfg.emailUrl && cfg.emailToken));
const esperarMs = ms => new Promise(res => setTimeout(res, ms));
const mascarar = tel => tel.slice(0, -4).replace(/\d/g, '•') + tel.slice(-4);

async function enviarTeams(msg) {
  const corpo = JSON.stringify(montarPayloadTeams(msg));
  let ultimo = '';
  for (let t = 1; t <= 3; t++) {
    try {
      const r = await fetch(cfg.webhookUrl, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: corpo});
      if (r.ok) return {ok: true, detalhe: `HTTP ${r.status}`};
      ultimo = `HTTP ${r.status} em ${new URL(cfg.webhookUrl).host}: ${(await r.text()).slice(0, 200)}`;
      if (r.status < 500 && r.status !== 408 && r.status !== 429) break;
    } catch (e) {
      ultimo = `bloqueado ou sem conexão (${e})`;
    }
    if (t < 3) await esperarMs(2000 * 2 ** (t - 1));
  }
  return {ok: false, detalhe: ultimo};
}

// CallMeBot não devolve CORS: o pedido sai (modo no-cors), mas a resposta não pode ser lida.
async function enviarSemResposta(url) {
  let ultimo = '';
  for (let t = 1; t <= 3; t++) {
    try {
      await fetch(url, {mode: 'no-cors'});
      return {ok: true, detalhe: 'enviado (sem confirmação de entrega)'};
    } catch (e) {
      ultimo = `bloqueado ou sem conexão (${e})`;
    }
    if (t < 3) await esperarMs(2000 * 2 ** (t - 1));
  }
  return {ok: false, detalhe: ultimo};
}

async function enviarTelegram(chatId, texto) {
  const url = `https://api.telegram.org/bot${cfg.telegramToken}/sendMessage`;
  const corpo = new URLSearchParams({chat_id: chatId, text: texto, disable_web_page_preview: 'true'});
  try {
    const r = await fetch(url, {method: 'POST', body: corpo});
    const j = await r.json().catch(() => ({}));
    return j.ok ? {ok: true, detalhe: 'ok'} : {ok: false, detalhe: `HTTP ${r.status}: ${j.description || ''}`};
  } catch (e) {
    // Sem leitura da resposta (CORS): uma única tentativa sem confirmação, para não duplicar a mensagem.
    try {
      await fetch(url, {method: 'POST', body: corpo, mode: 'no-cors'});
      return {ok: true, detalhe: 'enviado (sem confirmação de entrega)'};
    } catch (e2) {
      return {ok: false, detalhe: `bloqueado ou sem conexão (${e2})`};
    }
  }
}

// Registra bloqueios da política de segurança (CSP) da página, para explicar falhas de envio.
let ultimaViolacao = '';
document.addEventListener('securitypolicyviolation', e => {
  ultimaViolacao = `política de segurança do AppSheet (${e.violatedDirective}) bloqueou ${e.blockedURI}`;
});

// Envio alternativo: formulário POST para um iframe oculto. Não passa pela regra que bloqueia fetch,
// mas a resposta não pode ser lida (sem confirmação).
function enviarPorFormulario(url, dados) {
  return new Promise(resolve => {
    const nome = `mre-envio-${Date.now()}`;
    const alvo = Object.assign(document.createElement('iframe'), {name: nome});
    const form = Object.assign(document.createElement('form'), {method: 'POST', action: url, target: nome});
    const campo = Object.assign(document.createElement('input'), {type: 'hidden', name: 'dados', value: dados});
    alvo.style.display = form.style.display = 'none';
    form.appendChild(campo);
    document.body.append(alvo, form);
    const limpar = () => setTimeout(() => { alvo.remove(); form.remove(); }, 2000);
    const limite = setTimeout(() => { limpar(); resolve(false); }, 20000);
    alvo.onload = () => { clearTimeout(limite); limpar(); resolve(true); };
    form.submit();
  });
}

// Apps Script: corpo como text/plain (sem preflight de CORS); a resposta JSON confirma o envio.
// Uma tentativa por ciclo: se a resposta se perder, o e-mail pode já ter saído e repetir duplicaria.
async function enviarEmail(msg) {
  const corpo = JSON.stringify({token: cfg.emailToken.trim(), assunto: msg.titulo, html: montarHtmlEmail(msg),
    texto: montarTexto(msg, false)});
  let r;
  try {
    ultimaViolacao = '';
    r = await fetch(cfg.emailUrl, {method: 'POST', body: corpo, redirect: 'follow'});
  } catch (e) {
    // fetch bloqueado (política da página ou rede): tenta pelo formulário.
    await esperarMs(200);  // o evento securitypolicyviolation chega logo depois da falha do fetch
    const motivo = ultimaViolacao || String(e);
    if (await enviarPorFormulario(cfg.emailUrl, corpo)) {
      return {ok: true, detalhe: `enviado por formulário, sem confirmação (fetch falhou: ${motivo})`};
    }
    return {ok: false, detalhe: `bloqueado: ${ultimaViolacao || motivo}`};
  }
  const texto = await r.text().catch(() => '');
  let j = null;
  try { j = JSON.parse(texto); } catch (e) { /* resposta não é JSON */ }
  if (j && j.ok) return {ok: true, detalhe: `ok (cota restante hoje: ${j.cotaRestante})`};
  if (j) return {ok: false, detalhe: `${j.erro} (confira TOKEN/DESTINATARIOS no script)`};
  const login = /accounts\.google\.com|ServiceLogin|Sign in|Fazer login/i.test(texto);
  return {ok: false, detalhe: `HTTP ${r.status}: ` + (login
    ? 'o script pediu login. Na implantação, "Quem pode acessar" precisa ser "Qualquer pessoa"'
    : 'resposta inesperada. Confira se a URL é a da implantação (termina em /exec) e se o código foi reimplantado')};
}

// Envia para todos os canais configurados; ok se pelo menos um aceitou.
async function enviar(msg) {
  const canais = [];
  if (cfg.webhookUrl) canais.push(['Teams', () => enviarTeams(msg)]);
  for (const d of destinosWhatsApp(cfg.whatsappDestinos)) {
    canais.push([`WhatsApp ${mascarar(d.telefone)}`, () => enviarSemResposta(urlCallMeBot(d.telefone, d.apikey, montarTexto(msg, true)))]);
  }
  if (cfg.telegramToken) {
    for (const chat of lista(cfg.telegramChatIds)) canais.push([`Telegram ${chat}`, () => enviarTelegram(chat, montarTexto(msg, false))]);
  }
  if (cfg.emailUrl && cfg.emailToken) canais.push(['E-mail', () => enviarEmail(msg)]);
  if (!canais.length) {
    return {ok: false, detalhe: 'nenhum canal salvo. Abra "Configurar", preencha o canal '
      + '(e-mail precisa da URL /exec e do token) e clique em Salvar'};
  }
  let algum = false;
  const partes = [];
  for (const [nome, fn] of canais) {
    const r = await fn();
    algum = algum || r.ok;
    partes.push(`${nome}: ${r.ok ? 'ok' : 'falhou'} (${r.detalhe})`);
  }
  return {ok: algum, detalhe: partes.join('; ')};
}

function notificarPC(msg) {
  if (!('Notification' in window) || Notification.permission !== 'granted') return;
  try { new Notification(msg.titulo.slice(0, 120), {body: msg.resumo.slice(0, 250), requireInteraction: msg.nivel !== 'info'}); }
  catch (e) { console.warn('[Monitor rotas extras] notificação local falhou:', e); }
}

async function alertar(tipo, titulo, texto, cooldownMin = undefined, nivel = 'erro') {
  const agora = Date.now();
  const cd = cooldownMin === undefined ? Number(cfg.cooldownAlertasMin) : cooldownMin;
  if (!podeAlertar(est, tipo, agora, cd)) return;
  const msg = {titulo: `⚙ Monitor de rotas extras: ${titulo}`, resumo: texto, nivel, secoes: [], alertas: [], link: urlApp};
  evento(`ALERTA ${titulo}: ${texto}`);
  notificarPC(msg);
  if ((await enviar(msg)).ok || !temCanal()) est.alertas[tipo] = agora;
}

async function testarEnvio() {
  status('enviando teste...');
  const r = await enviar({titulo: '🧪 Teste do monitor de rotas extras', nivel: 'info', link: urlApp,
    resumo: 'Se você está lendo isto, o aviso está funcionando.', alertas: [],
    secoes: [[null, [['Enviado em', fmtData(Date.now())]]]]});
  const texto = r.ok ? `Teste enviado. ${r.detalhe}. Confira se chegou.` : `Falhou: ${r.detalhe}`;
  evento(texto);
  status(texto);
  alert(texto);
}

// ---------------- ciclo ----------------

const urlApp = location.href;

function documentoAlvo() {
  if (modo === 'direto') return document;
  try {
    const d = quadro.contentDocument;
    return d && d.location.host === location.host ? d : null;
  } catch (e) {
    return null;  // quadro foi para outro domínio (ex.: tela de login do Google)
  }
}

function carregarQuadro() {
  return new Promise(resolve => {
    const fim = setTimeout(() => resolve(false), 60000);
    quadro.onload = () => { clearTimeout(fim); resolve(true); };
    if (quadro.src) quadro.contentWindow.location.reload(); else quadro.src = urlApp;
  });
}

async function ciclo() {
  if (ocupado) return;
  ocupado = true;
  try {
    if (modo === 'quadro') {
      status('recarregando o app...');
      await carregarQuadro();
      if (documentoAlvo()) {
        lidoNoQuadro = true;
      } else {
        if (!lidoNoQuadro) {
          // O app não abriu dentro do quadro (bloqueio de iframe): lê a própria página, sem recarregar.
          modo = 'direto';
          quadro.style.display = 'none';
          raiz.style.bottom = 'auto';
          evento('O app não abriu no quadro interno; lendo a página direto (sem recarregar). '
            + 'Ative a atualização automática do app ou recarregue a aba e clique no favorito de vez em quando.');
        } else {
          await alertar('sessao', 'sessão do AppSheet expirada',
            'O app saiu do AppSheet (provável tela de login). Faça login em outra aba e clique em "Ler agora".');
          status('aguardando login no AppSheet');
          return;
        }
      }
    }
    status('lendo a tela...');
    const leitura = await criarLeitor(documentoAlvo()).ler();
    await processar(leitura);
  } catch (e) {
    console.error('[Monitor rotas extras]', e);
    evento(`Erro na leitura: ${e}`);
  } finally {
    ocupado = false;
    salvar();
  }
}

async function processar(dados) {
  const agora = Date.now();
  if (dados.login) {
    await alertar('sessao', 'sessão do AppSheet expirada', 'O app está na tela de login. Faça login e clique em "Ler agora".');
    return status('aguardando login no AppSheet');
  }
  if (!dados.rotaExtra) {
    await alertar('fora_tela', 'fora da tela ROTA EXTRA', 'Abra MENU > ROTA EXTRA nesta aba e clique no favorito de novo.');
    return status('a página não está em MENU > ROTA EXTRA');
  }
  delete est.alertas.sessao;
  delete est.alertas.fora_tela;
  est.ultimaLeitura = agora;
  est.stats.leituras += 1;

  const unicas = new Map();
  for (const bruto of dados.cards) { const r = parseCard(bruto); const k = chave(r); if (!unicas.has(k)) unicas.set(k, r); }
  await checarLayout(dados, [...unicas.values()]);
  const novas = registrarPresentes(est, unicas.values(), agora);

  let falhou = false;
  for (const [k, rota] of unicas) {
    const reg = est.rotas[k];
    if (reg.status !== 'pendente') continue;
    const av = avaliar(rota, Date.now(), cfg);
    if (av.filtro) {
      reg.status = 'filtrada'; reg.motivo = av.filtro; est.stats.filtradas += 1;
      evento(`Filtrada (${av.filtro}): ${rota.nome}`);
      continue;
    }
    if (falhou) continue;
    const msg = mensagemRota(rota, av, reg.primeiraVez, Date.now(), {...cfg, urlRotas: urlApp});
    if (reg.tentativas === 0) notificarPC(msg);
    const r = await enviar(msg);
    if (r.ok) {
      reg.status = 'notificada'; reg.notificadaEm = Date.now(); est.stats.notificadas += 1;
      evento(`Avisada no Teams: ${rota.nome} (ETA ${fmtData(rota.eta)})`);
    } else {
      reg.tentativas += 1; est.stats.falhasEnvio += 1; falhou = true;
      evento(`Falha ao avisar ${rota.nome} (${r.detalhe}); nova tentativa no próximo ciclo`);
    }
  }
  esquecerAusentes(est, new Set(unicas.keys()), agora, Number(cfg.esquecerAposMin));
  est.ultimoCiclo = {em: agora, cards: dados.cards.length, unicas: unicas.size, novas: novas.length};
  await heartbeat(agora);
  status(`última leitura ${fmtHora(agora)}: ${dados.cards.length} card(s), ${novas.length} nova(s)` +
    (modo === 'direto' ? ' (modo sem recarregar)' : '') + `. Próxima em ${cfg.intervaloS}s.`);
}

async function checarLayout(dados, rotas) {
  if (!dados.cards.length) {
    est.ciclosVazios += 1;
    if (dados.labelsTotal > 0 && dados.totalBotoes === 0) {
      await alertar('layout_aceitar', 'possível mudança de layout',
        `Há ${dados.labelsTotal} elemento(s) de rota mas nenhum botão ACEITAR reconhecido.`, null);
    } else if (est.ciclosVazios === Number(cfg.ciclosVaziosAlerta)) {
      await alertar('vazio', 'nenhum card há muito tempo',
        `0 cards em ${est.ciclosVazios} leituras seguidas. Pode ser só falta de oferta.`, null, 'alerta');
    }
    return;
  }
  est.ciclosVazios = 0;
  delete est.alertas.vazio;
  delete est.alertas.layout_aceitar;
  const semNome = rotas.filter(r => !r.nome).length;
  if (semNome || dados.semCard) {
    await alertar('layout_parse', 'cards com leitura incompleta',
      `${semNome} card(s) sem nome de rota e ${dados.semCard} botão(ões) ACEITAR sem card. O layout pode ter mudado.`);
  }
}

async function heartbeat(agora) {
  const [h, m] = String(cfg.heartbeatHora || '08:00').split(':').map(Number);
  const d = new Date(agora);
  const hoje = `${d.getFullYear()}-${d.getMonth() + 1}-${d.getDate()}`;
  if (d.getHours() * 60 + d.getMinutes() < h * 60 + m || est.heartbeatData === hoje) return;
  const s = est.stats;
  const msg = {titulo: '💓 Monitor de rotas extras ativo', resumo: 'Resumo desde o último heartbeat (favorito).',
    nivel: 'info', alertas: [], link: urlApp, secoes: [[null, [
      ['Leituras da tela', String(s.leituras)], ['Rotas novas', String(s.rotasNovas)],
      ['Avisadas', String(s.notificadas)], ['Filtradas', String(s.filtradas)],
      ['Falhas de envio', String(s.falhasEnvio)], ['Última rota nova', s.ultimaRotaEm ? fmtData(s.ultimaRotaEm) : '-'],
    ]]]};
  if ((await enviar(msg)).ok) {
    est.heartbeatData = hoje;
    est.stats = {...statsZerados(), ultimaRotaEm: s.ultimaRotaEm};
  }
}

function agendar() {
  clearInterval(timer);
  timer = setInterval(ciclo, Math.max(30, Number(cfg.intervaloS)) * 1000);
}

function parar() {
  clearInterval(timer);
  window.removeEventListener('beforeunload', avisarSaida);
  raiz.remove();
  delete window.__monitorRotasFavorito;
}

function avisarSaida(e) { e.preventDefault(); e.returnValue = ''; }

raiz.addEventListener('click', async e => {
  const acao = e.target.dataset && e.target.dataset.acao;
  if (acao === 'ler') ciclo();
  else if (acao === 'config') abrirConfig($('config').style.display === 'none');
  else if (acao === 'salvar') salvarConfig();
  else if (acao === 'teste') {
    // Se o formulário está aberto, salva antes de testar (testar sem salvar usava a configuração antiga).
    if ($('config').style.display !== 'none') {
      const erro = salvarConfig();
      if (erro) { alert(`Não salvei: ${erro}`); return; }
    }
    testarEnvio();
  }
  else if (acao === 'alerta' && 'Notification' in window) {
    const p = await Notification.requestPermission();
    evento(p === 'granted' ? 'Alerta no PC ativado' : 'Alerta no PC não permitido pelo navegador');
  } else if (acao === 'ocultar') {
    const ev = $('eventos');
    const escondido = ev.style.display === 'none';
    ev.style.display = escondido ? 'block' : 'none';
    e.target.textContent = escondido ? 'Esconder eventos' : 'Mostrar eventos';
  } else if (acao === 'parar' && confirm('Parar o monitor de rotas extras nesta aba?')) parar();
});

window.addEventListener('beforeunload', avisarSaida);
window.__monitorRotasFavorito = {mostrar: () => { raiz.style.display = 'flex'; }, ciclo, estado: est, config: cfg};
desenharEventos();
evento('Monitor iniciado nesta aba');
if (!temCanal()) {
  abrirConfig(true);
  status('configure pelo menos um canal (e-mail, Teams, WhatsApp ou Telegram) e clique em Salvar');
}
agendar();
ciclo();
