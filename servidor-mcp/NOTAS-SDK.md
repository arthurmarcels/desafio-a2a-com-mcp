# Spike do SDK `mcp` v2 (Python) — servidor para o desafio "A Ponte"

Data: 2026-10-10 · SDK: `mcp` 2.3.0 (+ `mcp-types` 2.3.0) · Python 3.12 (venv)
Instalação: `uv venv .venv && uv pip install mcp` — site-packages inspecionado em
`.venv/lib/python3.12/site-packages/mcp/`.

Verificação empírica além da leitura do código: um script de spike executou
`render_elicitation_schema` sobre modelos dinâmicos e conferiu
`find_resolved_parameters`/`_wants_union`/`_state_key` (saída abaixo).

## (a) MRTR `input_required` com `inputRequests`/`requestState` — CONFIRMADO

O SDK v2.3.0 fala a revisão `2026-07-28` nativamente (não é pós-cutoff em
relação ao pacote instalado — o próprio `mcp_types.KNOWN_PROTOCOL_VERSIONS` a
lista, e `MODERN_PROTOCOL_VERSIONS = ("2026-07-28",)`):

- **Resolver DI** (`mcp/server/mcpserver/resolve.py`): um parâmetro de tool
  anotado `Annotated[ElicitationResult[Any], Resolve(fn)]` faz `fn` rodar antes
  do corpo da tool. O resolver retorna `Elicit(message, model)` e o framework
  transforma em `InputRequiredResult(input_requests=..., request_state=...)`
  quando o protocolo é ≥ 2026-07-28 (`_uses_input_required`). O corpo da tool
  **não roda** na primeira rodada; na rodada do retry, o outcome vem de
  `_restore_outcome` (do `request_state` decodificado) ou de
  `context.input_responses` — validado contra o schema com
  `marker.schema.model_validate(answer.content)`.
  - Anotar **`ElicitationResult[Any]`** (não o modelo nu) faz o consumidor
    receber o outcome completo (accept/decline/cancel) — `_wants_union` → True,
    verificado no spike.
- **`InputRequiredResult`** (`mcp_types/_types.py:2074`): `resultType:
  "input_required"`, `inputRequests` (mapa com chave atribuída pelo servidor) e
  `requestState` (opaco). Ao menos um dos dois é obrigatório.
- **Chave do `inputRequests`** = `module:qualname` do resolver (`_state_key`) —
  estável entre rounds e entre workers. Com um resolver de nível de módulo
  `escolha_de_sala`, a chave é `__main__:escolha_de_sala` — idêntica ao wire
  example 03. Verificado executando.
- **Wire**: `InputRequest` com `method: "elicitation/create"`,
  `params.mode: "form"`, `params.message`, `params.requestedSchema` (via
  `ElicitRequestFormParams`). O render usa
  `render_elicitation_schema` (`mcp/server/elicitation.py`), que valida cada
  propriedade contra `PrimitiveSchemaDefinition`. Verificado no spike:
  - `create_model` com `Literal[tuple(alts)]` → `"enum": [...]` com 2+ alts e
    `"const"` com 1 alt — exatamente o que o validador aceita (ambas as formas).
  - `Field(..., description=...)` propaga a description.

## (b) Resposta em JSON puro (`application/json`) — CONFIRMADO

`MCPServer(...).streamable_http_app(json_response=True)` → o SessionManager
roteia qualquer request cujo header `MCP-Protocol-Version` não é das versões de
handshake (`2024-11-05`…`2025-11-25`) para
`handle_modern_request` (`mcp/server/_streamable_http_modern.py`), o caminho
single-exchange. Com `json_response=True` o POST é respondido com um único
`Response(media_type="application/json", status=200)` contendo a resposta
JSON-RPC serializada (`_write`) — **não SSE** — desde que o `Accept` do cliente
inclua `application/json` (o validador envia `application/json,
text/event-stream`, que satisfaz `check_accept_headers`). Um handler que
completa sem emitir notificações dentro da janela de 15s responde JSON mesmo em
modo SSE; em modo JSON puro é sempre JSON.

## (c) Stateless puro — CONFIRMADO

O caminho moderno é stateless por construção: cada POST roda
`classify_inbound_request` (valida `params._meta` com
`io.modelcontextprotocol/protocolVersion` e
`io.modelcontextprotocol/clientCapabilities` **neste request**), cria uma
`Connection.from_envelope(...)` efêmera e chama `serve_one`. **Não existe
`initialize`** nessa era — um `initialize` recebido leva
`UNSUPPORTED_PROTOCOL_VERSION`. `tools/list` como primeira chamada funciona
sem handshake. `stateless_http=True` só afeta o caminho legado; para a era
moderna é irrelevante (mas inofensivo) — não precisamos dele.

## (d) Status HTTP controlável — CONFIRMADO

