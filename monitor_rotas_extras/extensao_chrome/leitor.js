// Leitura da tela ROTA EXTRA, compartilhada pela extensão e pelo favorito. Só LÊ: nunca clica em nada.
// Recebe o documento a ler (a própria página ou o de um iframe do mesmo app).
// IDs e classes do AppSheet são gerados; usamos aria-label, role, data-testid, prefixo de id e texto.
function criarLeitor(doc) {
  const win = doc.defaultView;
  const SEL = {
    aceitar: ['[role="button"][aria-label="ACEITAR" i]'],
    aceitarTexto: 'ACEITAR',
    cardLabel: '[id^="label"]',
    cardDescricao: '[id^="description"]',
    cardAcoes: ['i.fa-times-circle', '[aria-label="ABRIR ENDEREÇO" i]', 'i.fa-map-marker'],
    breadcrumbAtual: '[aria-current="page"]',
    camposLogin: 'input[type="password"], input[name="identifier"]',
  };
  const TEXTOS_LOGIN = ['SIGN IN WITH GOOGLE', 'FAZER LOGIN COM O GOOGLE', 'SIGN IN TO CONTINUE', 'FACA LOGIN PARA CONTINUAR'];

  const norm = s => (s || '').normalize('NFD').replace(/[̀-ͯ]/g, '').replace(/\s+/g, ' ').trim().toUpperCase();
  const textoDe = e => (e.innerText || e.textContent || '').trim();
  const esperar = ms => new Promise(r => setTimeout(r, ms));

  const botoesAceitar = () => {
    for (const s of SEL.aceitar) { const l = [...doc.querySelectorAll(s)]; if (l.length) return l; }
    return [...doc.querySelectorAll('[role="button"]')].filter(b => norm(b.textContent) === SEL.aceitarTexto);
  };

  const rolavel = el => {
    for (let e = el && el.parentElement; e && e !== doc.body && e !== doc.documentElement; e = e.parentElement) {
      const ov = win.getComputedStyle(e).overflowY;
      if (/(auto|scroll|overlay)/.test(ov) && e.scrollHeight > e.clientHeight + 1) return e;
    }
    return doc.scrollingElement;
  };

  // Card = menor ancestral com exatamente um ACEITAR e com label + descrição (com fallbacks).
  const cardDe = (botao, botoes) => {
    let comDesc = null, comAcoes = null, comLabel = null;
    for (let el = botao.parentElement; el && el !== doc.body; el = el.parentElement) {
      let n = 0;
      for (const b of botoes) if (el.contains(b) && ++n > 1) break;
      if (n > 1) break;
      if (!el.querySelector(SEL.cardLabel)) continue;
      if (el.querySelector(SEL.cardDescricao)) { comDesc = el; break; }
      if (!comAcoes && SEL.cardAcoes.some(s => el.querySelector(s))) comAcoes = el;
      if (!comLabel) comLabel = el;
    }
    return comDesc || comAcoes || comLabel;
  };

  const secaoDe = (card, botoes) => {
    let el = card;
    for (let d = 0; el && el !== doc.body && d < 8; d++, el = el.parentElement) {
      for (let s = el.previousElementSibling; s; s = s.previousElementSibling) {
        if (botoes.some(b => s.contains(b))) continue;
        const t = textoDe(s);
        if (t && t.length <= 60 && !t.includes('\n')) return t;
      }
    }
    return null;
  };

  const lerCards = () => {
    const botoes = botoesAceitar();
    const cards = [];
    let semCard = 0;
    for (const b of botoes) {
      const c = cardDe(b, botoes);
      if (!c) { semCard++; continue; }
      const labels = [...c.querySelectorAll(SEL.cardLabel)].map(textoDe).filter(Boolean);
      const descs = [...c.querySelectorAll(SEL.cardDescricao)].map(textoDe).filter(Boolean);
      cards.push({label: labels[0] || '', labels, descricao: descs.join('\n'), texto: textoDe(c), secao: secaoDe(c, botoes)});
    }
    return {cards, totalBotoes: botoes.length, semCard, labelsTotal: doc.querySelectorAll(SEL.cardLabel).length};
  };

  // Espera o DOM ficar sem mudanças por `quieto` ms (ou estoura em `maximo`).
  const aguardarEstavel = (quieto, maximo) => new Promise(resolve => {
    let t;
    const obs = new win.MutationObserver(() => { clearTimeout(t); t = setTimeout(fim, quieto); });
    const limite = setTimeout(fim, maximo);
    function fim() { obs.disconnect(); clearTimeout(t); clearTimeout(limite); resolve(); }
    obs.observe(doc.body, {childList: true, subtree: true, characterData: true});
    t = setTimeout(fim, quieto);
  });

  const telaPronta = () => Boolean(botoesAceitar().length || doc.querySelector(SEL.breadcrumbAtual)
    || doc.querySelector(SEL.cardLabel) || doc.querySelector(SEL.camposLogin));

  const ehLogin = () => {
    if (doc.querySelector(SEL.camposLogin)) return true;
    const corpo = norm(doc.body ? doc.body.innerText.slice(0, 5000) : '');
    return TEXTOS_LOGIN.some(t => corpo.includes(t));
  };

  const ehRotaExtra = () => {
    const atual = doc.querySelector(SEL.breadcrumbAtual);
    if (atual && norm(textoDe(atual)).includes('ROTA EXTRA')) return true;
    let hash = '';
    try { hash = decodeURIComponent(win.location.hash); } catch (e) { hash = win.location.hash; }
    return norm(hash).includes('VIEW=ROTA EXTRA') || botoesAceitar().length > 0;
  };

  async function lerComRolagem() {
    const vistos = new Map();
    const acc = {totalBotoes: 0, semCard: 0, labelsTotal: 0};
    for (let i = 0; i < 60; i++) {
      const r = lerCards();
      acc.totalBotoes = Math.max(acc.totalBotoes, r.totalBotoes);
      acc.semCard = Math.max(acc.semCard, r.semCard);
      acc.labelsTotal = Math.max(acc.labelsTotal, r.labelsTotal);
      for (const c of r.cards) { const k = norm(c.label) + '|' + norm(c.descricao); if (!vistos.has(k)) vistos.set(k, c); }
      const bs = botoesAceitar();
      const el = bs.length ? rolavel(bs[bs.length - 1]) : doc.scrollingElement;
      const antes = el.scrollTop, altura = el.scrollHeight;
      el.scrollTop = antes + Math.max(200, el.clientHeight * 0.8);
      if (el.scrollTop <= antes + 1) {
        // Fim da lista: dá uma chance para carga sob demanda trazer mais cards.
        await aguardarEstavel(800, 2500);
        if (el.scrollHeight <= altura + 1 && botoesAceitar().length <= bs.length) break;
      } else {
        await aguardarEstavel(400, 2000);
      }
    }
    const bs = botoesAceitar();
    (bs.length ? rolavel(bs[0]) : doc.scrollingElement).scrollTop = 0;
    return {cards: [...vistos.values()], ...acc};
  }

  // Espera a tela carregar e devolve a leitura completa (ou o motivo de não ter lido).
  async function ler(maxEsperaMs = 45000) {
    const inicio = Date.now();
    while (!telaPronta() && Date.now() - inicio < maxEsperaMs) await esperar(1000);
    await aguardarEstavel(1500, 10000);
    const login = ehLogin();
    const rotaExtra = !login && ehRotaExtra();
    const leitura = rotaExtra ? await lerComRolagem() : {cards: [], totalBotoes: 0, semCard: 0, labelsTotal: 0};
    return {url: win.location.href, login, rotaExtra, ...leitura};
  }

  return {ler, lerComRolagem, telaPronta, ehLogin, ehRotaExtra};
}
