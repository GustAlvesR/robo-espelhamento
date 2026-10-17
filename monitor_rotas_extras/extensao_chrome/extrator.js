// Content script: a cada carregamento da página do AppSheet, lê a tela (via leitor.js) e manda para o background.
// Só LÊ a tela: nunca clica em ACEITAR nem em recusar.
(() => {
  if (window.__monitorRotasExtras) return;
  window.__monitorRotasExtras = true;

  criarLeitor(document).ler().then(async leitura => {
    try {
      await chrome.runtime.sendMessage({tipo: 'leitura', ...leitura});
    } catch (e) {
      console.warn('[Monitor rotas extras] background indisponível:', e);
    }
  });
})();
