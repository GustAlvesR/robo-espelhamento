/**
 * Monitor de Rotas Extras: fila e envio de e-mail (Google Apps Script publicado como app da web).
 *
 * O favorito manda cada rota nova para cá; elas entram numa fila e um gatilho de tempo envia UM e-mail
 * com todas as rotas acumuladas a cada INTERVALO_MIN minutos. O mesmo gatilho avisa por e-mail se o
 * favorito parar de mandar sinal (notebook desligado, aba fechada, sessão expirada).
 *
 * Propriedades do script (Configurações do projeto > Propriedades do script):
 *   TOKEN                 obrigatória; senha longa, a mesma vai no favorito
 *   DESTINATARIOS         obrigatória; e-mails separados por vírgula ou ponto e vírgula
 *   INTERVALO_MIN         opcional; 5, 10, 15 (padrão) ou 30. Depois de mudar, rode instalarGatilhos
 *   LIMITE_SEM_SINAL_MIN  opcional; minutos sem sinal do favorito para avisar que parou (padrão 20)
 *   JANELA_OFERTA_MIN     opcional; minutos até a rota sumir do app (padrão 30)
 *
 * Os destinatários ficam aqui, não no favorito: quem tiver a URL e o token só consegue mandar
 * para essa lista, nunca para outros endereços.
 */

const FUSO = 'America/Sao_Paulo';
const PREFIXO_FILA = 'FILA_';
const PREFIXO_ENVIADA = 'ENV_';
const LIMITE_ASSUNTO = 250;
const LIMITE_PROPRIEDADE = 8500;  // o Apps Script aceita até ~9 KB por propriedade

// ---------------- entrada (favorito) ----------------

function doPost(e) {
  // Aceita JSON no corpo (fetch) ou no campo "dados" de um formulário (envio alternativo do favorito).
  let dados;
  try {
    dados = JSON.parse(e.parameter && e.parameter.dados ? e.parameter.dados : e.postData.contents);
  } catch (erro) {
    return responder_({ok: false, erro: 'corpo inválido'});
  }
  const props = PropertiesService.getScriptProperties();
  const token = (props.getProperty('TOKEN') || '').trim();
  if (!token || String(dados.token || '').trim() !== token) return responder_({ok: false, erro: 'token inválido'});

  // Qualquer chamada válida é sinal de vida do favorito.
  props.setProperty('ULTIMO_SINAL', String(Date.now()));
  if (dados.estatisticas) props.setProperty('ESTATISTICAS', JSON.stringify(dados.estatisticas).slice(0, LIMITE_PROPRIEDADE));

  if (dados.tipo === 'ping') return responder_({ok: true});
  if (dados.tipo === 'rota' || dados.tipo === 'alerta') return enfileirar_(props, dados);
  return enviarImediato_(props, dados);
}

// Abrir a URL /exec no navegador mostra o status (sem enviar nada).
function doGet() {
  const props = PropertiesService.getScriptProperties();
  const sinal = Number(props.getProperty('ULTIMO_SINAL') || 0);
  return responder_({ok: true, servico: 'monitor-rotas-extras', naFila: chavesDaFila_(props).length,
    ultimoSinal: sinal ? formatar_(sinal) : 'nunca', cotaRestante: MailApp.getRemainingDailyQuota()});
}

function enfileirar_(props, dados) {
  let id, item;
  if (dados.tipo === 'rota') {
    if (!dados.rota || !dados.rota.chave) return responder_({ok: false, erro: 'rota sem chave'});
    id = hash_(dados.rota.chave);
    item = {tipo: 'rota', recebidoEm: Date.now(), rota: dados.rota};
  } else {
    id = `${Date.now()}_${Math.floor(Math.random() * 1e6)}`;
    item = {tipo: 'alerta', recebidoEm: Date.now(), alerta: dados.alerta || {}};
  }
  const lock = LockService.getScriptLock();
  lock.waitLock(10000);
  try {
    // A mesma rota (mesma chave) entra uma vez só, mesmo que o favorito reenvie.
    if (!props.getProperty(PREFIXO_FILA + id) && !props.getProperty(PREFIXO_ENVIADA + id)) {
      props.setProperty(PREFIXO_FILA + id, compactar_(item));
    }
  } finally {
    lock.releaseLock();
  }
  return responder_({ok: true, naFila: chavesDaFila_(props).length, intervaloMin: intervaloMin_(props)});
}

