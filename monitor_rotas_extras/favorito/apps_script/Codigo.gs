/**
 * Envio de e-mail do Monitor de Rotas Extras (Google Apps Script publicado como app da web).
 *
 * Propriedades do script (Configurações do projeto > Propriedades do script):
 *   TOKEN          senha longa qualquer; a mesma vai no favorito
 *   DESTINATARIOS  e-mails separados por vírgula
 *
 * Os destinatários ficam aqui, não no favorito: quem tiver a URL e o token só consegue mandar
 * para essa lista, nunca para outros endereços.
 */

const LIMITE_ASSUNTO = 250;

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

  const destinatarios = (props.getProperty('DESTINATARIOS') || '').split(/[,;\s]+/).filter(String).join(',');
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

// Abrir a URL /exec no navegador mostra se está no ar e quanto resta da cota (sem enviar nada).
function doGet() {
  return responder_({ok: true, servico: 'monitor-rotas-extras', cotaRestante: MailApp.getRemainingDailyQuota()});
}

// Rode uma vez pelo editor (botão Executar) para autorizar o envio e receber um e-mail de teste.
function testarEnvio() {
  const destinatarios = PropertiesService.getScriptProperties().getProperty('DESTINATARIOS');
  if (!destinatarios) throw new Error('Preencha a propriedade DESTINATARIOS antes de testar.');
  MailApp.sendEmail(destinatarios, 'Teste do Monitor de Rotas Extras', 'Se você recebeu, o envio de e-mail está funcionando.');
}

function responder_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
