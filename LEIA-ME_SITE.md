# Site Torre LH (mapa + TomTom)

O site roda dentro do próprio robô (mesmo Web Service no Render, mesmo comando
`python monitor_espelhamento.py`). Sem `TOMTOM_API_KEY` o robô continua
funcionando como antes, só sem mapa e sem previsão.

## Arquivos novos no repositório

- `geo_tomtom.py`: estações, posição do veículo, rotas e previsão.
- `site_web.py`, `templates/`, `static/`: o site.
- `criar_usuario.py`: gera a linha de cada usuário.
- `testar_tomtom.py`: confere se a chave funciona em cada API.
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

Na planilha Acompanhamento Telemetria, crie a aba `Usuarios_Site` com o cabeçalho:

`Email | Nome | Senha_Hash | Ativo`

Para cada pessoa, rode no PC e cole a linha gerada:

```powershell
python criar_usuario.py fulano@ceva.com "Fulano de Tal"
```

Para bloquear alguém, troque `Ativo` para `Não` (vale em até 5 minutos).
Cinco senhas erradas seguidas bloqueiam o IP por 15 minutos.

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
- `Risco de Atraso`: `Atraso provável` (desvio acima da tolerância), `No prazo`,
  `No destino`, `Encerrada`, `Posição antiga`, `Sem posição`,
  `Sem coord. da estação`.
- `Status Viagem`: cópia da coluna `status_corrigido` da Telemetria (PRE VIAGEM,
  EM TRANSITO). Se a coluna sumir, sai vazia e o site usa o horário de início previsto.

## Uso do mapa

- Clicar num caminhão, numa rota ou num item da lista isola aquela rota. Para voltar:
  botão "Ver todos", clique numa área vazia do mapa ou tecla Esc.
- Rota isolada: tracejado = viagem não iniciada; cinza = trecho já percorrido; azul =
  falta percorrer. O "percorrido" é o trajeto previsto cortado no ponto mais próximo do
  caminhão (não há histórico de posições); com o caminhão a mais de 30 km do trajeto,
  o corte não é feito.
- A lista vem por gravidade: sem sinal agora, NOK, atraso provável e o resto.
- Botão "Estações" no canto do mapa: mostra os HUBs (last mile, códigos `HUB-`),
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
