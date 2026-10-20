# Monitor de Rotas Extras (AppSheet TORRE DE CONTROLE)

Robô que abre a tela MENU > ROTA EXTRA do app AppSheet no Google Chrome (ou no Edge), lê os cards a cada 45 s,
cruza a origem da rota com a TABELA DE ENDEREÇO e avisa no Teams (com e-mail via Outlook como reserva)
cada rota nova uma única vez. O robô só LÊ a tela: nunca clica em ACEITAR nem em recusar. O aceite
continua sendo feito por uma pessoa do time, no app.

Este projeto é independente do robô de espelhamento que está na raiz do repositório.

Há três formas de rodar, da mais simples para o PC da empresa à mais completa:

- Favorito do Chrome (`favorito/`, recomendada no PC corporativo): abra `favorito/instalar_favorito.html`,
  arraste o botão para a barra de favoritos e clique nele com a tela ROTA EXTRA aberta. Roda na aba em que
  você já está logado; não precisa de Python, extensão, modo desenvolvedor nem login novo. Para se a aba
  for recarregada ou fechada (é só clicar de novo). Avisa por e-mail (Google Apps Script, código em
  `favorito/apps_script/Codigo.gs`), Teams, WhatsApp (via CallMeBot) e/ou Telegram; instruções de cada
  canal na própria página de instalação.
- Extensão do Chrome (`extensao_chrome/`): mais robusta que o favorito, mas exige "Modo do desenvolvedor",
  que muitas empresas bloqueiam.
- Robô Python (o resto desta pasta): faz tudo, inclusive o cruzamento com a tabela de endereço, mas precisa
  de um login que o Google aceite no navegador do robô. Em alguns PCs/contas o Google bloqueia
  ("Esse navegador ou app pode não ser seguro").

O favorito e a extensão ainda não cruzam a rota com a TABELA DE ENDEREÇO.

## Estrutura

```
monitor_rotas_extras/
  rotas_extras/
    config.py          carrega config.yaml + .env e valida
    seletores.py       TODOS os seletores e textos de tela (único lugar para ajustar)
    parser.py          parsing do card (veículo, origem, ETA, CPT, tempo limite) e validações
    regras.py          filtros e alertas agregados
    address_lookup.py  leitura da tabela de endereço, agrupamento de linhas, cache e cruzamento com o hub
    scraper.py         Chrome/Edge via Playwright: recarregar tela, extrair cards (somente leitura), dumps
    notifier.py        Adaptive Card no Teams (Power Automate) e e-mail via Outlook, com retry
    state.py           estado em JSON (deduplicação, cooldown de alertas)
    main.py            loop, resiliência, heartbeat e modos de diagnóstico
  favorito/            favorito do Chrome (bookmarklet): favorito.js + gerar.py -> instalar_favorito.html
  extensao_chrome/     extensão do Chrome; leitor.js e nucleo.js também são usados pelo favorito
  tests/               pytest (unitários + integração com páginas que imitam o AppSheet)
  config.example.yaml  configuração comentada
  .env.example         segredos (webhook, e-mails)
  instalar.bat         instalação em um clique (ambiente virtual + dependências + config)
  iniciar_monitor.bat  inicia o robô (também usado pelo Agendador de Tarefas)
```

Gerados em tempo de execução (fora do Git): `config.yaml`, `.env`, `perfil_navegador/` (login), `dados/`
(estado e cache da tabela), `logs/`, `dumps/`.

## 1. Instalação no Windows corporativo (sem administrador)

Baixar o código: no GitHub, abra a branch do projeto > botão "Code" > "Download ZIP", extraia e entre na
pasta `monitor_rotas_extras`. (Ou use o ZIP enviado na conversa, que já é só esta pasta.)

Caminho rápido: instale o Python (passo 1) e dê dois cliques em `instalar.bat`. Ele faz os passos 2 e 3,
cria `config.yaml` e `.env` e mostra o que falta preencher. Depois:

