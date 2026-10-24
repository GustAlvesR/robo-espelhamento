# Site Torre LH (mapa + TomTom)

O site roda dentro do próprio robô (mesmo Web Service no Render, mesmo comando
`python monitor_espelhamento.py`). Sem `TOMTOM_API_KEY` o robô continua
funcionando como antes, só sem mapa e sem previsão.

## Arquivos novos no repositório

- `geo_tomtom.py`: estações, posição do veículo, rotas e previsão.
- `site_web.py`, `templates/`, `static/`: o site.
- `criar_usuario.py`: gera a linha de cada usuário.
- `testar_tomtom.py`: confere se a chave funciona em cada API.
- `noticias.py` e `dados/municipios.csv`: notícias de ocorrência nas rodovias (ver
  "Notícias no caminho"). A lista de municípios, com as coordenadas da sede, vem do
  projeto kelvins/municipios-brasileiros (licença MIT em `dados/municipios.LICENSE`).
- `monitor_espelhamento.py` e `requirements.txt`: atualizados.

## 1. Chave TomTom

1. A chave colada no chat deve ser trocada: gere outra no portal da TomTom e apague a antiga.
2. No seu PC, dentro da pasta do projeto:
   ```powershell
   pip install -r requirements.txt
   $env:TOMTOM_API_KEY="NOVA_CHAVE"; python testar_tomtom.py
   ```
   As cinco linhas precisam sair `OK`. Se alguma sair `FALHA`, me mande a saída.

A chave nunca vai para o navegador: os tiles do mapa passam pelo servidor, e só
para quem está logado.

## 2. Variáveis no Render (Environment)

| Variável | Valor |
|---|---|
| `TOMTOM_API_KEY` | a chave nova |
| `SECRET_KEY` | gere com `python -c "import secrets; print(secrets.token_hex(32))"` |

Opcionais: `TOMTOM_MAX_CHAMADAS_DIA` (padrão 2000), `TOLERANCIA_ATRASO_MIN` (30),
`ETA_MAX_IDADE_POSICAO_MIN` (180), `ETA_RECALCULO_MIN` (30).

## 3. Usuários

Aba `Usuarios_Site` da planilha Acompanhamento Telemetria:

`Email | Nome | Senha_Hash | Ativo | Trocar_Senha`

- A primeira coluna pode ser um e-mail ou um usuário curto (`rodrigo.galani`). O login
  aceita os dois, sem diferenciar maiúsculas.
- Usuário novo: rode no PC `python criar_usuario.py rodrigo.galani "Rodrigo Galani"`,
  digite uma senha provisória e cole a linha gerada (já sai com `Trocar_Senha` = Sim).
  No primeiro acesso, o site só abre a tela "Trocar senha"; depois de trocar, a coluna
  vira `Não` sozinha.
- Qualquer pessoa troca a própria senha pelo link "Trocar senha" no topo do site
  (mínimo 10 caracteres). Trocar a senha derruba as outras sessões abertas daquele usuário.
- Esqueceu a senha: gere uma provisória com o `criar_usuario.py`, cole só o
  `Senha_Hash` na linha da pessoa e marque `Trocar_Senha` como `Sim`.
- Para bloquear alguém, troque `Ativo` para `Não` (vale em até 5 minutos).
- Cinco senhas erradas seguidas bloqueiam o IP por 15 minutos.

## 4. Aba Estacoes (revise uma vez)

O robô cria a aba `Estacoes` sozinho e preenche as coordenadas pelo nome que a
Angellira dá à estação (SOC_MG_BETIM vira "BETIM, MG"). Isso é coordenada do
centro da cidade, não do galpão. Para cada estação:

- Linha com `preencher`: o robô não achou. Digite latitude e longitude (no
  Google Maps, botão direito no galpão copia as coordenadas) e troque a Fonte
  para `manual`.
- Linha `auto (cidade)`: está aproximada. Corrigir para o galpão melhora a
  rota e a regra de "No destino" (5 km).

O robô nunca sobrescreve uma linha com latitude e longitude preenchidas.

