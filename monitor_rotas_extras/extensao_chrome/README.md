# Extensão do Chrome: Monitor de Rotas Extras

Roda dentro do Chrome em que você já está logado no AppSheet. Por isso não depende de login automatizado
(que o Google bloqueia com "Esse navegador ou app pode não ser seguro") nem de Python.

O que faz: a cada 60 s recarrega a aba MENU > ROTA EXTRA, lê os cards (sem clicar em nada), e avisa no
Teams cada rota nova uma única vez, com veículo, ETA, CPT, tempo restante estimado e alertas
(nomenclatura fora do padrão, datas inconsistentes). Também mostra uma notificação do Windows no PC.

Ainda não faz: o cruzamento com a TABELA DE ENDEREÇO (endereço/restrição do hub). Isso existe só no robô
Python por enquanto.

## Instalar (uma vez)

1. Extraia o ZIP. A pasta que interessa é `extensao_chrome`.
2. No Chrome, abra `chrome://extensions`.
3. Ligue "Modo do desenvolvedor" (canto superior direito).
4. Clique em "Carregar sem compactação" e escolha a pasta `extensao_chrome`.
5. Fixe o ícone: clique no quebra-cabeça da barra do Chrome e no alfinete de "Monitor de Rotas Extras".

Se o botão "Modo do desenvolvedor" estiver bloqueado ou a extensão for removida sozinha, é política da
empresa; aí só o TI libera.

## Configurar

1. Clique no ícone da extensão: abre a página de opções.
2. Cole a URL do webhook do Teams (como criar: README principal, seção 4) e clique em Salvar.
3. Clique em "Enviar teste ao Teams" e confira se o card chegou.
4. Opcional: cole a URL da tela ROTA EXTRA. Com ela, a extensão abre sozinha uma aba fixada se a aba for
   fechada. Sem ela, basta deixar uma aba aberta em MENU > ROTA EXTRA.
5. Opcional: filtros de veículo, origem e ETA mínima.

## Usar

- Deixe uma aba do Chrome em MENU > ROTA EXTRA. Use outras abas para trabalhar: essa é recarregada sozinha.
- O ideal é deixar essa aba numa janela própria, sem minimizar. Com a janela minimizada ou coberta, o
  Chrome reduz o ritmo da página e a leitura pode atrasar.
- A página de opções mostra a aba monitorada, a última leitura e os últimos eventos.
- Se a sessão do AppSheet expirar, chega um alerta. Faça login na própria aba (é o Chrome normal) e o
  monitor volta sozinho.
- O Chrome e o PC precisam ficar ligados. Se o Chrome fechar, a extensão para junto.

## Limitações

- Depende do layout do AppSheet (os seletores estão no início de `extrator.js`).
- "Tempo restante" conta 30 min a partir da primeira vez que a extensão viu a rota (estimativa otimista).
- O Chrome às vezes mostra um aviso sobre extensões em modo desenvolvedor ao abrir; pode ignorar.
- Sem o cruzamento com a tabela de endereço (ver acima).

## Testes

```bat
node --test extensao_chrome\testes\nucleo.test.mjs
```

O teste de ponta a ponta (`tests/test_extensao.py`) carrega a extensão num Chromium e precisa de
`PW_EXECUTAVEL` apontando para um Chromium (o Chrome comum não aceita carregar extensão por linha de comando).