```bat
iniciar_monitor.bat --login         rem 1ª vez: login no AppSheet
iniciar_monitor.bat --test-notify   rem testa o Teams
iniciar_monitor.bat --once --dry-run
iniciar_monitor.bat                 rem deixa rodando
```

Passo a passo manual:

1. Python 3.10 ou superior. Se não tiver, use o instalador do python.org com "Install Now" (instala só
   para o seu usuário, sem admin) e marque "Add python.exe to PATH". Alternativas: Microsoft Store ou o
   portal de software da empresa.
2. Abra o Prompt de Comando na pasta `monitor_rotas_extras` e crie o ambiente virtual:

   ```bat
   py -m venv .venv
   .venv\Scripts\activate
   python -m pip install --upgrade pip
   pip install -r requirements.txt
   ```

   Atrás de proxy: `pip install --proxy http://usuario@proxy.empresa:porta -r requirements.txt`
   (ou `set HTTPS_PROXY=http://proxy.empresa:porta` antes do pip).

3. NÃO rode `playwright install`. O robô usa o Google Chrome já instalado (`canal: chrome`). Para usar o
   Edge, troque para `canal: msedge`.

Dependências: `playwright`, `PyYAML`, `tzdata` e `pywin32` (só para o e-mail). O que pode ser bloqueado:

- `playwright` traz um `node.exe` dentro de `site-packages\playwright\driver`. Alguns antivírus colocam
  esse arquivo em quarentena; se o robô falhar ao iniciar com erro de "driver", peça liberação da pasta.
- `pywin32` às vezes é barrado por política. Sem ele o e-mail não funciona, mas o Teams sim.

## 2. Configuração

```bat
copy config.example.yaml config.yaml
copy .env.example .env
```

- `config.yaml` > `app.url_rotas`: abra MENU > ROTA EXTRA no Chrome e copie a URL inteira da barra de
  endereço (ela termina com algo como `#...view=ROTA%20EXTRA`).
- `app.url_tabela_endereco`: faça o mesmo na TABELA DE ENDEREÇO. Recomendado: sem isso o robô tenta
  abrir pelo menu ou pelo pin do primeiro card, e a tela aberta pelo pin pode vir filtrada.
- `.env`: `ROTAS_TEAMS_WEBHOOK_URL` e `ROTAS_EMAIL_DESTINATARIOS`. Nada de credencial no código.

Chave desconhecida no YAML gera erro de propósito (um erro de digitação não passa despercebido).

## 3. Primeiro login (SSO)

```bat
iniciar_monitor.bat --login
```