function enviarImediato_(props, dados) {
  const destinatarios = destinatarios_(props);
  if (!destinatarios) return responder_({ok: false, erro: 'propriedade DESTINATARIOS vazia'});
  if (MailApp.getRemainingDailyQuota() < 1) return responder_({ok: false, erro: 'cota diária de e-mail esgotada'});
  MailApp.sendEmail({
    to: destinatarios,
    subject: String(dados.assunto || 'Monitor de Rotas Extras').slice(0, LIMITE_ASSUNTO),
    htmlBody: String(dados.html || ''),
    body: String(dados.texto || ''),
    name: 'Monitor de Rotas Extras',
  });
  return responder_({ok: true, cotaRestante: MailApp.getRemainingDailyQuota()});
}

// ---------------- gatilho de tempo ----------------

// Executado pelo gatilho a cada INTERVALO_MIN minutos (também pode ser rodado à mão para forçar o envio).
function enviarResumo() {
  const props = PropertiesService.getScriptProperties();
  const lock = LockService.getScriptLock();
  lock.waitLock(30000);
  try {
    const agora = Date.now();
    const chaves = chavesDaFila_(props);
    const itens = chaves.map(k => JSON.parse(props.getProperty(k))).sort((a, b) => a.recebidoEm - b.recebidoEm);
    const rotas = itens.filter(i => i.tipo === 'rota').map(i => i.rota);
    const alertas = itens.filter(i => i.tipo === 'alerta').map(i => i.alerta);

    const sinal = Number(props.getProperty('ULTIMO_SINAL') || 0);
    const limite = Number(props.getProperty('LIMITE_SEM_SINAL_MIN') || 20) * 60000;
    const jaAvisouParado = props.getProperty('AVISO_PARADO') === '1';
    const parado = sinal > 0 && agora - sinal > limite;
    if (parado && !jaAvisouParado) {
      alertas.push({titulo: 'monitor parou de ler a tela', nivel: 'erro',
        texto: `Último sinal do favorito às ${formatar_(sinal)}. Notebook desligado ou suspenso, aba fechada ou `
          + 'sessão do AppSheet expirada. Abra ROTA EXTRA e clique no favorito.'});
    } else if (!parado && jaAvisouParado) {
      alertas.push({titulo: 'monitor voltou a ler a tela', nivel: 'info', texto: `Sinal recebido às ${formatar_(sinal)}.`});
    }
    if (!rotas.length && !alertas.length) return;

    const destinatarios = destinatarios_(props);
    const qtdDest = destinatarios ? destinatarios.split(',').length : 0;
    if (!qtdDest || MailApp.getRemainingDailyQuota() < qtdDest) {
      console.warn('Sem destinatários ou sem cota; a fila fica para o próximo ciclo.');
      return;
    }
    const email = montarResumo_(rotas, alertas, agora, Number(props.getProperty('JANELA_OFERTA_MIN') || 30));
    MailApp.sendEmail({to: destinatarios, subject: email.assunto.slice(0, LIMITE_ASSUNTO), htmlBody: email.html,
      body: email.texto, name: 'Monitor de Rotas Extras'});

    for (const k of chaves) {
      if (k.indexOf(PREFIXO_FILA) === 0 && JSON.parse(props.getProperty(k) || '{}').tipo === 'rota') {
        props.setProperty(PREFIXO_ENVIADA + k.slice(PREFIXO_FILA.length), String(agora));
      }
      props.deleteProperty(k);
    }
    if (parado) props.setProperty('AVISO_PARADO', '1'); else props.deleteProperty('AVISO_PARADO');
    limparEnviadas_(props, agora);
  } finally {
    lock.releaseLock();
  }
}

// Rode uma vez pelo editor (e de novo se mudar INTERVALO_MIN). Cria o gatilho e pede as autorizações.
function instalarGatilhos() {
  for (const g of ScriptApp.getProjectTriggers()) {
    if (g.getHandlerFunction() === 'enviarResumo') ScriptApp.deleteTrigger(g);
  }
  const minutos = intervaloMin_(PropertiesService.getScriptProperties());
  ScriptApp.newTrigger('enviarResumo').timeBased().everyMinutes(minutos).create();
  console.log(`Gatilho criado: enviarResumo a cada ${minutos} minutos.`);
}

// Rode pelo editor para receber um e-mail de teste agora.
function testarEnvio() {
  const destinatarios = destinatarios_(PropertiesService.getScriptProperties());
  if (!destinatarios) throw new Error('Preencha a propriedade DESTINATARIOS antes de testar.');
  MailApp.sendEmail(destinatarios, 'Teste do Monitor de Rotas Extras', 'Se você recebeu, o envio de e-mail está funcionando.');
}

// ---------------- montagem do e-mail ----------------

