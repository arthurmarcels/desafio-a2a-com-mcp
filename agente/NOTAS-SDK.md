# NOTAS-SDK — descobertas do SDK `mcp` v2 (2.3.0) no lado cliente (T2)

Registro das descobertas relevantes para o **agente como host MCP**, com as
divergências SDK vs. spec e as decisões de camada. Referência de versão:
`mcp==2.3.0` / `mcp-types==2.3.0` (travadas no `pyproject.toml` + `uv.lock`
da raiz). O validador exige a revisão de protocolo `2026-07-28`.

## Decisões de camada (as duas que o contrato pede para registrar)

### 1. Cliente MCP **sem callback de elicitation** — MRTR manual

O `Client` de alto nível (`mcp.client.client.Client.call_tool`) embute o
driver do MRTR (`mcp/client/_input_required.py`): ao receber
`InputRequiredResult`, ele despacha cada `input_requests[*]` para os
callbacks de sampling/elicitation/roots e **retrya sozinho** com as respostas
coletadas. Para o agente A2A isso seria errado em dobro:

* quem responde à elicitation é o **cliente A2A** (usuário final), não o
  host — a resposta só chega num `SendMessage` futuro, potencialmente
  minutos depois;
* com callback registrado, o ciclo fecharia dentro do host e a Task nunca
  pausaria (o README do desafio chama isso de "metade do desafio evapora").

**Decisão:** usar `ClientSession.call_tool(..., allow_input_required=True)`
via `client.session`, que devolve o `InputRequiredResult` cru. O host guarda
`request_state` + chave do `inputRequests` ligados à Task A2A e **não
retrya** — o retry nasce de um novo `SendMessage` do cliente A2A, como
request JSON-RPC independente. Nem o `Client` alto nível é usado para
`call_tool`, nem callback de elicitation é registrado — o fluxo do
`_drive_input_required` nunca roda.

### 2. Capability declarada **sem** callback — wrap do stamp do `ClientSession`

Divergência SDK vs. contrato: `ClientSession._build_capabilities()`
(session.py:614) só inclui `elicitation` no anúncio quando há callback
registrado (`self._elicitation_callback is not _default_elicitation_callback`)
— e, quando há, anuncia `{"form": {}, "url": {}}`. O contrato do desafio
exige que **todo** request declare `io.modelcontextprotocol/clientCapabilities
= {"elicitation": {"form": {}}}` **sem** callback (a capability é sobre o que
o cliente consegue renderizar; o MRTR stateless por request faz a resposta
voltar num request novo, não por canal de volta).

**Decisão:** wrap de `ClientSession._stamp` (o mesmo callable que o SDK usa
para carimbar `_meta` + headers em cada request — ver abaixo). O wrap
preserva todo o comportamento do stamp original e apenas regrava a chave
`io.modelcontextprotocol/clientCapabilities` com `{"elicitation":
{"form": {}}}`. É ponto único, roda por request e não reescreve protocolo:
os campos `protocolVersion`, `clientInfo` e os headers espelhados continuam
saindo do código do SDK. `_stamp` é privado do SDK — se uma versão futura
mudar o mecanismo, este é o lugar para revisar (por isso esta nota existe).

## Como cada peça foi feita (e o que o SDK dá de graça)

### Conexão: pin de revisão, sem handshake, sem sessão

`Client(url, mode="2026-07-28")` — `mode` aceita uma revisão moderna
diretamente: `__aenter__` faz `session.adopt(_synthesize_discover(mode))`
**sem trafegar nada na rede** (client.py:452-460). Resultado:

* nenhum `initialize`/`server/discover` na subida (o servidor MCP do desafio
  é stateless e rejeitaria handshake);
* nenhuma sessão HTTP (`Mcp-Session-Id`): o servidor nunca devolve o header,
  o transporte nunca reenvia — cada POST é independente;
* o GET de SSE só abre mediante notificação `initialized`, que nunca é
  enviada — o transporte fica POST-only por construção.

Cuidado: o read timeout padrão do transporte é **300s**
(`MCP_DEFAULT_SSE_READ_TIMEOUT`, pensado para stream de resposta aberto).
Com servidor stateless de tool rápida, passamos `read_timeout_seconds=30`
no `Client` para um servidor morto não pendurar 5 minutos.

### `_meta` obrigatório por request

O stamp moderno (`_make_modern_stamp`, session.py:153) escreve em **todo**
request: `io.modelcontextprotocol/protocolVersion`, `clientInfo`,
`clientCapabilities` dentro de `params._meta`, e os headers
`MCP-Protocol-Version` + `Mcp-Method`. Como pinamos `mode="2026-07-28"`, o
stamp moderno já está ativo no `__aenter__` — nada de `_meta` montado à mão.

### Headers espelhados (`Mcp-Method`, `Mcp-Name`)

