// Regras puras do monitor (sem APIs do Chrome): parsing, validação, filtros, estado e mensagem do Teams.

export const CONFIG_PADRAO = {
  webhookUrl: '',
  urlRotas: '',
  intervaloS: 60,
  janelaOfertaMin: 30,
  esquecerAposMin: 10,
  regexNome: String.raw`^BR[A-Z]{2}\d+(-[A-Z0-9]+)+(\.[A-Z]+)+$`,
  veiculos: '',
  origens: '',
  etaMinimaMin: 0,
  heartbeatHora: '08:00',
  ciclosVaziosAlerta: 60,
  cooldownAlertasMin: 30,
  notificacaoLocal: true,
  // Um destino por linha: telefone,apikey (chave do CallMeBot de cada pessoa).
  whatsappDestinos: '',
  telegramToken: '',
  telegramChatIds: '',
  // E-mail via Google Apps Script publicado como app da web (ver favorito/apps_script/Codigo.gs).
  emailUrl: '',
  emailToken: '',
};

export function normalizar(s) {
  return (s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/\s+/g, ' ').trim().toUpperCase();
}

export function lista(texto) {
  return (texto || '').split(/[,;\n]/).map(s => s.trim()).filter(Boolean);
}

const DATA_HORA = String.raw`(\d{2}/\d{2}/\d{4})\s+(\d{1,2}:\d{2}(?::\d{2})?)`;
const RE_ETA = new RegExp(String.raw`\bETA\b\s*:?\s*` + DATA_HORA, 'i');
const RE_CPT = new RegExp(String.raw`\bCPT\b\s*:?\s*` + DATA_HORA, 'i');
const RE_TEMPO_LIMITE = /TEMPO\s+LIMITE\s+DE\s+RETORNO\s*:?\s*(\d{1,3}):(\d{2})(?::(\d{2}))?/i;
const RE_VEICULO = /^(\d+)\s*-\s*([A-Z0-9][A-Z0-9/ ]{1,29}?)$/;
const RE_NOME = /\bBR[A-Z]{2}\d+[-A-Z0-9.]*/;
const RE_TIPO = /^[A-Z][A-Z ]{2,29}$/;
const RE_LINHA_DATA = /\b(ETA|CPT)\b|TEMPO LIMITE/;
const IGNORADAS = new Set(['ACEITAR', 'RECUSAR', 'ABRIR ENDERECO']);

// Datas do card são horário de Brasília; o PC do operador está nesse fuso, então usamos a hora local.
export function parseDataHora(data, hora) {
  const [d, m, a] = data.split('/').map(Number);
  const [h, mi, s = 0] = hora.split(':').map(Number);
  const dt = new Date(a, m - 1, d, h, mi, s);
  if (dt.getFullYear() !== a || dt.getMonth() !== m - 1 || dt.getDate() !== d || h > 23 || mi > 59) return null;
  return dt.getTime();
}

export function extrairOrigem(nome) {
  const m = normalizar(nome).match(/^[A-Z0-9]+/);
  return m ? m[0] : null;
}

function escolherNome(labels, texto) {
  const limpos = labels.map(normalizar).filter(Boolean);
  const comPadrao = limpos.find(l => RE_NOME.test(l));
  if (comPadrao) return comPadrao;
  if (limpos.length) return limpos[0];
  const m = normalizar(texto).match(RE_NOME);
  return m ? m[0] : '';
}