function montarResumo_(rotas, alertas, agora, janelaMin) {
  const e = escapar_;
  const partes = [];
  const linhasTexto = [];
  if (rotas.length) {
    const cab = ['Rota', 'Veículo', 'Tipo', 'Origem', 'ETA', 'CPT', 'Lim. retorno', 'Detectada', 'Restante (est.)', 'Alertas'];
    const linhas = rotas.map(r => {
      const expira = r.primeiraVez + janelaMin * 60000;
      const restante = expira > agora ? `até ~${formatar_(expira, 'HH:mm')} (${Math.ceil((expira - agora) / 60000)} min)`
        : 'provavelmente expirada';
      const celulas = [r.nome, r.veiculo, r.tipo, r.origem, r.eta ? formatar_(r.eta) : r.etaTexto,
        r.cpt ? formatar_(r.cpt) : r.cptTexto, r.tempoLimite, formatar_(r.primeiraVez, 'HH:mm'), restante,
        (r.alertas || []).join('; ')];
      linhasTexto.push(celulas.map((c, i) => `${cab[i]}: ${c || '-'}`).join(' | '));
      const estilo = (r.alertas || []).length ? ' style="background:#fff4e5"' : '';
      return `<tr${estilo}>` + celulas.map(c => `<td style="padding:4px 8px;border:1px solid #ddd">${e(c || '-')}</td>`).join('') + '</tr>';
    });
    partes.push(`<h2 style="margin:0 0 8px">🚚 ${rotas.length} rota(s) extra(s) ofertada(s)</h2>`
      + '<table style="border-collapse:collapse;font-size:13px"><tr>'
      + cab.map(c => `<th style="padding:4px 8px;border:1px solid #ddd;background:#0b3d91;color:#fff;text-align:left">${c}</th>`).join('')
      + '</tr>' + linhas.join('') + '</table>'
      + `<p style="color:#555;font-size:12px">"Restante" conta ${janelaMin} min a partir de quando o monitor viu a rota; `
      + 'ela pode já ter sido aceita por outra pessoa.</p>');
  }
  if (alertas.length) {
    partes.push('<h3 style="margin:14px 0 6px">⚙ Avisos do monitor</h3><ul>'
      + alertas.map(a => `<li><b>${e(a.titulo)}</b>: ${e(a.texto)}</li>`).join('') + '</ul>');
    for (const a of alertas) linhasTexto.push(`Aviso: ${a.titulo}: ${a.texto}`);
  }
  const hora = formatar_(agora, 'HH:mm');
  const assunto = rotas.length
    ? `[Rotas Extras] ${rotas.length} rota(s) ofertada(s) até ${hora}: ${rotas.slice(0, 3).map(r => r.nome).join(', ')}`
      + (rotas.length > 3 ? '...' : '')
    : `[Rotas Extras] Aviso do monitor (${hora})`;
  return {assunto, html: `<div style="font-family:Segoe UI,Arial,sans-serif">${partes.join('')}</div>`,
    texto: linhasTexto.join('\n')};
}

// ---------------- utilidades ----------------

function chavesDaFila_(props) {
  return props.getKeys().filter(k => k.indexOf(PREFIXO_FILA) === 0);
}

function limparEnviadas_(props, agora) {
  for (const k of props.getKeys()) {
    if (k.indexOf(PREFIXO_ENVIADA) === 0 && agora - Number(props.getProperty(k)) > 24 * 3600000) props.deleteProperty(k);
  }
}

function destinatarios_(props) {
  return (props.getProperty('DESTINATARIOS') || '').split(/[,;\s]+/).filter(String).join(',');
}

function intervaloMin_(props) {
  const v = Number(props.getProperty('INTERVALO_MIN') || 15);
  return [1, 5, 10, 15, 30].indexOf(v) >= 0 ? v : 15;
}

function compactar_(item) {
  let json = JSON.stringify(item);
  if (json.length > LIMITE_PROPRIEDADE && item.rota) {
    item.rota.alertas = (item.rota.alertas || []).slice(0, 3).map(a => String(a).slice(0, 200));
    json = JSON.stringify(item);
  }
  return json.slice(0, LIMITE_PROPRIEDADE);
}

function hash_(texto) {
  return Utilities.computeDigest(Utilities.DigestAlgorithm.MD5, texto, Utilities.Charset.UTF_8)
    .map(b => (b < 0 ? b + 256 : b).toString(16).padStart(2, '0')).join('');
}

function formatar_(ms, padrao) {
  return Utilities.formatDate(new Date(ms), FUSO, padrao || 'dd/MM HH:mm');
}

function escapar_(t) {
  return String(t == null ? '' : t).replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}

function responder_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
