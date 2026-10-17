// Rode com: node --test extensao_chrome/testes/nucleo.test.mjs  (o fuso é fixado em America/Sao_Paulo abaixo)
process.env.TZ = 'America/Sao_Paulo';
import {test} from 'node:test';
import assert from 'node:assert/strict';
import * as N from '../nucleo.js';

const NOME = 'BRSP21-BRSP11-SSP34.SOW.VEN';
const DESC = 'DATA E HORA ETA 16/10/2026 20:00  |  DATA E HORA CPT 16/10/2026 22:00';
const TEXTO = `1 - TOCO\nCAPTAÇÃO\n${NOME}\nTEMPO LIMITE DE RETORNO: 00:18:00\n${DESC}\nACEITAR`;
const bruto = (extra = {}) => ({label: NOME, labels: [NOME], descricao: DESC, texto: TEXTO, secao: 'FULLS SP', ...extra});
const CFG = {...N.CONFIG_PADRAO, urlRotas: 'https://www.appsheet.com/start/x#view=ROTA%20EXTRA'};
const AGORA = new Date(2026, 9, 16, 15, 0).getTime();

test('card completo', () => {
  const r = N.parseCard(bruto());
  assert.equal(r.nome, NOME);
  assert.equal(r.origem, 'BRSP21');
  assert.deepEqual([r.qtdVeiculos, r.veiculo, r.veiculoTexto], [1, 'TOCO', '1 - TOCO']);
  assert.equal(r.tipo, 'CAPTAÇÃO');
  assert.equal(r.eta, new Date(2026, 9, 16, 20, 0).getTime());
  assert.equal(r.cpt, new Date(2026, 9, 16, 22, 0).getTime());
  assert.equal(r.tempoLimiteTexto, '00:18:00');
  assert.deepEqual(r.errosParse, []);
});

test('chave = nome + ETA + CPT', () => {
  const a = N.parseCard(bruto());
  const b = N.parseCard(bruto({descricao: DESC.replace('20:00', '21:00')}));
  assert.equal(N.chave(a), `${NOME}|2026-10-16 20:00|2026-10-16 22:00`);
  assert.notEqual(N.chave(a), N.chave(b));
});

test('data inválida e datas ausentes', () => {
  const r = N.parseCard(bruto({descricao: 'DATA E HORA ETA 31/02/2026 20:00 | DATA E HORA CPT 16/10/2026 22:00'}));
  assert.equal(r.eta, null);
  assert.ok(r.errosParse.some(e => e.includes('ETA com data inválida')));
  const s = N.parseCard(bruto({descricao: '', texto: NOME}));
  assert.ok(N.validarDatas(s, AGORA).includes('ETA ausente ou ilegível'));
  assert.ok(N.chave(s).startsWith(NOME));
});

test('tipo COLETA não é confundido com ETA', () => {
  assert.equal(N.parseCard(bruto({texto: `1 - TRUCK\nCOLETA\n${NOME}\n${DESC}`})).tipo, 'COLETA');
});

test('nomenclatura', () => {
  for (const [nome, ok] of [[NOME, true], ['brsp21-brsp11-ssp34.sow.ven', true], ['BRSP21-BRSP11', false], ['', false]])
    assert.equal(N.nomeValido(nome, CFG.regexNome), ok, nome);
});

test('validação de datas', () => {
  const r = N.parseCard(bruto());
  assert.deepEqual(N.validarDatas(r, AGORA), []);
  assert.ok(N.validarDatas(r, new Date(2026, 9, 16, 20, 30).getTime()).some(a => a.includes('já passou')));
  const inv = N.parseCard(bruto({descricao: 'ETA 16/10/2026 23:00 | CPT 16/10/2026 22:00'}));
  assert.ok(N.validarDatas(inv, AGORA).some(a => a.includes('não é anterior')));
});

