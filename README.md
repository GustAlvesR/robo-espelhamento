# Robô de Monitoramento de Espelhamento (Telemetria)

Lê a aba **Telemetria** (alimentada pelo DBLH) via Google Sheets, calcula SLA
de início, mapeia quedas/retornos de sinal e o % de espelhamento de cada
rota, e escreve tudo em uma planilha de resultados — rodando em loop
contínuo a cada N minutos.

## 1. Pré-requisitos

- Python 3.9+
- As duas planilhas (origem "Telemetria" e a de resultados) acessíveis pela
  sua conta Google.

Instale as dependências:

```bash
pip install -r requirements.txt
```

## 2. Criando a Service Account (credenciais do Google Sheets)

O robô roda sem intervenção humana, então ele não usa seu login Google — ele
usa uma **conta de serviço** (Service Account), que é como um "usuário-robô"
criado dentro de um projeto no Google Cloud.

1. Acesse o [Google Cloud Console](https://console.cloud.google.com/) e crie
   um projeto (ou use um existente).
2. No menu **APIs e Serviços > Biblioteca**, ative:
   - **Google Sheets API**
   - **Google Drive API**
3. Vá em **APIs e Serviços > Credenciais > Criar Credenciais > Conta de
   Serviço**. Dê um nome (ex.: `robo-espelhamento`) e conclua a criação.
4. Abra a conta de serviço criada, vá na aba **Chaves > Adicionar Chave >
   Criar nova chave > JSON**. Um arquivo `.json` será baixado.
5. Renomeie esse arquivo para `service_account.json` e coloque-o na mesma
   pasta do script (ou ajuste `CREDENTIALS_FILE` no topo do script).
6. Abra o arquivo JSON e copie o valor do campo `"client_email"`
   (algo como `robo-espelhamento@seu-projeto.iam.gserviceaccount.com`).

## 3. Compartilhando as planilhas com a Service Account

A conta de serviço só enxerga o que for explicitamente compartilhado com
ela:

1. Abra a planilha de **origem** (Telemetria) → botão **Compartilhar** →
   cole o `client_email` da service account → permissão **Leitor** (basta
   ler).
2. Abra a planilha de **resultados** → **Compartilhar** → mesmo
   `client_email` → permissão **Editor** (o robô precisa escrever nela).

> ⚠️ Nunca suba o `service_account.json` para o Git/GitHub — assim como você
> já faz com os outros segredos do painel-lh, mantenha-o fora do
> versionamento (adicione ao `.gitignore`).

## 4. Configurando o script

No topo de `monitor_espelhamento.py`, edite:

```python
SOURCE_SPREADSHEET_ID = "..."     # ID da planilha de origem (Telemetria)
RESULTS_SPREADSHEET_ID = "..."    # ID da planilha de resultados
```

O ID é o trecho da URL entre `/d/` e `/edit`:
`https://docs.google.com/spreadsheets/d/**ESTE_TRECHO**/edit`

Outros parâmetros ajustáveis: `CHECK_INTERVAL_MINUTES`,
`SLA_MINUTES_BEFORE_START`, `META_PERCENTUAL_ESPELHAMENTO`,
`STATE_RETENTION_DAYS`, `DBLH_WORKSHEET_NAME` (aba com os horários
realizados, padrão `DBLH`, na mesma planilha da Telemetria) e
`FINALIZADAS_HORAS` (padrão 24).

## 5. Rodando o robô

```bash
python monitor_espelhamento.py
```

Ele roda **para sempre** em primeiro plano: executa um ciclo assim que
inicia e depois a cada `CHECK_INTERVAL_MINUTES`. Os logs vão para o console
e para `monitor_espelhamento.log`.

Para deixar rodando em segundo plano de forma persistente, algumas opções
(mesma lógica do seu `PainelLH-AutoSync`, só que aqui é um processo Python
contínuo em vez de um script que roda e sai):

- **Windows**: crie uma tarefa no Agendador de Tarefas que execute o script
  no logon, ou use `pythonw monitor_espelhamento.py` para rodar sem console.
- **Linux/servidor**: rode como serviço `systemd`, ou com `tmux`/`screen` +
  `nohup python3 monitor_espelhamento.py &`.

## 6. O que cada saída gera

### Aba de resultados (`Resultado_Espelhamento`)
Uma linha por rota ativa, sobrescrita a cada ciclo, com: horários previstos,
status atual, se o SLA de 1h30 foi cumprido, duração da rota, tempo total
sem sinal, % de espelhamento, quantidade de quedas e um `Status Geral`
(OK/NOK/Pendente) que já combina as duas regras de reprovação usadas hoje
no acompanhamento de GR.

Regra da Shopee (documento "Rastreamento de Veículo de LH - Telemetria"):
a viagem é OK só se cumprir as duas etapas.

1. Espelhada pelo menos 1h30 antes do ETA Origem programado. O robô guarda
   o status lido quando chega esse horário (`SLA 1h30 Cumprido`).
2. Espelhada em pelo menos 95% do tempo da viagem. O tempo vai do que
   acontecer primeiro entre o ETA Origem programado e o CPT realizado até o
   ETA Destino realizado. Os dois realizados vêm da aba `DBLH`
   (`cpt_realizado` e `eta_destino_realizado`; em viagem com várias pernas,
   o CPT da primeira e a chegada da última). Quedas fora desse intervalo não
   contam.

Enquanto a chegada não acontece, o fim da janela é provisório (o fim
previsto, ou agora se a viagem já passou dele) e o % também. A coluna
`Janela Final` diz se o resultado já é o definitivo. Quando a rota sai da
aba Telemetria, ela continua na aba de resultados com `Status Viagem` =
FINALIZADA por `FINALIZADAS_HORAS` depois da chegada, com o % fechado na
chegada realizada, para o BSC copiar o resultado final. Essas linhas não
aparecem no site nem consomem a TomTom.

Ainda não considerado: zonas de sombra aprovadas pela Tecnorisk (perda de
sinal nelas não deveria contar).

### Aba de histórico (`Historico_Quedas_Sinal`) — extra
Como uma rota pode cair e voltar várias vezes, esta aba guarda um
**registro append-only** de cada evento de PERDA e RETORNO (com timestamp e
duração), para auditoria — sem depender só do "último evento" que aparece
na aba de resultados.

### Aba de fechamento (`Fechamento_Rotas`)
Uma linha por rota, gravada uma única vez quando ela termina, com o retrato
final de `Resultado_Espelhamento` (SLA, % espelhado, quedas, Status Geral...).
Na frente vêm `Fechada Em`, `Motivo` e `% Final` (número, para média e filtro).

- `Chegada realizada`: gravada 2 ciclos depois do ETA Destino realizado (aba
  DBLH). A janela da Shopee termina na chegada, então o % já não muda; os 2
  ciclos dão tempo de uma queda pendente de confirmação entrar na conta.
- `Saiu da Telemetria`: a rota sumiu da origem antes disso e ficou
  `FECHAMENTO_SUMIDA_MIN` minutos (padrão 30) sem voltar. Se a chegada
  realizada já estiver na DBLH, o retrato é fechado nela; senão, vale o último.

Rota cancelada (abaixo) fecha sem linha: não pontua.

Cada rota fecha uma vez só. Se ela voltar à Telemetria depois de fechada, não
ganha uma segunda linha.

### Rotas canceladas
A cada ciclo o robô consulta, na planilha de origem, `DBLH` (`status_agrupado`)
e `LH SEMANA ATUAL` (`STATUS REAL`). Se qualquer uma marcar a rota como
CANCELADO, NO SHOW, INFRUTÍFERA ou NÃO CONSUMIDA (DELETADA não conta), a linha
do Resultado sai com `Status Geral` = `Cancelada` e a coluna `Cancelamento`
diz a origem (ex.: `DBLH: CANCELADO`). Ela não gera eventos novos no histórico
de quedas, não entra no `Fechamento_Rotas` e fica fora dos indicadores do site.
O % e as quedas continuam calculados na linha, para conferência: quem soma
indicadores pela aba de Resultado deve filtrar `Status Geral` ≠ `Cancelada`.

A decisão vale no fechamento: se o cancelamento for desfeito antes do fim
previsto, a rota volta a contar normalmente.

## 7. Como funciona a memória do robô (`state_espelhamento.json`)

Esse arquivo é o que permite ao robô saber, a cada novo ciclo, "o que já
sabia antes" sobre cada rota (sem ele, toda leitura pareceria a primeira, e
seria impossível detectar uma *mudança* de status). Para cada `trip_number`
ele guarda: o último status visto, se a checagem de SLA já foi feita (e o
resultado), o início de uma eventual queda em aberto, o histórico de quedas
já fechadas e o tempo total acumulado sem sinal.

A gravação é **atômica** (grava em um arquivo temporário e só troca pelo
definitivo no final) para nunca corromper o estado se o processo cair no
meio de uma escrita. Rotas que somem da planilha de origem por mais de
`STATE_RETENTION_DAYS` dias são removidas automaticamente do estado, para o
arquivo não crescer para sempre.

Se algum dia você precisar "zerar" o robô, basta apagar o
`state_espelhamento.json` — ele será recriado do zero no próximo ciclo (mas
vai perder o controle das quedas já registradas).