## 5. Monitor anti-hibernação

Aponte o UptimeRobot (ou cron-job.org) para `https://SEU-APP.onrender.com/health`
a cada 5 minutos. A página inicial agora exige login, então o monitor precisa
usar `/health`.

## O que o robô acrescenta nas planilhas

Quatro colunas no fim de `Resultado_Espelhamento` e do histórico:

- `Previsão TomTom`: chegada estimada.
- `Desvio vs Fim Previsto (min)`: positivo é atraso.
- `Risco de Atraso`: o atraso vale até a SM ser encerrada na Angellira.
  - Com a chegada realizada da DBLH: `Chegou no prazo` ou `Chegou com atraso`.
  - SM encerrada sem chegada na DBLH: `Encerrada no prazo` ou `Encerrada com atraso`.
  - Passou do fim previsto sem chegada: `Atrasada`, mesmo perto do destino ou com a
    Angellira dizendo 100% percorrido (já veio 100% com o caminhão a 80 km).
  - Antes do fim previsto: `Atraso provável` (previsão da TomTom passa da tolerância),
    `No prazo`, `No destino` (a até 5 km do destino), `Posição antiga`, `Sem posição`,
    `Sem coord. da estação`.
- `Status Viagem`: cópia da coluna `status_corrigido` da Telemetria (PRE VIAGEM,
  EM TRANSITO). Se a coluna sumir, sai vazia e o site usa o horário de início previsto.
- `Alerta Chuva (INMET)` (última coluna): alertas de chuva do INMET cuja área o
  caminho que falta percorrer atravessa, do mais grave para o menos, por exemplo
  `Perigo: Chuvas Intensas até 21/10 10:00`. Vazio quando não há alerta.
- `Motorista` (depois de `Alerta Rotograma`): nome da coluna `Driver_Name`
  da Telemetria; se ela vier vazia, o `MOTORISTA` do relatório da Angellira. Quando o
  rastreador não identifica o motorista (vazio, "MOTORISTA NÃO LOCALIZADO", "EM ABERTO",
  "-"), vale o `MOTORISTA` do Team CEVA (abas `LH SEMANA ATUAL` e `LH PROXIMA SEMANA`,
  pela `LH TRIP`) e, por último, o `driver_name` da aba `DBLH` (espelho da CEVA_LH).
- `Fonte Motorista`: de onde veio o nome (`Rastreador`, `Team CEVA` ou `DBLH`). No site
  e na mensagem ao GR, o nome que não veio do rastreador aparece com a fonte ao lado.
- `SM Encerrada Em` (a última): quando a SM da viagem foi encerrada na Angellira. O
  relatório do Tracking só traz SMs em andamento, então é a hora em que a viagem saiu
  dele (ou em que a SITUAÇÃO passou a finalizada). A macro "FIM DE VIAGEM" do
  motorista não encerra. Para não errar: só vale a SM vista depois do início previsto e
  com saída prevista a até 6 h dele (`SM_TOLERANCIA_SAIDA_H`), o sumiço precisa se
  repetir em dois relatórios atualizados, e se mais da metade das viagens some de uma
  vez nada é encerrado. `Viagem Encerrada (Seguradora)` passa a seguir essa mesma regra.
- A telemetria (`% Espelhamento`) continua pela regra da Shopee: até a chegada realizada.
- `Alerta Acidente` (a última): ver "Acidentes no caminho".

## Uso do mapa

- Topo em duas faixas de altura fixa: marca, situação do ciclo e da Angellira e o menu
  com o nome do usuário (tema, trocar senha, sair); embaixo, os indicadores numa linha
  só, que rola para o lado se não couberem. Indicador zerado fica apagado, sem sumir.
- Na lista vão as duas etiquetas mais graves de cada rota; as outras viram "+N" (passe
  o mouse para ver). Todas aparecem no detalhe da rota.
- A legenda do mapa fica recolhida em "Legenda"; o site lembra se ela ficou aberta.