`ERROR_CODE_HTTP_STATUS` (`mcp/shared/inbound.py`) mapeia `PARSE_ERROR`,
`INVALID_REQUEST`, **`INVALID_PARAMS` (-32602) → 400**, `HEADER_MISMATCH`
(-32020) → 400, **`MISSING_REQUIRED_CLIENT_CAPABILITY` (-32021) → 400**,
`UNSUPPORTED_PROTOCOL_VERSION` → 400, `METHOD_NOT_FOUND` → 404. O `_write` do
caminho moderno aplica a tabela a qualquer `JSONRPCError`, seja do classificador
(ladder) ou de handler (`MCPError` propagado por
`handler_exception_to_error_data`). Basta então:

- checks 4/5 (envelope incompleto): a ladder do `classify_inbound_request`
  devolve `-32602` nomeando a chave faltante → `_write_rejection` → HTTP 400.
- check 15 (sem capability form): o `_require_capability` do resolver
  (`resolve.py`) levanta `MCPError(-32021)` com `data.requiredCapabilities`
  serializado de `MissingRequiredClientCapabilityErrorData` → 400. A mensagem
  do wire example ("Client did not declare the form elicitation capability
  required by resolver '__main__:escolha_de_sala'") casa com o formato
  `f"Client did not declare the {name} capability required by resolver {key!r}"`
  com `name="form elicitation"`.

## (e) requestState protegido (HMAC/AEAD + expiração) — CONFIRMADO

`RequestStateSecurity` + `RequestStateBoundary`
(`mcp/server/request_state.py`), instalados por padrão no `MCPServer`:

- Codec embutido: **AES-256-GCM** com chave derivada por HKDF-SHA256 de um
  segredo ≥ 32 bytes (`AESGCMRequestStateCodec`); token com prefixo `v1.` +
  base64url(kid|nonce|ct+tag). Trocar um caractere → `InvalidTag` →
  `InvalidRequestState("seal")` → `MCPError(-32602)` com mensagem congelada
  "Invalid or expired requestState" (check 17).
- `ttl=600.0` (10 min) por padrão — dentro da janela exigida de 5–30 min. O
  briefing da child pede 15 min: `RequestStateSecurity(keys=[...], ttl=900)`.
- Envelope de claims: `v/iat/exp/m/t/a/s/aud/p` — `m` (método), `t` (tool
  name), `a` (**digest dos argumentos**) e `aud` (audiência = nome do
  servidor). O binding do digest resolve o check 18 por rejeição: um retry com
  argumentos divergentes dos selados falha em `_request_identity` com
  "request binding" → `-32602`. Aceito pelo validador ("ou o servidor rejeita o
  estado, ou usa os valores selados").
- Chave via env `REQUEST_STATE_SECRET` (exigência ≥ 32 bytes validada pelo
  codec), `bind_principal=None` (sem OAuth no desafio) e o nome do servidor
  como audiência — passa no `_MISSING_AUDIENCE` gate do `MCPServer`.
- **Restart-safe**: o estado viaja no token (o plaintext `s` contém o progresso
  dos resolvers); sem estado em memória entre `input_required` e retry. Um
  retry pós-restart funciona se a chave vem do env — coberto.

## Pontos restantes verificados

- **Resource `politica://uso`** — `@mcp.resource("politica://uso",
  mime_type="text/markdown")` devolve `ReadResourceResult.contents[0]` com
  `text` + `mimeType`. URI inexistente: `ResourceManager.get_resource` levanta
  `ResourceNotFoundError` → `MCPError(INVALID_PARAMS)` (check 8), já que
  `_handle_read_resource` mapeia `ResourceNotFoundError` → `INVALID_PARAMS`.
- **stderr com método/id/traceparent** — o briefing exige registro por request.
  O SDK não loga isso por padrão (só logger interno). Implementarei um
  `ServerMiddleware` (`mcp/server/context.py`: `async (ctx, call_next)`,
  `ctx.method`, `ctx.request_id`, `ctx.params["_meta"]["traceparent"]`) que
  escreve uma linha em stderr por request. Middleware roda **antes** da
  validação de params, então também cobre requests rejeitados pela ladder
  (incluindo os de checks 4/5/15 — ids presentes; o check do validador só pede
  o par (método, id, traceparent) quando o request é válido).
- **`-32020` para headers** — `classify_inbound_request` roda a rung de
  header: `MCP-Protocol-Version` ≠ envelope, `Mcp-Method` ≠ corpo, ou
  `Mcp-Name` (tools/call/resources/read) ≠ param nomeado → `HEADER_MISMATCH`
  com HTTP 400. É a rung 2, antes da rung de versão suportada.
- **Erros de execução (`isError`) com mensagens byte a byte** — `ToolError`
  dentro da tool vira `CallToolResult(is_error=True)` com o texto prefixado
  "Error executing tool reservar_sala: <msg>" (o README aceita o prefixo; o
  validador usa `in`). O corpo prefixado vem de `Tool.run`/`ToolManager` —
  não é possível remover o prefixo pelo caminho padrão, e não precisamos.
- **structuredContent + texto** — `convert_result` valida contra o outputSchema
  e serializa o mesmo objeto em `content[0].text` via `_convert_to_content`
  (`pydantic_core.to_json(..., indent=2)`) — bate com o wire example 02 (JSON
  com indentação 2). O outputSchema do wire example (campo `motivo` etc.) é o
  que `create_model`/BaseModel gera naturalmente com defaults `None`.
- **`listar_salas` outputSchema** — o wire example mostra `$defs`/`$ref`; o
  schema gerado de um modelo aninhado reproduz isso. O validador (check 3) só
  exige `structuredContent == json.loads(text)`, não o formato do outputSchema.
- **Tool inexistente (check 6)** — `ToolManager.call_tool` levanta
  `ToolError("Unknown tool: voar_delorean")` → `_handle_call_tool` devolve
  `CallToolResult(is_error=True)` (aceito: "por -32602 ou por isError").
- **`consultar_disponibilidade` e `reservar_sala` compartilham validação** —
  ferramenta interna comum de política (sala existe, janela 08:00–20:00 -03:00,
  duração ≤ 2h, fim > início), lançando `ToolError` com as mensagens exatas.
- **Alternativas** — livres no intervalo, capacidade ≥ à pedida, máx 3,
  capacidade crescente, empate por id alfabético. `requestedSchema` com enum
  na ordem calculada. Sem alternativa → `isError` com "Sem alternativas
  disponiveis no intervalo" (não há elicitation nesse caso — o resolver nem
  pergunta: a ferramenta decide antes).
- **`action: decline|cancel`** — o outcome `DeclinedElicitation` chega ao
  corpo da tool quando o parâmetro anota `ElicitationResult[Any]`; a tool
  devolve `reservado=false, motivo="recusado"` sem `isError` (check 19).
- **Retry com escolha fora do enum** — `marker.schema.model_validate` falha →
  `ToolError` → `isError`. No fluxo A2A o agente re-exibe as alternativas
  (fora do escopo desta child; aqui só o comportamento do servidor).

## Decisões de implementação derivadas do spike

1. **Classe única `MCPServer`** com `streamable_http_app(json_response=True)`,
   sem `stateless_http` (irrelevante para a era moderna), montada com uvicorn
   na porta parametrizável (default 7301), caminho `/mcp`.
2. **Uma tool `reservar_sala` com resolver** `escolha_de_sala` anotado
   `Annotated[ElicitationResult[Any], Resolve(escolha_de_sala)]`. O resolver
   calcula alternativas, devolve `Elicit(MSG, create_model(...))` ou valor
   direto quando não há conflito... — **não**: o resolver só pergunta quando há
   conflito e há alternativas; sem alternativas a tool levanta o erro antes.
   O corpo usa os valores selados quando o outcome é accept.
3. **Sem estado em memória entre requests** além das reservas em si (lista em
   memória semeada de `dados/reservas.json`, exigência do desafio).
4. **Middleware de log em stderr** para método/id/traceparent por request.
5. **`REQUEST_STATE_SECRET`** lido do env no startup; sem valor → erro
   claro de boot (nunca fallback inseguro). TTL 900s. Documentar no README
   final (T1 só registra no spike; o README geral é da child de docs).
6. **Pydantic `create_model` dinâmico** para o `requestedSchema` do enum —
   verificado que gera `enum`/`const` conforme o número de alternativas.

## Riscos 1–11 do plano v1 — verificados contra o site-packages

| # | Risco | Veredito |
|---|-------|-----------|
| 1 | SDK v2 sem MRTR de primeira classe | **Falso** — `Resolve`/`Elicit`/`InputRequiredResult` nativos |
| 2 | `_meta` por request não validado | **Falso** — `classify_inbound_request` na entrada do caminho moderno |
| 3 | Status HTTP não controlável | **Falso** — `ERROR_CODE_HTTP_STATUS` mapeia `-32602`/`-32021` → 400 |
| 4 | requestState sem utilitário de integridade | **Falso** — `RequestStateSecurity` AES-GCM + claims + TTL |
| 5 | Tools com resolver vazando o param no inputSchema | **Falso** — `skip_names` remove resolved params do schema |
| 6 | Resposta sempre SSE | **Falso** — `json_response=True` responde JSON puro |
| 7 | `inputRequests` com chave não determinística | **Falso** — `module:qualname` estável |
| 8 | Enum de 1 elemento rejeitado pelo render | **Falso** — `const` gerado e aceito pelo validador |
| 9 | Retry precisa de estado em memória | **Falso** — estado viaja no `requestState` selado |
| 10 | Prefixo de ToolError inaceitável | **Falso** — validador aceita prefixo ("o que não pode é a mensagem ser diferente") |
| 11 | Resource inexistente devolve contents vazio | **Falso** — `ResourceNotFoundError` → `-32602` |
