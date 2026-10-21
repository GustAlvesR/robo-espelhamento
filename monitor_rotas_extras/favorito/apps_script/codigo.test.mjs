// Testa o Codigo.gs com dublês dos serviços do Apps Script. Rode: node --test favorito/apps_script/codigo.test.mjs
import {test} from 'node:test';
import assert from 'node:assert/strict';
import crypto from 'node:crypto';
import fs from 'node:fs';
import vm from 'node:vm';

const MIN = 60000;

function carregar(propsIniciais, {cota = 100, agora = Date.UTC(2026, 9, 21, 15, 0)} = {}) {
  const props = new Map(Object.entries(propsIniciais));
  const enviados = [];
  const gatilhos = [];
  const relogio = {agora};
  const ctx = {
    console: {log() {}, warn() {}},
    PropertiesService: {getScriptProperties: () => ({
      getProperty: k => (props.has(k) ? props.get(k) : null),
      setProperty: (k, v) => props.set(k, String(v)),
      deleteProperty: k => props.delete(k),
      getKeys: () => [...props.keys()],
    })},
    LockService: {getScriptLock: () => ({waitLock() {}, releaseLock() {}})},
    MailApp: {getRemainingDailyQuota: () => cota, sendEmail: (...a) => enviados.push(a)},
    ContentService: {MimeType: {JSON: 'json'}, createTextOutput: t => ({setMimeType: () => JSON.parse(t)})},
    Utilities: {
      DigestAlgorithm: {MD5: 'md5'}, Charset: {UTF_8: 'utf8'},
      computeDigest: (_alg, texto) => [...crypto.createHash('md5').update(texto, 'utf8').digest()].map(b => (b > 127 ? b - 256 : b)),
      formatDate: (d, _fuso, padrao) => {
        const sp = new Date(d.getTime() - 3 * 3600000);
        const p = n => String(n).padStart(2, '0');
        const hm = `${p(sp.getUTCHours())}:${p(sp.getUTCMinutes())}`;
        return padrao === 'HH:mm' ? hm : `${p(sp.getUTCDate())}/${p(sp.getUTCMonth() + 1)} ${hm}`;
      },
    },
    ScriptApp: {
      getProjectTriggers: () => gatilhos,
      deleteTrigger: g => gatilhos.splice(gatilhos.indexOf(g), 1),
      newTrigger: nome => ({timeBased: () => ({everyMinutes: m => ({create: () => gatilhos.push({
        getHandlerFunction: () => nome, minutos: m})})})}),
    },
    Date: class extends Date {
      constructor(...a) { if (a.length) super(...a); else super(relogio.agora); }
      static now() { return relogio.agora; }
    },
  };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(new URL('./Codigo.gs', import.meta.url), 'utf8'), ctx);
  return {ctx, props, enviados, gatilhos, relogio};
}

const post = obj => ({postData: {contents: JSON.stringify(obj)}});
const BASE = {TOKEN: 'segredo', DESTINATARIOS: 'a@x.com; b@x.com'};
const rota = (nome, extra = {}) => ({chave: `${nome}|eta|cpt`, nome, veiculo: '1 - TOCO', tipo: 'CAPTAÇÃO',
  origem: 'BRSP21', eta: Date.UTC(2026, 9, 21, 23, 0), cpt: Date.UTC(2026, 9, 22, 1, 0),
  tempoLimite: '00:18:00', primeiraVez: Date.UTC(2026, 9, 21, 14, 55), alertas: [], ...extra});

test('sem tipo: envia na hora para os destinatários do script (ignora "to" vindo de fora)', () => {
  const {ctx, enviados} = carregar(BASE);
  const r = ctx.doPost(post({token: 'segredo', assunto: 'Teste', html: '<b>oi</b>', texto: 'oi', to: 'invasor@x.com'}));
  assert.equal(r.ok, true);
  assert.equal(enviados[0][0].to, 'a@x.com,b@x.com');
});

test('recusa token errado, corpo inválido, sem destinatários e cota esgotada', () => {
  assert.equal(carregar(BASE).ctx.doPost(post({token: 'x'})).erro, 'token inválido');
  assert.equal(carregar(BASE).ctx.doPost({postData: {contents: '{'}}).erro, 'corpo inválido');
  assert.equal(carregar({TOKEN: ''}).ctx.doPost(post({token: ''})).erro, 'token inválido');
  assert.match(carregar({TOKEN: 's'}).ctx.doPost(post({token: 's'})).erro, /DESTINATARIOS/);
  assert.match(carregar(BASE, {cota: 0}).ctx.doPost(post({token: 'segredo'})).erro, /cota/);
});