- O trajeto previsto só aparece na rota selecionada. Clicar num caminhão ou num item da
  lista isola aquela rota e mostra o trajeto. Para voltar:
  botão "Ver todos", clique numa área vazia do mapa ou tecla Esc.
- Rota isolada:
  - tracejado azul = viagem não iniciada (trajeto planejado);
  - cinza com pontos = já percorrido. Cada ponto é uma posição recebida da Angellira
    desde o início da viagem (o robô guarda até 300 por rota, no estado); entre um
    ponto e outro a linha é reta, porque não há o caminho real. A posição vem do texto
    da Angellira (cidade ou ponto de referência), não de GPS;
  - azul = melhor caminho agora: rota mais rápida da TomTom, para caminhão e com o
    trânsito do momento, da última posição até o destino. É recalculada quando chega
    posição nova ou a cada 30 minutos, na mesma chamada que já fazia a previsão de
    chegada (não gasta cota a mais). O detalhe mostra a distância, a hora do cálculo
    e o atraso por trânsito;
  - pontilhado cinza = trajeto planejado origem → destino, para comparar.
  Sem posições ainda, vale o jeito antigo: trajeto planejado cortado no ponto mais
  próximo do caminhão (sem corte se ele estiver a mais de 30 km do trajeto).
- A lista vem por gravidade: sem sinal agora, NOK, atraso provável e o resto.
- Filtro "Trecho": lista os pares origem → destino das rotas carregadas, com a cidade
  de cada sigla (ex.: Betim/MG → Recife/PE · SOC-MG2 → SOC-PE4) e quantas rotas há em
  cada um. A busca também aceita o nome da cidade e o do motorista, sem diferenciar
  acento nem maiúsculas ("joao" acha "JOÃO"). Quando a rota aparece pelo motorista,
  o nome dele surge na lista.
- As cidades vêm de `dados/estacoes_shopee.csv`, gerado da planilha de endereços da
  Shopee. Quando a planilha mudar: `pip install openpyxl`, depois
  `python importar_estacoes.py "Endereços e CNPJ.xlsx"` e commit do CSV. Endereço e
  CNPJ não entram no arquivo.
- Botão "Camadas" no canto do mapa, com as estações da Shopee e o clima. Estações: mostra os HUBs (last mile, códigos `HUB-`),
  FM HUBs (`FMH-`), SOCs (`SOC-`) e as outras estações da aba `Estacoes` que têm
  latitude e longitude. Começa tudo desligado. A lista vem dentro da página, como
  as rotas (a aba é relida a cada 10 minutos), porque há redes que bloqueiam as
  chamadas em segundo plano. A escolha não fica salva: ao abrir o site de novo,
  as estações voltam escondidas. Passe o mouse no losango para ver o código e o nome.
- "Angellira: há X min" no topo é a posição mais recente do último relatório importado;
  fica vermelho acima de 1 hora.
- "Exportar CSV" baixa a lista com os filtros atuais (separador `;`, abre direto no Excel).
- O seletor "Tema" no topo escolhe claro, escuro ou automático (segue o computador).
  A escolha fica salva no navegador de cada pessoa. Se você definiu
  `TOMTOM_TILE_URL` no Render, troque `style=street-light` por `style={estilo}`.

## Como a previsão é calculada (e onde ela erra)

- Com posição da Angellira: rota de caminhão com trânsito atual da posição até
  o destino. Em movimento, o tempo conta a partir da hora da posição; parado
  (velocidade 0), conta a partir de agora, ou do início previsto se ainda não saiu.
- Sem posição e antes da saída: início previsto + tempo da rota TomTom.
- A posição vem do texto da Angellira, não de GPS. O mapa mostra a cidade
  (contorno tracejado) ou o ponto de referência quando ele cai a menos de 30 km
  da cidade. A previsão é tão atual quanto a última importação do relatório.
- A TomTom não conhece as paradas obrigatórias do motorista (descanso da Lei
  do Motorista). Em viagens longas, isso deixa a previsão otimista.

## Alertas de chuva (INMET)

