# A Ponte: um agente A2A com MCP por dentro

Entrega do desafio [devfullcycle/desafio-a2a-com-mcp](https://github.com/devfullcycle/desafio-a2a-com-mcp):
um servidor MCP (salas de reunião da Hill Valley Tech) e um agente que é host
MCP por dentro e servidor A2A por fora, costurados pela ponte MRTR→Task.

- **Stack:** Python 3.10+, SDK oficial `mcp` v2.3.0 (revisão `2026-07-28`),
  `uvicorn`, binding A2A JSON-RPC 2.0 sobre HTTP (implementação própria, sem SDK de A2A).
- **Sem LLM no caminho** — o agente traduz protocolo por regra, não por modelo.
- Versões travadas em `pyproject.toml` + `uv.lock`.

## 1. Como rodar (a partir de um clone limpo)

Requisitos: Python 3.10+ e [uv](https://docs.astral.sh/uv/) (ou instale
`mcp==2.3.0` e `uvicorn==0.54.0` manualmente no seu venv).

```bash
# 0. clone e dependências
git clone <url-do-seu-fork>.git && cd desafio-a2a-com-mcp
uv sync                                    # cria .venv com mcp==2.3.0 + uvicorn==0.54.0

# 1. gere a chave de integridade do requestState (>= 32 bytes; NUNCA comite o valor)
export REQUEST_STATE_SECRET="$(python3 -c "import secrets; print(secrets.token_hex(32))")"

# 2. terminal 1 — servidor MCP (porta 7301, Streamable HTTP em /mcp)
uv run python servidor-mcp/servidor.py

# 3. terminal 2 — agente A2A (porta 7300, /a2a e /.well-known/agent-card.json)
uv run python agente/agente.py

# 4. validador (com os dois processos recém-iniciados)
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Variáveis de ambiente (todas com default; o validador usa os defaults):

| Variável | Default | Onde |
| --- | --- | --- |
| `REQUEST_STATE_SECRET` | — (obrigatória) | servidor MCP |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `7301` | servidor MCP |
| `AGENTE_PORT` / `MCP_URL` | `7300` / `http://localhost:7301/mcp` | agente |

O servidor MCP sai com exit 2 se `REQUEST_STATE_SECRET` estiver ausente ou
tiver menos de 32 bytes. O segredo não é persistido em nenhum lugar: existe
apenas no ambiente do processo do servidor; o agente nunca o vê (para ele o
`requestState` é opaco). Para o teste de restart do passo 12 abaixo, reexporte
a mesma variável ao reiniciar o processo.

### Fluxo manual (13 passos)

Os corpos de request abaixo seguem `exemplos/wire/` (intocado). `bash` + `curl`
bastam; o stderr do servidor MCP é o instrumento de conferência.

1.  Agent Card: `curl http://localhost:7300/.well-known/agent-card.json` — confira
    contra `exemplos/wire/07-a2a-agent-card.json` a interface JSON-RPC (`protocolVersion`
    `1.0`) e a skill `reservar-sala`.
2.  Descoberta MCP direto no servidor:
    `POST /mcp` com `Mcp-Method: tools/list` (wire 01) — as três tools voltam.
3.  `tools/call` livre (`listar_salas`, wire 02): `structuredContent` + o mesmo
    JSON em bloco de texto.
4.  Resource: `resources/read` de `politica://uso` (wire 05) devolve a política
    (`versao: 2026-11-01`).
5.  **Ordem de descoberta do agente:** suba o agente com o servidor MCP já no ar
    e olhe o stderr do servidor — o `tools/list` do agente aparece **antes** do
    primeiro `tools/call` dele (no startup, junto com `resources/read`; a
    descoberta também é exigida dentro de `chamar_reserva` antes de qualquer
    chamada).
6.  **Propagação de trace-id:** mande um `SendMessage` A2A com header
    `traceparent: 00-<trace-id>-<span-id>-01` e procure o mesmo `<trace-id>` no
    `traceparent` do `_meta` dos `tools/call` registrados no stderr do servidor
    MCP (o validador imprime o trace-id dele na primeira linha — é o valor a procurar).
7.  `SendMessage` pedindo `sala-garagem` 14h–15h de 03/11/2026 (wire 08):
    Task em `TASK_STATE_INPUT_REQUIRED` com a linha `alternativas: <ids>` na
    ordem do `enum` (com o starter limpo: `alternativas: sala-fusca, sala-mirante`).
8.  `GetTask` da Task pausada (wire 09): estado corrente + histórico.
9.  Continuação `escolha=<id>` (wire 10): Task `COMPLETED` com artifact `reserva`
    apontando para a sala escolhida e o campo `politica`.
10. Repita 7 e responda `escolha=recusar`: Task `TASK_STATE_CANCELED`.
11. **`requestState` adulterado:** repita o `tools/call` de conflito direto no
    servidor MCP (wire 03), troque um caractere do `requestState` e reenvie com
    `inputResponses` (wire 04): recusa com `-32602` (HTTP 400).
12. **Retry válido após restart:** capture um `requestState` de um conflito,
    mate o servidor MCP, reexporte a mesma `REQUEST_STATE_SECRET`, suba-o de
    novo e reenvie o retry: a reserva conclui — o estado selado viaja no
    `requestState`, não na memória do servidor.
13. **Conflito sem capability:** o mesmo `tools/call` de conflito com
    `"clientCapabilities": {}` no `_meta` (wire 06): `-32021` com
    `data.requiredCapabilities` e HTTP 400.

## 2. Onde a ponte acontece

Dois pontos de código no agente (`agente/agente.py`), mais o request selado no
servidor:

- **`input_required` → `TASK_STATE_INPUT_REQUIRED`:** `Agente._executar`
  (`agente/agente.py:438-449`). Quando `HostMcp.chamar_reserva` devolve um
  `InputRequiredResult` (o host pede o resultado cru com
  `allow_input_required=True`, sem callback de elicitation), o agente extrai a
  chave do `inputRequests` e o `enum` de alternativas
  (`parse_input_required`, `agente/agente.py:272`), pausa a Task
  (`task.estado = PAUSADA`), guarda `request_state`, `chave_input` e
  `alternativas` **ligados à Task** (`agente/agente.py:441-444`) e responde ao
  cliente A2A com a linha `alternativas: <ids>` — sem prefixo, byte a byte
  igual entre pausas iguais.
- **`requestState` de volta ao servidor:** `Agente._continuar`
  (`agente/agente.py:365-408`). O `SendMessage` de continuação (`escolha=<id>`
  ou `escolha=recusar`) chama `_executar` de novo, que reenvia o `tools/call`
  com `input_responses={chave: resposta}` e `request_state=task.request_state`
  ecoado sem modificação (`agente/agente.py:402-408`). O dispatcher do SDK
  minta um **id JSON-RPC novo** a cada request, então o retry sai com id ≠
  inicial por construção — conferível no stderr do servidor MCP (par
  `tools/call id=3` → `tools/call id=4` na mesma Task).
- **No servidor, o outro lado da costura:** `escolha_de_sala`
  (`servidor-mcp/servidor.py:194-213`) é o resolver `Resolve` que decide se há
  o que perguntar; `reservar_sala` (`servidor-mcp/servidor.py:237-260`)
  recebe a resposta restaurada do `requestState` selado e conclui. O servidor
  não guarda nada entre o `input_required` e o retry — o estado completo do
  pedido vive cifrado dentro do `requestState`.

O `requestState` nunca vaza: fica em `Task.request_state`, não vai para card,
artifact, histórico ou resposta A2A (check 34 do validador).

## 3. Decisões técnicas

- **`requestState` protegido com AEAD (AES-256-GCM)**, não só HMAC: o
  utilitário `RequestStateSecurity` do SDK (`mcp==2.3.0`) selar+cifra o estado
  completo do pedido (args originais, resolver, versão da política, expiração).
  Cifrar é opcional pela spec; escolhi cifrar porque o conteúdo inclui o
  responsável pelo pedido — legível é aceitável, adulterável não é. A chave vem
  de `REQUEST_STATE_SECRET` (≥ 32 bytes, validado no startup, exit 2 sem ela);
  segredo nenhum no código.
- **Expiração de 15 minutos** (TTL 900 s, dentro da janela 5–30 min da spec):
  `RequestStateSecurity(keys=[segredo], ttl=900.0)` em `servidor-mcp/servidor.py:358`.
  Adulterado ou expirado → `-32602` com `data.reason: "invalid_request_state"`.
- **Retry após restart funciona** porque nada do MRTR vive em memória no
  servidor: o pedido original selado dentro do `requestState` é reconstruído
  pelo framework no retry (`_restore_outcome`), e argumentos divergentes no
  retry não tomam efeito (o selado vence — check 18 do validador).
- **Estado das Tasks em memória no agente** (`Agente.tasks: dict[str, Task]`),
  com histórico, artifacts e o estado da ponte (`request_state`, `chave_input`,
  `alternativas`) por Task. Sem persistência em disco (fora de escopo por
  design); Tasks não sobrevivem ao restart do agente, o que a spec permite.
  Pausas concorrentes ficam isoladas por Task (check 33).
- **Host MCP sem callback de elicitation:** `ClientSession.call_tool(...,
  allow_input_required=True)` devolve o `InputRequiredResult` cru; quem
  responde é o cliente A2A no `SendMessage` seguinte, não um canal de volta.
  O `clientCapabilities` do `_meta` é regravado para
  `{"elicitation": {"form": {}}}` no stamp de cada request (wrap de
  `session._stamp`, `agente/agente.py:155-173`) porque o SDK só declara a
  capability quando há callback registrado.
- **A2A sem SDK:** JSON-RPC 2.0 + máquina de estados da Task implementados em
  ASGI puro (Starlette/uvicorn). Sem streaming, sem OAuth, sem persistência —
  todos fora de escopo.
- **Determinismo:** sem LLM; o mesmo pedido produz sempre a mesma pausa (check
  36). Reservas em memória no servidor, reseed do starter a cada restart.

Divergências SDK × spec e spikes ficaram registrados em
`servidor-mcp/NOTAS-SDK.md` e `agente/NOTAS-SDK.md`.

## 4. Saída do validador

Executado em 2026-10-13 a partir de um clone limpo da branch, com os dois
processos recém-iniciados (`uv sync` + `REQUEST_STATE_SECRET` gerada na hora):

```text
$ python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
trace-id desta execucao: 18758aefe7cfb5ef385188fbce8b9980
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```

Exit code: `0`.

Evidências de amostragem manual (stderr do servidor MCP e respostas HTTP) dos
passos críticos, coletadas na mesma sessão do clone limpo:

<details>
<summary>Passo 5 — <code>tools/list</code> antes do primeiro <code>tools/call</code></summary>

```text
request method=tools/list id=1 traceparent=-          <- agente, no startup
request method=resources/read id=2 traceparent=-      <- agente, no startup
...                                                   <- validador: checks 01-20 (HTTP direto)
request method=tools/call id=3 traceparent=00-18758aefe7cfb5ef385188fbce8b9980-79bbe386d5eae12e-01   <- primeiro tools/call do agente
```

A descoberta é exigida em código antes de qualquer chamada:
`HostMcp.chamar_reserva` chama `await self.descobrir_tools()` primeiro
(`agente/agente.py:261`).
</details>

<details>
<summary>Passo 6 — trace-id propagado do A2A até o servidor MCP</summary>

```text
$ python3 validador/validar.py ...
trace-id desta execucao: 18758aefe7cfb5ef385188fbce8b9980

# stderr do servidor MCP:
request method=tools/call id=3 traceparent=00-18758aefe7cfb5ef385188fbce8b9980-79bbe386d5eae12e-01
request method=tools/call id=4 traceparent=00-18758aefe7cfb5ef385188fbce8b9980-79bbe386d5eae12e-01
request method=tools/call id=5 traceparent=00-18758aefe7cfb5ef385188fbce8b9980-79bbe386d5eae12e-01
```

O trace-id do header `traceparent` do `SendMessage` A2A chega idêntico no
`_meta.traceparent` de todos os `tools/call` da Task (span-id novo, trace-id
preservado).
</details>

<details>
<summary>Passo 11 — <code>requestState</code> adulterado → <code>-32602</code></summary>

```json
{"jsonrpc":"2.0","id":"tamper-1","error":{"code":-32602,"message":"Invalid or expired requestState","data":{"reason":"invalid_request_state"}}}
```

HTTP 400. Um único caractere trocado no fim do `requestState` (último `A` →
`B`) derruba a verificação AEAD — o tag não bate e o estado é rejeitado.
</details>

<details>
<summary>Passo 12 — retry válido após restart do servidor MCP</summary>

```text
requestState capturado (377 chars): v1.vVuu48iTvhsW7UGEZYA...
servidor MCP reiniciado (mesma REQUEST_STATE_SECRET reexportada antes de subir)

{"jsonrpc":"2.0","id":"restart-2","result":{"content":[{"text":"{\n  \"reserva\": \"res-0003\",\n  \"reservado\": true,\n  \"sala\": \"sala-fusca\",\n  \"inicio\": \"2026-11-03T14:00:00-03:00\",\n  \"fim\": \"2026-11-03T15:00:00-03:00\",\n  \"responsavel\": \"Marty\",\n  \"politica\": \"2026-11-01\",\n  \"motivo\": null}\n","type":"text"}],
 "isError":false,"resultType":"complete","structuredContent":{"reserva":"res-0003","reservado":true,"sala":"sala-fusca",
 "inicio":"2026-11-03T14:00:00-03:00","fim":"2026-11-03T15:00:00-03:00","responsavel":"Marty","politica":"2026-11-01","motivo":null}, ...}}
```

HTTP 200. O processo foi morto e recriado entre o `input_required` e o retry;
a reserva concluiu com os argumentos originais selados (não os do retry).
</details>

<details>
<summary>Passo 13 — conflito sem capability de elicitation → <code>-32021</code></summary>

```json
{"jsonrpc":"2.0","id":"sem-cap","error":{"code":-32021,"message":"Client did not declare the form elicitation capability required by resolver '__main__:escolha_de_sala'","data":{"requiredCapabilities":{"elicitation":{"form":{}}}}}}
```

HTTP 400, com `data.requiredCapabilities` listando o que faltou.
</details>

<details>
<summary>Retry com id novo (a ponte vista do stderr do servidor MCP)</summary>

```text
# SendMessage A2A (sala-garagem 14h-15h, conflito) → Task INPUT_REQUIRED
request method=tools/call id=3 traceparent=00-18758aefe7cfb5ef385188fbce8b9980-79bbe386d5eae12e-01
# escolha fora do enum repetiu a pausa (id=4) e a continuação concluiu (id=5)
request method=tools/call id=5 traceparent=00-18758aefe7cfb5ef385188fbce8b9980-79bbe386d5eae12e-01
```

Dois `tools/call` distintos para a mesma Task, ids diferentes — o retry nasce
de um request JSON-RPC independente, como a spec manda.
</details>