export function parseCard(bruto) {
  const labels = bruto.labels && bruto.labels.length ? bruto.labels : [bruto.label || ''];
  const descricao = (bruto.descricao || '').trim();
  const texto = (bruto.texto || '').trim();
  const erros = [];
  const nome = escolherNome(labels, texto);
  if (!nome) erros.push('nome da rota não encontrado');

  const fonte = `${descricao}\n${texto}`;
  const datas = {};
  for (const [re, rotulo] of [[RE_ETA, 'ETA'], [RE_CPT, 'CPT']]) {
    const m = fonte.match(re);
    if (!m) continue;
    const valor = parseDataHora(m[1], m[2]);
    if (valor === null) erros.push(`${rotulo} com data inválida: ${m[1]} ${m[2]}`);
    datas[rotulo] = {valor, texto: `${m[1]} ${m[2]}`};
  }
  const tl = fonte.match(RE_TEMPO_LIMITE);
  const linhas = texto.split('\n').map(l => l.trim()).filter(Boolean);

  let qtd = null, veiculo = null, veiculoTexto = null;
  for (const l of linhas) {
    const m = normalizar(l).match(RE_VEICULO);
    if (m) { qtd = Number(m[1]); veiculo = m[2].trim(); veiculoTexto = l; break; }
  }
  const nomeN = normalizar(nome), secaoN = normalizar(bruto.secao);
  let tipo = null;
  for (const l of linhas) {
    const n = normalizar(l);
    if (IGNORADAS.has(n) || n === nomeN || n === secaoN || RE_VEICULO.test(n) || RE_LINHA_DATA.test(n)) continue;
    if (RE_TIPO.test(n)) { tipo = l; break; }
  }
  return {
    nome, origem: nome ? extrairOrigem(nome) : null, veiculo, qtdVeiculos: qtd, veiculoTexto, tipo,
    eta: datas.ETA ? datas.ETA.valor : null, cpt: datas.CPT ? datas.CPT.valor : null,
    etaTexto: datas.ETA ? datas.ETA.texto : null, cptTexto: datas.CPT ? datas.CPT.texto : null,
    tempoLimiteTexto: tl ? `${tl[1]}:${tl[2]}` + (tl[3] ? `:${tl[3]}` : '') : null,
    secao: bruto.secao || null, descricao, texto, errosParse: erros,
  };
}