- Fonte: feed oficial de avisos do INMET (`https://apiprevmet3.inmet.gov.br/avisos/rss`),
  um aviso por item, no padrão CAP (evento, severidade, vigência e polígono da área).
  Gratuito, sem chave. O robô relê o feed a cada 30 minutos e só baixa os avisos novos.
- Entram os avisos de chuva e tempestade vigentes e os que começam nas próximas 12 horas.
  Cores: azul claro = Perigo Potencial, azul médio = Perigo, azul escuro = Grande Perigo (azul para não se confundir com atraso e sem sinal; o nível também aparece escrito na etiqueta).
- Para cada rota, o robô verifica se o caminho que falta percorrer (o azul da TomTom) ou,
  antes da viagem, o trajeto planejado passa por alguma área com alerta. As rotas
  marcadas ganham a etiqueta "Chuva" na lista, a seção "Chuva no caminho" no detalhe e
  entram no KPI "Chuva no caminho" (clique para filtrar).
- No mapa, "Camadas" → "Alertas de chuva (INMET)" desenha as áreas. Começa desligado.
- É alerta de área, não previsão hora a hora: diz onde o INMET espera chuva forte, não a
  que horas vai chover no ponto em que o caminhão estará. A previsão por ponto fica para
  a integração com o AccuWeather, quando houver a chave.
- Confira no log do Render: `INMET: N aviso(s) no feed, M área(s) de chuva vigente(s)`.
  Se aparecer `INMET: falha ao ler o feed`, o motivo vem junto (alguns serviços do governo
  bloqueiam acessos de fora do Brasil, e o Render não tem servidor no Brasil). O resto do
  robô segue funcionando.
- Falha momentânea do INMET (conexão derrubada, servidor fora do ar): o robô tenta de
  novo na hora (2 e 6 segundos depois) e, se continuar falhando, a cada 5 minutos
  (`INMET_RETENTATIVA_MIN`). Enquanto isso, valem os alertas da última leitura boa,
  menos os que já venceram. A camada do mapa mostra de quando é a leitura.
- Variáveis opcionais: `INMET_ATIVO=0` desliga; `INMET_ATUALIZAR_MIN` (30),
  `INMET_ANTECEDENCIA_H` (12), `INMET_MAX_NOVOS_POR_CICLO` (40), `INMET_RSS_URL`.

## Acidentes no caminho

Dupla checagem, com prioridade para o motorista:

- Motorista: macro com "ACIDENTE" na última mensagem da Angellira (por exemplo
  `ACIDENTE NA PISTA`). Vira o alerta principal (etiqueta vermelha cheia, primeiro da
  lista) e vale até ele mandar outra mensagem, no máximo 6 h (`ACIDENTE_MOTORISTA_MAX_H`).
  Só conta com a viagem em andamento: a macro da viagem anterior do caminhão não entra.
- TomTom: na mesma chamada que calcula a chegada (sem custo extra), a rota até o
  destino vem com os trechos de trânsito. Acidente (causa TPEG 2), interdição e
  congestionamento com 15 min ou mais de atraso (`CONGESTIONAMENTO_MIN_ATRASO`) aparecem
  como losangos na rota selecionada; acidente e interdição também vão para o alerta.
- Se a TomTom vê acidente a até 20 km do caminhão (`ACIDENTE_CONFIRMA_KM`) quando o
  motorista avisa, o alerta diz "TomTom confirma". Só a TomTom: etiqueta vermelha de
  contorno, "Acidente (TomTom)" ou "Interdição (TomTom)".
- O trânsito da TomTom só existe para caminhões com posição recente (o ETA é
  recalculado a cada posição nova, no máximo a cada 30 min).
- Todo ciclo o log traz `TomTom trânsito: X de Y rota(s) com o caminho até o destino
  consultado, Z trecho(s) de trânsito recebido(s); ...`. Z acima de zero mostra que a
  TomTom está mandando os trechos (inclusive os pequenos, que o filtro descarta); X
  baixo quer dizer caminhões sem posição recente da Angellira.
