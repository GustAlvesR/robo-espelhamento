import {CONFIG_PADRAO, fmtData} from './nucleo.js';

const CAMPOS = ['webhookUrl', 'urlRotas', 'intervaloS', 'janelaOfertaMin', 'heartbeatHora', 'veiculos', 'origens',
  'etaMinimaMin', 'regexNome'];
const $ = id => document.getElementById(id);

function aviso(texto, ok) {
  const m = $('msg');
  m.textContent = texto;
  m.className = ok ? 'ok' : 'erro';
}

async function carregar() {
  const {config = {}} = await chrome.storage.local.get('config');
  const cfg = {...CONFIG_PADRAO, ...config};
  for (const c of CAMPOS) $(c).value = cfg[c];
  $('notificacaoLocal').checked = cfg.notificacaoLocal;
}

function validar(cfg) {
  if (cfg.webhookUrl && !/^https:\/\//.test(cfg.webhookUrl)) return 'A URL do webhook deve começar com https://';
  if (cfg.urlRotas && !/^https:\/\/([a-z0-9-]+\.)*appsheet\.com\//i.test(cfg.urlRotas)) return 'A URL da tela deve ser do appsheet.com';
  if (!(cfg.intervaloS >= 30)) return 'Intervalo mínimo: 30 s';
  if (!/^\d{2}:\d{2}$/.test(cfg.heartbeatHora)) return 'Horário do resumo no formato HH:MM';
  try { new RegExp(cfg.regexNome); } catch (e) { return 'Regex da nomenclatura inválida'; }
  return null;
}

async function salvar() {
  const cfg = {};
  for (const c of CAMPOS) cfg[c] = $(c).value.trim();
  for (const c of ['intervaloS', 'janelaOfertaMin', 'etaMinimaMin']) cfg[c] = Number(cfg[c]);
  cfg.notificacaoLocal = $('notificacaoLocal').checked;
  const erro = validar(cfg);
  if (erro) return aviso(erro, false);
  await chrome.storage.local.set({config: cfg});
  aviso('Salvo.', true);
}

async function testar() {
  aviso('Enviando...', true);
  const r = await chrome.runtime.sendMessage({tipo: 'teste'});
  aviso(r.ok ? 'Teste enviado. Confira o Teams.' : r.erro, r.ok);
}

async function atualizarStatus() {
  const {estado} = await chrome.storage.local.get('estado');
  const e = estado || {rotas: {}, eventos: [], stats: {}};
  const pend = Object.values(e.rotas).filter(r => r.status === 'pendente').length;
  const linhas = [
    ['Aba monitorada', e.abaId !== null && e.abaId !== undefined ? `#${e.abaId}` : 'nenhuma (abra MENU > ROTA EXTRA)'],
    ['Última leitura', e.ultimaLeitura ? fmtData(e.ultimaLeitura) : 'nenhuma ainda'],
    ['Último ciclo', e.ultimoCiclo ? `${e.ultimoCiclo.cards} card(s), ${e.ultimoCiclo.novas} nova(s)` : '-'],
    ['Rotas em memória', `${Object.keys(e.rotas).length} (${pend} aguardando envio)`],
    ['Desde o último resumo', e.stats ? `${e.stats.notificadas || 0} avisada(s), ${e.stats.filtradas || 0} filtrada(s)` : '-'],
  ];
  $('status').innerHTML = '';
  for (const [k, v] of linhas) {
    const tr = document.createElement('tr');
    tr.innerHTML = '<td></td><td></td>';
    tr.children[0].textContent = k;
    tr.children[1].textContent = v;
    $('status').appendChild(tr);
  }
  $('eventos').innerHTML = '';
  for (const ev of e.eventos || []) {
    const div = document.createElement('div');
    div.textContent = `${fmtData(ev.em)}  ${ev.texto}`;
    $('eventos').appendChild(div);
  }
  if (!(e.eventos || []).length) $('eventos').textContent = 'Nenhum evento ainda.';
}

$('salvar').addEventListener('click', salvar);
$('teste').addEventListener('click', testar);
$('cicloAgora').addEventListener('click', async () => {
  await chrome.runtime.sendMessage({tipo: 'ciclo_agora'});
  aviso('Leitura solicitada; o status atualiza em alguns segundos.', true);
});
chrome.storage.onChanged.addListener(atualizarStatus);
carregar();
atualizarStatus();