function isoMin(ms) {
  const d = new Date(ms), p = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function chave(rota) {
  const eta = rota.eta !== null ? isoMin(rota.eta) : (rota.etaTexto || '');
  const cpt = rota.cpt !== null ? isoMin(rota.cpt) : (rota.cptTexto || '');
  let k = `${normalizar(rota.nome)}|${eta}|${cpt}`;
  if (!eta || !cpt) k += `|${normalizar(rota.descricao)}`;
  return k;
}

export function nomeValido(nome, regexStr) {
  return Boolean(nome) && new RegExp(regexStr).test(nome.trim().toUpperCase());
}

export function fmtData(ms) {
  if (ms === null || ms === undefined) return '?';
  const d = new Date(ms), p = n => String(n).padStart(2, '0');
  return `${p(d.getDate())}/${p(d.getMonth() + 1)}/${d.getFullYear()} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function fmtHora(ms) {
  const d = new Date(ms), p = n => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}`;
}

export function fmtDelta(ms) {
  const totalMin = Math.floor(Math.abs(ms) / 60000);
  const h = Math.floor(totalMin / 60), m = totalMin % 60;
  return h ? `${h}h${String(m).padStart(2, '0')}` : `${m} min`;
}

export function validarDatas(rota, agora) {
  const a = [];
  if (rota.eta === null) a.push('ETA ausente ou ilegível');
  if (rota.cpt === null) a.push('CPT ausente ou ilegível');
  if (rota.eta !== null && rota.cpt !== null && rota.eta >= rota.cpt)
    a.push(`ETA (${fmtData(rota.eta)}) não é anterior ao CPT (${fmtData(rota.cpt)})`);
  if (rota.eta !== null && rota.eta <= agora) a.push(`ETA (${fmtData(rota.eta)}) já passou`);
  return a;
}

// Nome fora do padrão não passa pelo filtro de origem (origem não confiável); veículo não lido não é filtrado.
export function aplicarFiltros(rota, cfg, nomeOk, agora) {
  const veiculos = lista(cfg.veiculos).map(normalizar);
  if (veiculos.length && rota.veiculo && !veiculos.includes(normalizar(rota.veiculo)))
    return `veículo ${rota.veiculo} fora dos permitidos`;
  const origens = lista(cfg.origens).map(normalizar);
  if (origens.length && nomeOk && rota.origem && !origens.some(o => normalizar(rota.origem).startsWith(o)))
    return `origem ${rota.origem} fora das permitidas`;
  const min = Number(cfg.etaMinimaMin) || 0;
  if (min && rota.eta !== null && rota.eta > agora && rota.eta - agora < min * 60000)
    return `ETA em ${Math.floor((rota.eta - agora) / 60000)} min (mínimo ${min})`;
  return null;
}

export function avaliar(rota, agora, cfg) {
  const nomeOk = nomeValido(rota.nome, cfg.regexNome);
  const anomalias = validarDatas(rota, agora);
  const filtro = aplicarFiltros(rota, cfg, nomeOk, agora);
  const alertas = [];
  if (!nomeOk) alertas.push(`Nomenclatura fora do padrão: ${rota.nome || '(vazio)'}`);
  alertas.push(...anomalias, ...rota.errosParse.map(e => `Parse: ${e}`));
  if (!rota.veiculo) alertas.push('Veículo não identificado no card');
  return {nomeOk, anomalias, filtro, alertas};
}

export function mensagemRota(rota, av, primeiraVez, agora, cfg) {
  const expira = primeiraVez + cfg.janelaOfertaMin * 60000;
  const restante = expira - agora;
  const restanteTxt = restante > 0 ? `≤ ${fmtDelta(restante)} (até ~${fmtHora(expira)})`
    : `provavelmente expirada (~${fmtHora(expira)})`;
  let etaTxt = rota.eta !== null ? fmtData(rota.eta) : (rota.etaTexto || '?');
  if (rota.eta !== null) etaTxt += ` (${rota.eta > agora ? 'em' : 'há'} ${fmtDelta(rota.eta - agora)})`;
  const fatos = [
    ['Rota', rota.nome || '?'],
    ['Veículo', rota.veiculoTexto || rota.veiculo || '?'],
    ['Tipo', rota.tipo || '-'],
    ['Origem', rota.origem || '?'],
    ['ETA', etaTxt],
    ['CPT', rota.cpt !== null ? fmtData(rota.cpt) : (rota.cptTexto || '?')],
    ['Tempo limite retorno', rota.tempoLimiteTexto || '-'],
    ['Tempo restante (est.)', restanteTxt],
    ['Seção', rota.secao || '-'],
    ['Detectada em', fmtData(primeiraVez)],
  ];
  let nivel = av.alertas.length ? 'alerta' : 'info';
  let prefixo = av.alertas.length ? 'Rota extra ofertada (com alertas)' : 'Rota extra ofertada';
  if (!av.nomeOk) prefixo = 'Rota extra com nomenclatura fora do padrão';
  return {
    titulo: `🚚 ${prefixo}: ${rota.nome || '?'}`,
    resumo: `${rota.veiculo || '?'} | ETA ${fmtData(rota.eta)} | CPT ${fmtData(rota.cpt)} | restam ${restanteTxt}`,
    nivel, secoes: [[null, fatos]], alertas: av.alertas, link: cfg.urlRotas || null,
  };
}

const COR = {info: 'Accent', sucesso: 'Good', alerta: 'Warning', erro: 'Attention'};

export function montarCard(msg) {
  const corpo = [
    {type: 'TextBlock', text: msg.titulo, weight: 'Bolder', size: 'Medium', wrap: true, color: COR[msg.nivel] || 'Default'},
    {type: 'TextBlock', text: msg.resumo, wrap: true, spacing: 'Small'},
  ];
  if (msg.alertas && msg.alertas.length)
    corpo.push({type: 'Container', style: 'attention', spacing: 'Medium',
      items: msg.alertas.map(a => ({type: 'TextBlock', text: `⚠ ${a}`, wrap: true, color: 'Attention'}))});
  for (const [titulo, fatos] of msg.secoes || []) {
    if (titulo) corpo.push({type: 'TextBlock', text: titulo, weight: 'Bolder', spacing: 'Medium', separator: true});
    corpo.push({type: 'FactSet', facts: fatos.map(([k, v]) => ({title: k, value: v || '-'}))});
  }
  const card = {type: 'AdaptiveCard', $schema: 'http://adaptivecards.io/schemas/adaptive-card.json',
    version: '1.4', msteams: {width: 'Full'}, body: corpo};
  if (msg.link) card.actions = [{type: 'Action.OpenUrl', title: 'Abrir no AppSheet', url: msg.link}];
  return card;
}

export function montarPayloadTeams(msg) {
  return {type: 'message', summary: msg.titulo, attachments: [{
    contentType: 'application/vnd.microsoft.card.adaptive', contentUrl: null, content: montarCard(msg)}]};
}

// Texto simples para WhatsApp/Telegram. negrito=true usa a marcação *texto* do WhatsApp.
export function montarTexto(msg, negrito = true) {
  const b = t => (negrito ? `*${t}*` : t);
  const linhas = [b(msg.titulo), msg.resumo];
  for (const a of msg.alertas || []) linhas.push(`⚠ ${a}`);
  for (const [titulo, fatos] of msg.secoes || []) {
    linhas.push('');
    if (titulo) linhas.push(b(titulo));
    for (const [k, v] of fatos) linhas.push(`${k}: ${v || '-'}`);
  }
  return linhas.join('\n').trim();
}

const escapar = t => String(t ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));