- O site atende 32 pedidos ao mesmo tempo (`SITE_THREADS`): depois de um deploy, os
  navegadores abertos recarregam o mapa juntos.

## Rota alternativa para o GR

Quando há ocorrência à frente, o robô monta o pedido de alteração de rota para o GR
validar (grupo Alinhamento GR). Nada vai direto para o motorista.

- Caminho de referência: o rotograma do trecho; sem rotograma, a rota planejada.
- Desvio da TomTom: se o caminho mais rápido com o trânsito de agora se afastou da rota
  planejada (mais de 5 km, `ALTERNATIVA_DESVIO_KM`), o robô calcula o caminho de
  referência com trânsito. Se ele tem acidente, interdição ou congestionamento e o mais
  rápido chega 30 min antes ou mais (`ALTERNATIVA_ECONOMIA_MIN`), vira sugestão.
- Aviso do motorista: a TomTom não sabe do acidente nem do atraso; o robô calcula o
  contorno dos próximos 6 km do caminho (`ALTERNATIVA_EVITAR_KM`) e mostra quanto custa.
- Os dois ETAs saem do mesmo momento: o de quem continua no trajeto atual (pelo caminho
  de referência, com o trânsito de agora) e o da rota alternativa. No aviso do
  motorista, o ETA do trajeto atual não inclui a retenção do acidente, que ninguém sabe
  quanto dura; quando o contorno custa tempo, a mensagem diz a partir de quantos minutos
  de retenção ele compensa.
- A mensagem ao GR vem formatada para o WhatsApp (negrito, emojis e uma informação por
  linha): viagem, placas, motorista, trecho, SM, posição, chegada planejada, ocorrência,
  os dois ETAs com km, o ganho ou custo e o link do trajeto.
- Custo: só esses dois casos chamam a TomTom, no máximo 6 vezes por ciclo
  (`ALTERNATIVA_MAX_CHAMADAS_CICLO`), e cada rota é recalculada a cada 30 min.
- No site: indicador "Rota alternativa p/ GR", etiqueta "Desvio −X min" (ou "Contorno"),
  linha verde tracejada no mapa e, no detalhe, os botões "Abrir no Google Maps",
  "Enviar ao GR (WhatsApp)" (abre o WhatsApp com a mensagem pronta; escolha o grupo
  Alinhamento GR, porque o link não abre grupo direto) e "Copiar mensagem".
- O link do Google Maps leva pontos de passagem tirados da rota de caminhão da TomTom,
  mas o Google Maps comum calcula como carro: entre os pontos ele pode escolher outra via.
- Na planilha, a coluna `Rota Alternativa` (a última) guarda o resumo e o link.
- Notícia no caminho de uma rota com pedido de rota alternativa entra na mensagem ao GR,
  logo abaixo da ocorrência.

## Notícias no caminho (Google Notícias)

Complemento da TomTom e do aviso do motorista: notícia sai de 30 min a 2 h depois do
fato e quase nunca diz o km, mas conta o que eles não contam (quanto deve durar uma
interdição, se a pista foi liberada, manifestação, queda de barreira).

- A cada 15 min (`NOTICIAS_ATUALIZAR_MIN`), o robô busca no Google Notícias as notícias
  das últimas 6 h (`NOTICIAS_MAX_H`) com termos de ocorrência (acidente, interdição,
  bloqueio, tombamento, queda de barreira, manifestação, pista liberada): uma consulta
  geral ("rodovia" ou "estrada") e uma por rodovia. As rodovias são as dos rotogramas
  mais a lista `NOTICIAS_RODOVIAS` (BR-116, BR-101, BR-381, BR-040, BR-251, BR-135,
  BR-324, BR-262, BR-050, BR-153, BR-232, BR-104, BR-146), no máximo 20 consultas
  (`NOTICIAS_MAX_CONSULTAS`).