O Google bloqueia login em navegador controlado por automação ("Esse navegador ou app pode não ser
seguro"). Por isso o `--login` abre um Chrome NORMAL, sem automação, usando a pasta de perfil do robô
(`perfil_navegador/`). É uma janela separada do seu Chrome do dia a dia, com outra pasta de dados, então
você precisa logar nela uma vez:

1. Faça o login do AppSheet/Google nessa janela, como no dia a dia (inclusive verificação em 2 etapas).
2. Confira que a tela ROTA EXTRA abriu.
3. Feche essa janela do Chrome e pressione ENTER no prompt.

Em seguida o robô abre o perfil sozinho e confirma se enxerga a tela de rotas. O login fica salvo no
perfil; nenhuma senha é guardada pelo robô. Se a sessão expirar, chega um alerta e basta repetir o `--login`
(com o robô parado).

Por que não usar direto o Chrome onde você já está logado: o perfil fica travado enquanto o Chrome estiver
aberto, e as versões recentes do Chrome não permitem automação no perfil padrão. Por isso o robô tem o
próprio perfil. Se o Chrome não for encontrado, informe o caminho do `chrome.exe` em
`navegador.executavel` (normalmente `C:\Program Files\Google\Chrome\Application\chrome.exe`).

## 4. Webhook do Teams (Power Automate / Workflows)

1. No Teams, vá no canal (ou chat) que deve receber os avisos > "..." > Workflows.
2. Escolha o modelo "Enviar alertas de webhook para um canal" (Post to a channel when a webhook request is
   received) ou, para receber push no celular, "Enviar alertas de webhook para um chat".
3. Dê um nome, confirme a conexão, escolha a equipe/canal (ou o chat) e crie. Copie a URL gerada.
4. Cole em `.env` como `ROTAS_TEAMS_WEBHOOK_URL=...`.
5. Teste: `python -m rotas_extras --test-notify`

Payload enviado (formato que o modelo do Workflows espera: um `message` com anexos Adaptive Card):

```json
{
  "type": "message",
  "summary": "🚚 Nova rota extra: BRSP21-BRSP11-SSP34.SOW.VEN",
  "attachments": [{
    "contentType": "application/vnd.microsoft.card.adaptive",
    "contentUrl": null,
    "content": {
      "type": "AdaptiveCard",
      "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
      "version": "1.4",
      "msteams": {"width": "Full"},
      "body": [
        {"type": "TextBlock", "text": "🚚 Nova rota extra: BRSP21-BRSP11-SSP34.SOW.VEN", "weight": "Bolder"},
        {"type": "TextBlock", "text": "TOCO | ETA 16/10/2026 20:00 | CPT 16/10/2026 22:00 | restam ≤ 28 min"},
        {"type": "FactSet", "facts": [{"title": "Veículo", "value": "1 - TOCO"}, {"title": "ETA", "value": "..."}]},
        {"type": "TextBlock", "text": "Hub de origem", "weight": "Bolder"},
        {"type": "FactSet", "facts": [{"title": "Situação", "value": "OK"}, {"title": "Endereço", "value": "..."}]}
      ],
      "actions": [{"type": "Action.OpenUrl", "title": "Abrir no AppSheet", "url": "https://www.appsheet.com/..."}]
    }
  }]
}
```

Observações:

- A resposta esperada é HTTP 202. Um 401/403 costuma significar que o gatilho foi configurado para aceitar
  só usuários autenticados do tenant; no fluxo, em "Quem pode disparar o fluxo", precisa estar "Qualquer
  pessoa". Se a política da empresa não permitir, use só o e-mail.
- A URL tem uma assinatura (`sig=...`) e funciona como senha. O robô nunca a escreve no log.
- Post em canal só gera notificação no celular se você seguir o canal com "Todas as atividades".
  Para alerta garantido no celular, prefira o modelo de chat.
- O proxy precisa liberar o host da URL (`*.logic.azure.com` ou `*.api.powerplatform.com`).

E-mail (reserva): usa o Outlook desktop já logado, via COM, sem senha. Só é usado se o Teams falhar.

## 5. Testar antes de deixar rodando

```bat
python -m rotas_extras --once --dry-run          rem um ciclo, só loga, não envia nem grava estado
python -m rotas_extras --once --dry-run --dump-html   rem idem + salva HTML/screenshot/cards em dumps\
python -m rotas_extras --refresh-table           rem lê a tabela de endereço e atualiza o cache
python -m rotas_extras --discover-table          rem diagnóstico da estrutura da tabela (ver seção 6)
python -m rotas_extras --once                    rem um ciclo real (notifica)
python -m rotas_extras                           rem loop contínuo (Ctrl+C para parar)
```

Em `logs/monitor.log` cada ciclo registra quantos cards leu, quantas rotas eram novas e o que foi feito.

## 6. Diagnóstico da tabela (--discover-table): o que me enviar

A leitura da tabela usa os `span[data-testid="text-type-display-span"]` com a posição na tela: agrupa em
linhas por Y (ou pelo elemento de linha, se existir) e mapeia colunas pela posição X dos títulos
(comparando texto sem acento). Como ainda não vimos o HTML da tabela, rode:

```bat
python -m rotas_extras --discover-table
```

Ele imprime, para "Vitória" e "MACRO REGIÃO", a cadeia de ancestrais (tag, role, data-testid, nº de filhos,
nº de spans de célula, posição, trecho do texto), as contagens de `role=row`, `tr` etc., os títulos
encontrados e as primeiras linhas agrupadas. Também salva em `dumps\discover_AAAAMMDD_HHMMSS\`:
`discover_table.json`, `tabela.html`, `tabela.png` e `linhas_lidas.json`.

Me envie o `discover_table.json` (e, se puder, o `tabela.png`). Com isso ajusto `estrategia_linhas` e
`seletor_linha` (`config.yaml` > `hub.tabela`) ou a função `agrupar_linhas` em `address_lookup.py`.

Para outros textos-alvo: `--discover-text "Vitória" "MACRO REGIÃO" "PERFIL RESTRIÇÃO"`.

## 7. Iniciar com o Windows (Agendador de Tarefas, sem admin)

1. Abra "Agendador de Tarefas" > "Criar Tarefa..." (não a básica).
2. Geral: nome "Monitor Rotas Extras"; "Executar somente quando o usuário estiver conectado"
   (necessário para a janela do navegador, o login e o Outlook).
3. Disparadores: Novo > "Ao fazer logon" > Usuário específico (você) > "Atrasar a tarefa por: 1 minuto".
4. Ações: Novo > Iniciar um programa:
   - Programa: `C:\caminho\monitor_rotas_extras\iniciar_monitor.bat`
   - Iniciar em: `C:\caminho\monitor_rotas_extras`
5. Condições: desmarque "Iniciar a tarefa somente se o computador estiver ligado à rede elétrica".
6. Configurações: desmarque "Interromper a tarefa se ela for executada por mais de"; marque "Se a tarefa
   falhar, reiniciar a cada 5 minutos, até 3 vezes"; "Se a tarefa já estiver em execução: Não iniciar uma
   nova instância" (duas instâncias brigariam pelo mesmo perfil do navegador).

Se o Agendador estiver bloqueado por política, coloque um atalho para o `iniciar_monitor.bat` em
`shell:startup` (Win+R > `shell:startup`).

Deixe o PC sem suspender (Configurações > Sistema > Energia > Suspender: Nunca). A tela pode bloquear:
o navegador continua rodando, mas se o computador suspender o robô para.

## 8. Como funciona

- Ciclo: recarrega a tela, espera os cards (ou a lista vazia), rola para carregar cards sob demanda e
  extrai todos. O card de cada botão ACEITAR é o menor ancestral com exatamente um ACEITAR e com
  `[id^="label"]` e `[id^="description"]`.
- Chave de deduplicação: nome + ETA + CPT. Cada chave é avisada uma vez. Rota que some da tela por mais de
  `esquecer_apos_min` é esquecida (se voltar, é avisada de novo). Se o envio falhar em todos os canais, a
  rota continua pendente e é reenviada no próximo ciclo.
- Tempo restante: 30 min contados da primeira vez que o robô viu a rota. É uma estimativa otimista: a
  rota pode ter sido publicada até um intervalo antes.
- Validações que viram alerta na mensagem (não descartam a rota): nomenclatura fora do padrão, ETA >= CPT,
  ETA no passado, datas ausentes, veículo não identificado.
- Filtros (descartam): veículo fora da lista, origem fora dos prefixos (não se aplica a nome fora do
  padrão, porque a origem não é confiável), ETA em menos de `eta_minima_min` (ETA já passada não é
  filtrada, sai como anomalia).
- Hub: procura a ORIGEM como palavra inteira em Localização Fixa, depois MICRO REGIÃO, depois Operação.
  Uma linha (ou linhas equivalentes) = usa; hubs diferentes = "AMBÍGUO" na mensagem; nenhuma = "SEM_MATCH"
  e a origem vai para `logs/origens_sem_match.log` (corrija com `hub.mapeamento_manual`).
  Restrição: termos de `termos_alerta` geram ALERTA; com `perfil_indica: proibidos`, veículo citado no
  PERFIL RESTRIÇÃO = INCOMPATÍVEL e não citado = compatível (CARRETA no perfil, rota TOCO = ok).
- Alertas de sistema (Teams/e-mail): sessão expirada, ciclos falhando, 0 cards por muitos ciclos, cards sem
  ACEITAR reconhecível, falha ao ler a tabela. Heartbeat diário com o resumo.

## 9. Solução de problemas

- Proxy: o Chrome/Edge usa o proxy do Windows sozinho. O envio ao Teams usa o proxy do sistema (variáveis
  `HTTPS_PROXY` ou configuração do Windows). Se o proxy usa arquivo PAC ou autenticação NTLM, o Python pode
  não conseguir: defina `notificacao.proxy` com o endereço do proxy ou peça ao TI a liberação do host do
  webhook. Erro `CERTIFICATE_VERIFY_FAILED`: o Python no Windows usa os certificados do sistema; se ainda
  assim falhar, a inspeção TLS da empresa está usando um certificado fora do repositório do Windows.
- "Esse navegador ou app pode não ser seguro" no login do Google: acontece se você tentar logar na janela
  controlada pelo robô. Pare o robô e use `iniciar_monitor.bat --login`, que abre um Chrome comum.
- Sessão expirada: chega um alerta "sessão expirada". Pare o robô e rode `--login` de novo.
- Navegador não abre / "Browser closed": feche as janelas que estejam usando `perfil_navegador`. Veja
  `chrome://policy` (ou `edge://policy`): se `RemoteDebuggingAllowed` estiver desativada, o Playwright não
  consegue controlar o navegador; peça liberação ao TI ou teste o outro navegador (`canal: msedge`).
  Se aparecer "chrome executable not found", preencha `navegador.executavel` com o caminho do `chrome.exe`.
- Antivírus: pode bloquear o `node.exe` do Playwright ou o próprio navegador em modo automação. Peça exceção
  para a pasta do projeto e do ambiente virtual.
- E-mail não sai: o "Novo Outlook" não tem COM; precisa do Outlook clássico. Com o Outlook fechado, o
  e-mail pode ficar na Caixa de Saída até ele abrir.
- `ZoneInfoNotFoundError`: falta o pacote `tzdata` (`pip install tzdata`).
- 0 cards com rotas visíveis na tela: rode `--once --dry-run --dump-html` e me envie o `pagina.html` e o
  `cards.json`; os seletores ficam todos em `rotas_extras/seletores.py`.
- Logs: `logs/monitor.log` (rotação em 5 x 5 MB). Use `--verbose` para nível DEBUG.

## 10. Riscos e limitações

- Depende do layout do AppSheet. IDs e classes mudam; usamos aria-label, role, data-testid, prefixo de id e
  texto, mas uma mudança no app (renomear "ACEITAR", trocar o tipo de view) quebra a leitura. Há alertas
  para isso, não imunidade.
- O PC precisa estar ligado, logado e sem suspender. Queda de rede, VPN ou reinício do Windows interrompem
  a leitura (o Agendador religa no logon).
- Atraso real: até um intervalo (45 s) + tempo de recarregar o app + envio. Notificação de canal no Teams
  pode chegar com atraso no celular.
- O AppSheet pode mostrar dados do cache local por alguns segundos após recarregar; o robô espera a rede
  ficar ociosa, mas não há garantia absoluta de que leu a sincronização mais recente.
- A leitura da tabela de endereço ainda não foi validada no HTML real (falta a saída do `--discover-table`).
  O cruzamento origem x hub depende do conteúdo real de Localização Fixa/Micro Região, que eu não conheço.
- Políticas: automação de navegador e uso de webhook externo podem esbarrar em regras da empresa ou do
  Mercado Livre. O robô só lê a tela (não aceita nem recusa), o que reduz o risco, mas vale avisar o gestor.
- Recarregar o app a cada 45 s gera tráfego e leituras no AppSheet; se o app tiver cota ou auditoria de
  acesso, isso pode chamar atenção. Ajuste `intervalo_s` se necessário.

## 11. Testes

```bat
pip install -r requirements-dev.txt
python -m pytest
```

Os testes de integração (`test_dom_integracao.py`, `test_monitor_e2e.py`) abrem páginas em
`tests/fixtures` que imitam o HTML do AppSheet. No Windows usam o Chrome (ou `set PW_CANAL=msedge` antes do
pytest); sem navegador disponível eles são pulados.