O mesmo stamp deriva `Mcp-Name` de `NAME_BEARING_METHODS`
(shared/inbound.py:72): `tools/call` → `params.name`,
`resources/read` → `params.uri`. Ou seja, com o SDK v2 os headers espelhados
saem automáticos em `tools/call` e `resources/read`; não há como injetar
headers por request na API pública (o caminho documentado seria
`ClientMessageMetadata.headers`, mas `send_request` só o consulta para
resumption). Não precisamos injetar nada — o contrato bate com o que o SDK
emite.

### `traceparent` dentro do `_meta`

`RequestParamsMeta` é um `TypedDict` aberto (`extra_items=Any`): passar
`meta=RequestParamsMeta(traceparent=...)` para `call_tool`/`read_resource`
faz a chave viajar em `params._meta.traceparent` sem que o SDK a valide ou
reescreva (a chave oficial de trace context reservado do SDK é para OTel
ambient; aqui o valor vem do header do cliente A2A). O mesmo `traceparent`
(mesmo trace-id) é reenviado em todos os requests MCP da Task; span-id pode
ser novo — como não geramos span, ecoamos o header tal como chegou.

### Retry do MRTR: `inputResponses` + `requestState` + id novo

`ClientSession.call_tool(name, arguments, input_responses=..., request_state=...)`
serializa como `inputResponses`/`requestState` (aliases by_alias) nos params
do `tools/call`. Pontos verificados:

* **id novo**: o dispatcher minta id incremental por request
  (`send_raw_request`); nenhum id é reaproveitado entre o request inicial e
  o retry — verificado no log do servidor (ids 3→4 na mesma Task). O
  `CallOptions.request_id` existiria para fixar ids, mas não usamos;
* **`requestState` opaco**: guardado string pura na Task, ecoado byte a
  byte, nunca aberto (o SDK também promete pass-through byte-exact no
  driver — aqui nem o driver roda);
* **mesma chave**: `inputResponses` usa exatamente a chave recebida no
  `inputRequests` (extraída com `parse_input_required`, aceitando `enum` ou
  `const` no `requestedSchema` — a spec aceita ambos e o validador também).

### `tools/list` antes do primeiro `tools/call`

Feito uma vez por processo no startup (lifespan do ASGI) e guardado; o
caminho de reserva chama `descobrir_tools()` antes do call por garantia (o
SDK também precisa do listing para cache de output schema — ele re-lista
lazy se não conhece a tool). Nada de lista fixa no código: os nomes das
tools vêm do servidor.

### `resources/read` de `politica://uso`

`session.read_resource("politica://uso", meta=..., allow_input_required=True)`
— também aceita `InputRequiredResult` (que ignoramos para este resource; a
política não pausa). Versão extraída com regex `versao:\s*(\S+)` da primeira
linha, cacheada por processo. O valor alimenta o campo `politica` do
artifact quando o `structuredContent` da tool não o traz (a T1 traz).

### Resultado de tool com `isError`

`CallToolResult.is_error` + `content[*].text` — a mensagem exata da tool
vai para o histórico da Task como mensagem de status em
`TASK_STATE_FAILED` (check 35). Erro de **protocolo** (JSON-RPC `error`,
ex.: `-32021`) sobe como `MCPError` do SDK — capturado no `_executar` e
também derruba a Task em `FAILED` com a mensagem do erro.

## Divergências SDK vs. spec observadas (registro)

1. **Capabilities vs. callbacks** (detalhada acima): o SDK amarra o anúncio
   de capability à existência de callback, mas a capability descreve o
   cliente, não o callback em si. Contornado com o wrap do stamp.
2. **`resultType`/`cacheScope`/`ttlMs` obrigatórios nos results**: os tipos
   do `mcp-types` 2.3.0 exigem `cacheScope` e `ttlMs` em `ListToolsResult`
   (e irmãos) na validação do lado cliente — um servidor que omita esses
   campos (permitidos na redação da spec que lemos) faz o cliente Pydantic
   rejeitar a resposta. Os wires do desafio (01–06) os incluem, então a T1
   precisa emiti-los — vale alinhar (o mock da suíte de testes já emite).
3. **`elicitation` com `url`**: quando há callback, o SDK anuncia `url`
   junto de `form`; o contrato do desafio quer **só** `form`. O wrap do
   stamp resolve (regrava a chave inteira).
4. **GET de SSE pós-`initialized`**: irrelevante aqui (nunca enviamos
   `initialized`), mas o comportamento existe — com `mode` pinado moderno
   ele não dispara.

## Suíte de apoio (fase 1, sem a T1)

`agente/tests/mock_mcp.py` — servidor MCP mínimo em Starlette que replica o
contrato dos wires 01–06 (tools, resource, erros exatos, MRTR com
requestState HMAC + argumentos selados). **Não é produção** — é a suíte que
permitiu validar os checks 21–36 (e 01–20 por tabela) antes da T1 existir.
`agente/tests/test_ponte.py` — pytest que sobe mock + agente e percorre o
roteiro dos checks 21–36 (11 testes, todos verdes).

Com a T1 real no ar: `python3 validador/validar.py --agente
http://localhost:7300 --mcp http://localhost:7301` → o alvo é 36/36 com os
dois processos oficiais.