- A notícia só vale para a rota quando cita uma cidade (nome com maiúscula, como
  "Feira de Santana") que fica a até 20 km (`NOTICIAS_RAIO_KM`) do caminho que falta
  percorrer, nos próximos 600 km (`NOTICIAS_KM_FRENTE`). O caminho é o da TomTom e o
  do rotograma a partir do caminhão; antes do início, o rotograma ou o planejado.
  Com a UF citada ("(BA)", "/MG", "na Bahia"), ficam só as cidades dessa UF.
- Notícia sem cidade ("acidente na Serra do Cafezal") não tem como ser ligada a uma
  rota e fica de fora. Cidade homônima perto da rota pode trazer notícia de outro lugar:
  o título e o link estão no detalhe para conferir.
- No site: indicador "Notícia no caminho", etiqueta "Notícia" (ou "Pista liberada",
  que não conta no indicador), marcador "N" na cidade e, no detalhe, a seção "Notícias
  no caminho" com o título (link para a matéria), cidade, km, rodovia, veículo e hora.
- Na planilha, a coluna `Alerta Notícia` guarda o resumo.
- Rotas canceladas, encerradas ou no destino ficam de fora.
- Falha no Google Notícias não para o robô: duas consultas seguidas com erro encerram a
  rodada, e a próxima vem 15 min depois. O log mostra a cada rodada quantas consultas
  deram certo e, a cada ciclo, quantas rotas têm notícia.
- `NOTICIAS_ATIVO=0` desliga.

## Rotogramas (Angellira)

- Ficam em `dados/rotogramas/ORIGEM__DESTINO.json`, um por trecho, gerados de cada PDF
  com `python importar_rotograma.py SM39635748.PDF` (precisa de `pip install pdfplumber`).
  Rotograma novo do mesmo trecho substitui o anterior. Do PDF só vai para o arquivo a
  rota: SM, origem, destino, pontos autorizados, áreas de risco e rota detalhada.
  Motorista, CPF, placas e telefones ficam de fora, e o `.gitignore` bloqueia os PDFs.
- Trechos cadastrados: SOC-SP8 → SOC-MG4 (SBC x Contagem), SOC-PE2 → SOC-MG2
  (Recife x Betim) e SOC-MG2 → SOC-PE4 (Betim x Recife).
- O robô localiza cada ponto e cada área de risco pela TomTom (nome do posto perto do
  centro da cidade; se não achar, fica a posição da cidade), no máximo 40 chamadas por
  ciclo (`ROTOGRAMA_MAX_CHAMADAS_CICLO`), com cache. A primeira vez leva cerca de uma hora.
- A cidade só é aceita se a TomTom a devolver dentro do estado pedido: "Ribeirão, PE" já
  veio em Goiás e "Escada, PE" em Minas, e os postos dessas cidades foram parar lá. Isso
  vale também para a posição dos caminhões por cidade. Cidade fora do estado que já
  estava no cache é localizada de novo sozinha; se a TomTom não achar a cidade no
  estado, ela fica sem posição (e o log avisa) em vez de cair no lugar errado.
- Rota do rotograma (linha roxa): rota de caminhão passando pelos postos autorizados, na
  ordem, um a cada 40 km. O detalhe mostra o tamanho dela e o do rotograma, para conferir.
- Pontos autorizados (verde) e áreas de risco (vermelho com "!") aparecem ao selecionar
  uma rota do trecho. Contorno tracejado = posição só da cidade.
- Alertas (coluna `Alerta Rotograma`, etiqueta "Rotograma" e KPI "Alerta rotograma"):
  - `Fora da rota do rotograma (X km)`: caminhão a mais de 15 km da rota (30 km quando a
    posição da Angellira é só da cidade);
  - `Parado em local de roubo/local proibido: NOME`: parado (velocidade 0, ou ignição
    desligada sem velocidade) a até 2 km da área de risco, ou com o texto da posição
    citando o local;
  - `Parado fora dos pontos autorizados há XhYY`: parado 30 min ou mais
    (`ROTOGRAMA_PARADA_MIN`) longe dos pontos autorizados, da origem e do destino.
    Só com posição no nível de ponto de referência.
  Não geram alerta: viagem não iniciada, posição com mais de 90 min, rota no destino ou
  encerrada, cancelada, e áreas do tipo FRIGORIFICO.