export function montarHtmlEmail(msg) {
  const partes = [`<h2 style="margin:0 0 4px">${escapar(msg.titulo)}</h2><p style="margin:0 0 10px">${escapar(msg.resumo)}</p>`];
  if (msg.alertas && msg.alertas.length) {
    partes.push('<div style="background:#fde7e9;padding:8px 10px;border-left:4px solid #c50f1f;margin-bottom:10px">'
      + '<b>Alertas</b><ul style="margin:4px 0">' + msg.alertas.map(a => `<li>${escapar(a)}</li>`).join('') + '</ul></div>');
  }
  for (const [titulo, fatos] of msg.secoes || []) {
    if (titulo) partes.push(`<h3 style="margin:10px 0 4px">${escapar(titulo)}</h3>`);
    partes.push('<table style="border-collapse:collapse;font-size:13px">' + fatos.map(([k, v]) =>
      `<tr><td style="padding:2px 14px 2px 0;color:#555">${escapar(k)}</td><td>${escapar(v || '-')}</td></tr>`).join('') + '</table>');
  }
  if (msg.link) partes.push(`<p><a href="${escapar(msg.link)}">Abrir no AppSheet</a></p>`);
  return `<div style="font-family:Segoe UI,Arial,sans-serif">${partes.join('')}</div>`;
}

export function destinosWhatsApp(texto) {
  // Telefone pode ter espaços e pontuação; a chave é o que vem depois da última vírgula/ponto e vírgula.
  return (texto || '').split('\n').map(l => l.trim()).filter(Boolean).map(l => {
    const m = l.match(/^(.+)[,;]\s*(\S+)$/) || l.match(/^(\S+)\s+(\S+)$/);
    return m ? {telefone: m[1].replace(/[^\d+]/g, ''), apikey: m[2]} : null;
  }).filter(d => d && /\d{8,}/.test(d.telefone));
}

export function urlCallMeBot(telefone, apikey, texto) {
  return 'https://api.callmebot.com/whatsapp.php?phone=' + encodeURIComponent(telefone)
    + '&text=' + encodeURIComponent(texto) + '&apikey=' + encodeURIComponent(apikey);
}

// ---------------- estado ----------------

export function estadoInicial() {
  return {rotas: {}, alertas: {}, abaId: null, ultimaLeitura: null, ciclosVazios: 0, heartbeatData: null,
    stats: statsZerados(), eventos: [], ultimoCiclo: null};
}

export function statsZerados() {
  return {leituras: 0, rotasNovas: 0, notificadas: 0, filtradas: 0, falhasEnvio: 0, ultimaRotaEm: null};
}

export function registrarPresentes(est, rotas, agora) {
  const novas = [];
  for (const r of rotas) {
    const k = chave(r);
    if (!est.rotas[k]) {
      est.rotas[k] = {nome: r.nome, primeiraVez: agora, ultimaVez: agora, status: 'pendente', tentativas: 0};
      novas.push(k);
    } else {
      est.rotas[k].ultimaVez = agora;
    }
  }
  if (novas.length) { est.stats.rotasNovas += novas.length; est.stats.ultimaRotaEm = agora; }
  return novas;
}

export function esquecerAusentes(est, presentes, agora, aposMin) {
  const removidas = [];
  for (const [k, r] of Object.entries(est.rotas)) {
    if (!presentes.has(k) && agora - r.ultimaVez > aposMin * 60000) { delete est.rotas[k]; removidas.push(k); }
  }
  return removidas;
}

// cooldownMin null = alerta uma vez até limpar.
export function podeAlertar(est, tipo, agora, cooldownMin) {
  const ultimo = est.alertas[tipo];
  if (ultimo === undefined) return true;
  if (cooldownMin === null) return false;
  return agora - ultimo >= cooldownMin * 60000;
}

export function registrarEvento(est, agora, texto) {
  est.eventos.unshift({em: agora, texto});
  est.eventos.length = Math.min(est.eventos.length, 40);
}