test('aceita formulário e token com espaços', () => {
  const {ctx} = carregar({...BASE, TOKEN: ' segredo '});
  const dados = JSON.stringify({token: 'segredo  ', tipo: 'ping'});
  assert.equal(ctx.doPost({parameter: {dados}, postData: {contents: ''}}).ok, true);
});

test('rotas entram na fila sem duplicar e saem num único e-mail', () => {
  const {ctx, props, enviados, relogio} = carregar(BASE);
  for (const r of [rota('BRSP21-A.X'), rota('BRSP22-B.X', {alertas: ['ETA já passou']}), rota('BRSP21-A.X')]) {
    const resp = ctx.doPost(post({token: 'segredo', tipo: 'rota', rota: r}));
    assert.equal(resp.ok, true);
    assert.equal(resp.intervaloMin, 15);
  }
  assert.equal(ctx.doGet().naFila, 2);
  assert.equal(enviados.length, 0);

  relogio.agora += 10 * MIN;
  ctx.enviarResumo();
  assert.equal(enviados.length, 1);
  const email = enviados[0][0];
  assert.match(email.subject, /^\[Rotas Extras\] 2 rota\(s\) ofertada\(s\) até 12:10: BRSP21-A\.X, BRSP22-B\.X$/);
  assert.ok(email.htmlBody.includes('BRSP21-A.X') && email.htmlBody.includes('BRSP22-B.X'));
  assert.ok(email.htmlBody.includes('até ~12:25 (15 min)'));
  assert.ok(email.htmlBody.includes('ETA já passou'));
  assert.equal(ctx.doGet().naFila, 0);

  // fila vazia e monitor com sinal: nada sai; a mesma rota reenviada depois não volta para a fila
  ctx.doPost(post({token: 'segredo', tipo: 'rota', rota: rota('BRSP21-A.X')}));
  ctx.enviarResumo();
  assert.equal(enviados.length, 1);
  assert.ok([...props.keys()].some(k => k.startsWith('ENV_')));
});

test('avisa uma vez quando o favorito para de mandar sinal e quando volta', () => {
  const {ctx, enviados, relogio} = carregar(BASE);
  ctx.doPost(post({token: 'segredo', tipo: 'ping'}));
  relogio.agora += 15 * MIN;
  ctx.enviarResumo();
  assert.equal(enviados.length, 0);

  relogio.agora += 15 * MIN;
  ctx.enviarResumo();
  assert.equal(enviados.length, 1);
  assert.match(enviados[0][0].subject, /Aviso do monitor/);
  assert.ok(enviados[0][0].htmlBody.includes('monitor parou de ler a tela'));
  ctx.enviarResumo();
  assert.equal(enviados.length, 1);

  ctx.doPost(post({token: 'segredo', tipo: 'ping'}));
  ctx.enviarResumo();
  assert.equal(enviados.length, 2);
  assert.ok(enviados[1][0].htmlBody.includes('monitor voltou'));
});

test('sem cota suficiente, a fila espera o próximo ciclo', () => {
  const {ctx, enviados} = carregar(BASE, {cota: 1});
  ctx.doPost(post({token: 'segredo', tipo: 'rota', rota: rota('BRSP21-A.X')}));
  ctx.enviarResumo();
  assert.equal(enviados.length, 0);
  assert.equal(ctx.doGet().naFila, 1);
});

test('alertas do favorito entram no mesmo resumo', () => {
  const {ctx, enviados} = carregar(BASE);
  ctx.doPost(post({token: 'segredo', tipo: 'rota', rota: rota('BRSP21-A.X')}));
  ctx.doPost(post({token: 'segredo', tipo: 'alerta', alerta: {titulo: 'sessão expirada', texto: 'faça login'}}));
  ctx.enviarResumo();
  assert.equal(enviados.length, 1);
  assert.ok(enviados[0][0].htmlBody.includes('Avisos do monitor') && enviados[0][0].htmlBody.includes('sessão expirada'));
});

test('instalarGatilhos respeita INTERVALO_MIN e não duplica', () => {
  const {ctx, gatilhos} = carregar({...BASE, INTERVALO_MIN: '10'});
  ctx.instalarGatilhos();
  ctx.instalarGatilhos();
  assert.equal(gatilhos.length, 1);
  assert.equal(gatilhos[0].minutos, 10);
  const outro = carregar({...BASE, INTERVALO_MIN: '7'});
  outro.ctx.instalarGatilhos();
  assert.equal(outro.gatilhos[0].minutos, 15);
});