- A posição da Angellira não é GPS: os alertas são um apoio para o GR olhar a rota, não
  uma prova. As margens são ajustáveis: `ROTOGRAMA_DESVIO_KM`,
  `ROTOGRAMA_DESVIO_CIDADE_KM`, `ROTOGRAMA_RAIO_RISCO_KM`, `ROTOGRAMA_RAIO_PONTO_KM`.

## Relatório da Angellira automático

O robô baixa sozinho o relatório do Tracking da Angellira a cada 15 minutos
(`angellira.py`) e grava na aba `Rastreamento_Angellira`. Ele faz no navegador
(Chromium sem janela) o mesmo caminho da pessoa: login, Tracking, atualiza a página,
Exportar colunas, "Todas as colunas", Exportar.

- A gravação põe o relatório novo por cima e depois apaga o que sobrou do anterior
  (linhas abaixo e colunas à direita). O resultado é o mesmo de limpar e colar, mas
  a aba nunca fica vazia no meio de um ciclo.
- Relatório sem as colunas que o robô usa (PROCEDIMENTO DE EMBARQUE, VEÍCULO,
  DATA POSIÇÃO) não é gravado: a aba fica como estava e o motivo vai para o log.
- Falha em qualquer passo vai para o log com o nome do passo, por exemplo
  `Angellira: falhou clicando em exportar colunas (página: ...)`. A próxima tentativa
  é 15 minutos depois. O robô e o site seguem funcionando.
- O navegador roda num processo separado do robô, com prioridade baixa de CPU (o
  robô sempre tem preferência) e prazo de 8 minutos por rodada
  (`ANGELLIRA_LIMITE_MIN`). Passou do prazo, o navegador é encerrado à força, o log
  diz `a rodada passou de 8 min e o navegador foi encerrado` e o robô segue. Antes,
  uma tela travada depois do login prendia CPU até o vigia reiniciar o serviço.
- Janelas que aparecem por cima do Tracking (comunicados depois do login) são fechadas
  pelo X ou por Esc, e o texto delas vai para o log.
- Modo leve: o navegador não carrega o Google Maps nem scripts de métricas e chat, que
  o relatório não usa e que disputam CPU com o robô no plano gratuito. Se o Tracking
  não abrir assim, o robô volta sozinho ao modo completo e avisa no log. Para desligar
  de vez: `ANGELLIRA_MODO_LEVE=0`. Na primeira exportação depois de cada reinício, o
  log lista os domínios que a página carregou e o que o botão Exportar chamou.

### Configuração no Render (uma vez)

1. Environment:
   - `ANGELLIRA_USUARIO` e `ANGELLIRA_SENHA`: o login de monitoramento. Ficam só no
     Render, nunca no código nem na planilha.
   - `PLAYWRIGHT_BROWSERS_PATH` = `/opt/render/project/src/.pw-browsers` (o Chromium
     fica dentro da pasta do projeto, que é a que passa do build para o serviço).
2. Settings, Build Command:
   `pip install -r requirements.txt && python -m playwright install chromium`
3. Salve e faça o deploy. No log devem aparecer
   `Angellira: atualização automática ligada, a cada 15 min.` e, cerca de 1 minuto
   depois, `Angellira: relatório com N linha(s) e 45 coluna(s) gravado ...`.

Opcionais: `ANGELLIRA_INTERVALO_MIN` (15), `ANGELLIRA_TIMEOUT_S` (60),
`ANGELLIRA_ATIVO=0` (desliga e volta ao relatório colado à mão).

### Rodar no PC (teste ou alternativa)

```powershell
pip install -r requirements.txt
python -m playwright install chromium
$env:ANGELLIRA_USUARIO="..."; $env:ANGELLIRA_SENHA="..."
python angellira.py --uma-vez --visivel   # abre o navegador para acompanhar os passos
python angellira.py                       # fica rodando a cada 15 min
```