test('filtros', () => {
  const r = N.parseCard(bruto());
  assert.match(N.aplicarFiltros(r, {...CFG, veiculos: 'TRUCK'}, true, AGORA), /veículo TOCO/);
  assert.equal(N.aplicarFiltros(r, {...CFG, veiculos: 'toco, truck'}, true, AGORA), null);
  assert.equal(N.aplicarFiltros(r, {...CFG, origens: 'BRSP2'}, true, AGORA), null);
  assert.match(N.aplicarFiltros(r, {...CFG, origens: 'BRMG'}, true, AGORA), /origem BRSP21/);
  assert.equal(N.aplicarFiltros(r, {...CFG, origens: 'BRMG'}, false, AGORA), null);
  const quase = new Date(2026, 9, 16, 19, 30).getTime();
  assert.match(N.aplicarFiltros(r, {...CFG, etaMinimaMin: 60}, true, quase), /ETA em 30 min/);
});

test('mensagem e payload do Teams', () => {
  const r = N.parseCard(bruto());
  const av = N.avaliar(r, AGORA, CFG);
  const msg = N.mensagemRota(r, av, AGORA - 5 * 60000, AGORA, CFG);
  assert.equal(msg.titulo, `🚚 Rota extra ofertada: ${NOME}`);
  const fatos = Object.fromEntries(msg.secoes[0][1]);
  assert.equal(fatos['Veículo'], '1 - TOCO');
  assert.ok(fatos['Tempo restante (est.)'].startsWith('≤ 25 min'));
  assert.ok(fatos.ETA.includes('em 5h00'));
  const p = N.montarPayloadTeams(msg);
  assert.equal(p.type, 'message');
  assert.equal(p.attachments[0].contentType, 'application/vnd.microsoft.card.adaptive');
  assert.equal(p.attachments[0].content.version, '1.4');
  assert.equal(p.attachments[0].content.actions[0].url, CFG.urlRotas);

  const ruim = N.parseCard(bruto({label: 'XPTO', labels: ['XPTO']}));
  const m2 = N.mensagemRota(ruim, N.avaliar(ruim, AGORA, CFG), AGORA, AGORA, CFG);
  assert.match(m2.titulo, /nomenclatura fora do padrão/);
  assert.equal(m2.nivel, 'alerta');
});

test('estado: deduplicação, esquecimento e cooldown', () => {
  const est = N.estadoInicial();
  const r = N.parseCard(bruto());
  assert.deepEqual(N.registrarPresentes(est, [r], AGORA), [N.chave(r)]);
  assert.deepEqual(N.registrarPresentes(est, [r], AGORA + 60000), []);
  assert.deepEqual(N.esquecerAusentes(est, new Set(), AGORA + 5 * 60000, 10), []);
  assert.deepEqual(N.esquecerAusentes(est, new Set(), AGORA + 12 * 60000, 10), [N.chave(r)]);
  assert.ok(N.podeAlertar(est, 'x', AGORA, 30));
  est.alertas.x = AGORA;
  assert.ok(!N.podeAlertar(est, 'x', AGORA + 10 * 60000, 30));
  assert.ok(N.podeAlertar(est, 'x', AGORA + 31 * 60000, 30));
  assert.ok(!N.podeAlertar(est, 'x', AGORA + 99 * 60000, null));
});

test('texto para WhatsApp/Telegram e destinos do CallMeBot', () => {
  const r = N.parseCard(bruto());
  const msg = N.mensagemRota(r, N.avaliar(r, AGORA, CFG), AGORA, AGORA, CFG);
  const wa = N.montarTexto(msg, true);
  assert.ok(wa.startsWith(`*🚚 Rota extra ofertada: ${NOME}*`));
  assert.ok(wa.includes('Veículo: 1 - TOCO'));
  assert.ok(!N.montarTexto(msg, false).includes('*'));
  assert.deepEqual(N.destinosWhatsApp('+55 (11) 99999-8888, 123456\n\n5511888887777;654321\nlinha sem chave\nabc 999'),
    [{telefone: '+5511999998888', apikey: '123456'}, {telefone: '5511888887777', apikey: '654321'}]);
  const url = N.urlCallMeBot('+5511999998888', '123456', 'Olá & tchau');
  assert.equal(url, 'https://api.callmebot.com/whatsapp.php?phone=%2B5511999998888&text=Ol%C3%A1%20%26%20tchau&apikey=123456');
});
