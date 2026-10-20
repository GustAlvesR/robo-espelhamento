// Testa o Codigo.gs com dublês dos serviços do Apps Script. Rode: node --test favorito/apps_script/codigo.test.mjs
import {test} from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';

function carregar(props, cota = 100) {
  const enviados = [];
  const ctx = {
    PropertiesService: {getScriptProperties: () => ({getProperty: k => props[k] ?? null})},
    MailApp: {getRemainingDailyQuota: () => cota, sendEmail: (...a) => enviados.push(a)},
    ContentService: {MimeType: {JSON: 'json'},
      createTextOutput: t => ({setMimeType: () => JSON.parse(t)})},
  };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(new URL('./Codigo.gs', import.meta.url), 'utf8'), ctx);
  return {ctx, enviados};
}
const post = obj => ({postData: {contents: JSON.stringify(obj)}});

test('envia para os destinatários do script com token correto', () => {
  const {ctx, enviados} = carregar({TOKEN: 'segredo', DESTINATARIOS: 'a@x.com; b@x.com'});
  const r = ctx.doPost(post({token: 'segredo', assunto: 'Rota', html: '<b>oi</b>', texto: 'oi', to: 'invasor@x.com'}));
  assert.equal(r.ok, true);
  assert.equal(enviados.length, 1);
  assert.equal(enviados[0][0].to, 'a@x.com,b@x.com');
  assert.equal(enviados[0][0].subject, 'Rota');
});

test('recusa token errado, corpo inválido, sem destinatários e cota esgotada', () => {
  assert.equal(carregar({TOKEN: 's', DESTINATARIOS: 'a@x.com'}).ctx.doPost(post({token: 'x'})).erro, 'token inválido');
  assert.equal(carregar({TOKEN: 's', DESTINATARIOS: 'a@x.com'}).ctx.doPost({postData: {contents: '{'}}).erro, 'corpo inválido');
  assert.equal(carregar({TOKEN: '', DESTINATARIOS: 'a@x.com'}).ctx.doPost(post({token: ''})).erro, 'token inválido');
  assert.match(carregar({TOKEN: 's'}).ctx.doPost(post({token: 's'})).erro, /DESTINATARIOS/);
  const {ctx, enviados} = carregar({TOKEN: 's', DESTINATARIOS: 'a@x.com'}, 0);
  assert.match(ctx.doPost(post({token: 's'})).erro, /cota/);
  assert.equal(enviados.length, 0);
});
