#!/usr/bin/env python3
"""Agente A2A "Central de Salas" — host MCP por dentro, servidor A2A por fora.

Duas camadas num único processo:

* A2A (ASGI puro, JSON-RPC 2.0 sobre HTTP em ``/a2a``): recebe ``SendMessage``
  e ``GetTask``, mantém a máquina de estados da Task com histórico.
* Host MCP (SDK ``mcp`` v2, Streamable HTTP): descobre as tools em runtime,
  lê o resource ``politica://uso`` e opera o ciclo MRTR **manualmente** —
  ``allow_input_required=True`` devolve o ``InputRequiredResult`` cru, sem
  callback de elicitation, porque quem responde é o cliente A2A no retry,
  não um canal de volta.

A ponte: ``input_required`` vira ``TASK_STATE_INPUT_REQUIRED`` com a linha
``alternativas: <ids>``, o ``requestState`` opaco fica preso à Task (nunca
vaza em resposta A2A) e o retry leva ``inputResponses`` + ``requestState``
com id JSON-RPC novo (o dispatcher minta um a cada request).

    uv run python agente/agente.py

Env: ``AGENTE_PORT`` (7300), ``MCP_URL`` (http://localhost:7301/mcp).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import secrets
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import uvicorn
from mcp import Client
from mcp.client.session import ClientSession
from mcp.shared.exceptions import MCPError
from mcp_types import (
    CLIENT_CAPABILITIES_META_KEY,
    CallToolResult,
    ElicitRequestFormParams,
    ElicitResult,
    InputRequiredResult,
    InputResponses,
    RequestParamsMeta,
)
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

# ---------------------------------------------------------------------------
# Constantes de contrato
# ---------------------------------------------------------------------------

PROTOCOLO_MCP = "2026-07-28"
"""Revisão da spec MCP travada para todos os requests do host."""

URI_POLITICA = "politica://uso"
TOOL_RESERVAR = "reservar_sala"

RE_VERSAO = re.compile(r"versao:\s*(\S+)")
"""Versão declarada na primeira linha do resource (``versao: 2026-11-01``)."""

RE_PEDIDO = re.compile(
    r"^reservar\s+sala=(\S+)\s+inicio=(\S+)\s+fim=(\S+)\s+responsavel=(.+)$"
)
RE_ESCOLHA = re.compile(r"^escolha=(\S+)$")

ERRO_PARSE = -32700
ERRO_REQUISICAO = -32600
ERRO_METODO = -32601
ERRO_PARAMS = -32602
ERRO_INTERNO = -32603


@dataclass(frozen=True)
class ErroJsonRpc(Exception):
    """Erro JSON-RPC 2.0 com código e mensagem; sobe até o handler A2A."""

    code: int
    message: str

    def __str__(self) -> str:  # pragma: no cover — trivial
        return f"[{self.code}] {self.message}"


# ---------------------------------------------------------------------------
# Modelo da Task A2A
# ---------------------------------------------------------------------------

SUBMETIDA = "TASK_STATE_SUBMITTED"
TRABALHANDO = "TASK_STATE_WORKING"
PAUSADA = "TASK_STATE_INPUT_REQUIRED"
CONCLUIDA = "TASK_STATE_COMPLETED"
CANCELADA = "TASK_STATE_CANCELED"
FALHOU = "TASK_STATE_FAILED"

TERMINAIS = frozenset({CONCLUIDA, CANCELADA, FALHOU})


@dataclass
class Mensagem:
    """Mensagem A2A no formato dos wires 08–10."""

    message_id: str
    role: str
    parts: list[dict[str, Any]]
    task_id: str | None = None
    context_id: str | None = None

    def para_json(self) -> dict[str, Any]:
        corpo: dict[str, Any] = {
            "messageId": self.message_id,
            "role": self.role,
            "parts": self.parts,
        }
        if self.task_id is not None:
            corpo["taskId"] = self.task_id
        if self.context_id is not None:
            corpo["contextId"] = self.context_id
        return corpo


@dataclass
class Task:
    """Task A2A com máquina de estados, histórico e o estado MCP da ponte."""

    id: str
    context_id: str
    estado: str = SUBMETIDA
    historico: list[Mensagem] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    msg_status: Mensagem | None = None
    # Estado da ponte — nunca vaza em resposta A2A (check 34):
    request_state: str | None = None
    chave_input: str | None = None
    alternativas: list[str] = field(default_factory=list)
    pedido: dict[str, str] | None = None
    traceparent: str | None = None

    def terminal(self) -> bool:
        return self.estado in TERMINAIS


# ---------------------------------------------------------------------------
# Host MCP
# ---------------------------------------------------------------------------


def _caps_elicitation_form_sem_callback(session: ClientSession) -> None:
    """Regrava o ``clientCapabilities`` do stamp moderno: elicitation form, sem url.

    O SDK só declara ``elicitation`` no ``_meta`` quando existe callback, e com
    callback declara ``form``+``url``. O contrato pede ``{"elicitation":
    {"form": {}}}`` **sem** callback — o MRTR aqui é stateless por request: a
    resposta à elicitation chega num request novo do cliente A2A, não por um
    canal de volta. O wrap preserva o stamp do SDK (protocolVersion,
    clientInfo, headers espelhados Mcp-Method/Mcp-Name) e só substitui a
    chave de capabilities.
    """
    original = session._stamp  # ponto de extensão do host (ver NOTAS-SDK.md)

    def stamp(dados: dict[str, Any], opts: dict[str, Any]) -> None:
        original(dados, opts)
        meta = dados.setdefault("params", {}).setdefault("_meta", {})
        meta[CLIENT_CAPABILITIES_META_KEY] = {"elicitation": {"form": {}}}

    session._stamp = stamp


class HostMcp:
    """Cliente MCP stateless da central de salas (descoberta e leitura em runtime).

    O transporte Streamable HTTP do SDK usa read timeout de 300s por padrão;
    para um servidor stateless de tool rápida, 30s por request bastam. A
    conexão é por processo (objeto cliente vivo entre chamadas é normal e
    recomendado — nada de estado de protocolo inferido de request anterior).
    """

    TIMEOUT_REQUEST = 30.0

    def __init__(self, url: str) -> None:
        self.url = url
        self._client: Client | None = None
        self._session: ClientSession | None = None
        self._tools_descobertas: list[str] | None = None
        self._versao_politica: str | None = None

    async def conectar(self) -> None:
        """Abre o transporte e pina a revisão 2026-07-28 (sem handshake, sem sessão)."""
        await self.fechar()
        client = Client(
            self.url,
            mode=PROTOCOLO_MCP,
            read_timeout_seconds=self.TIMEOUT_REQUEST,
        )
        await client.__aenter__()
        self._client = client
        self._session = client.session
        _caps_elicitation_form_sem_callback(self._session)

    async def fechar(self) -> None:
        if self._client is not None:
            with contextlib.suppress(Exception):
                await self._client.__aexit__(None, None, None)
            self._client = None
            self._session = None

    @property
    def session(self) -> ClientSession:
        if self._session is None:
            raise ErroJsonRpc(ERRO_INTERNO, "host MCP desconectado")
        return self._session

    @staticmethod
    def _meta(traceparent: str | None) -> RequestParamsMeta:
        # RequestParamsMeta é aberto (extra_items=Any): o traceparent segue
        # reservado no _meta e o stamp do SDK completa as chaves obrigatórias.
        return RequestParamsMeta(traceparent=traceparent) if traceparent else RequestParamsMeta()

    async def descobrir_tools(self) -> list[str]:
        """``tools/list`` em runtime, uma vez por processo — nada de lista fixa no código."""
        if self._tools_descobertas is None:
            resultado = await self.session.list_tools()
            self._tools_descobertas = [t.name for t in resultado.tools]
        return self._tools_descobertas

    async def versao_politica(self, traceparent: str | None = None) -> str:
        """Lê ``politica://uso`` e extrai a versão da primeira linha."""
        if self._versao_politica is None:
            resultado = await self.session.read_resource(
                URI_POLITICA, meta=self._meta(traceparent), allow_input_required=True
            )
            if isinstance(resultado, InputRequiredResult):  # pragma: no cover — política não pausa
                raise ErroJsonRpc(ERRO_INTERNO, f"leitura de {URI_POLITICA} pediu input")
            texto = "".join(c.text for c in resultado.contents)
            m = RE_VERSAO.search(texto)
            if not m:
                raise ErroJsonRpc(ERRO_INTERNO, f"{URI_POLITICA} não declara versão")
            self._versao_politica = m.group(1)
        return self._versao_politica

    async def chamar_reserva(
        self,
        pedido: dict[str, str],
        traceparent: str | None,
        input_responses: InputResponses | None = None,
        request_state: str | None = None,
    ) -> CallToolResult | InputRequiredResult:
        """``tools/call`` ``reservar_sala`` com MRTR manual.

        O dispatcher minta id JSON-RPC novo a cada request, então o retry sai
        com id ≠ inicial por construção. ``allow_input_required=True`` devolve
        o ``InputRequiredResult`` cru para a ponte decidir.
        """
        await self.descobrir_tools()  # descoberta antes do primeiro tools/call
        return await self.session.call_tool(
            TOOL_RESERVAR,
            arguments=pedido,
            input_responses=input_responses,
            request_state=request_state,
            meta=self._meta(traceparent),
            allow_input_required=True,
        )


def parse_input_required(resultado: InputRequiredResult) -> tuple[str, list[str]]:
    """Chave do ``inputRequests`` e o enum da elicitation, na ordem do servidor."""
    pedidos = resultado.input_requests or {}
    if len(pedidos) != 1:
        raise ErroJsonRpc(ERRO_INTERNO, f"esperava 1 inputRequest, veio {len(pedidos)}")
    chave, pedido = next(iter(pedidos.items()))
    params = pedido.params
    if not isinstance(params, ElicitRequestFormParams):
        raise ErroJsonRpc(ERRO_INTERNO, "elicitation não é form mode")
    campo = (params.requested_schema.get("properties") or {}).get("sala") or {}
    if isinstance(campo.get("enum"), list):
        return chave, [str(v) for v in campo["enum"]]
    if "const" in campo:  # schema de alternativa única pode virar const
        return chave, [str(campo["const"])]
    raise ErroJsonRpc(ERRO_INTERNO, "requestedSchema da elicitation não traz enum/const de sala")


# ---------------------------------------------------------------------------
# Núcleo do agente (a ponte)
# ---------------------------------------------------------------------------


class Agente:
    def __init__(self, host: HostMcp) -> None:
        self.host = host
        self.tasks: dict[str, Task] = {}

    # -- utilidades -------------------------------------------------------

    def _task(self, id_: str) -> Task:
        task = self.tasks.get(id_)
        if task is None:
            raise ErroJsonRpc(ERRO_PARAMS, f"Task inexistente: {id_}")
        return task

    def _task_para_json(self, task: Task) -> dict[str, Any]:
        status: dict[str, Any] = {"state": task.estado}
        if task.msg_status is not None:
            status["message"] = task.msg_status.para_json()
        return {
            "id": task.id,
            "contextId": task.context_id,
            "status": status,
            "history": [m.para_json() for m in task.historico],
            "artifacts": task.artifacts,
        }

    def _msg_agente(self, task: Task, texto: str) -> Mensagem:
        return Mensagem(
            message_id=f"msg-{secrets.token_hex(6)}",
            role="ROLE_AGENT",
            parts=[{"text": texto}],
            task_id=task.id,
            context_id=task.context_id,
        )

    # -- SendMessage ------------------------------------------------------

    async def enviar_mensagem(
        self, mensagem: dict[str, Any], traceparent: str | None
    ) -> dict[str, Any]:
        task_id = mensagem.get("taskId")
        texto = " ".join(p.get("text", "") for p in mensagem.get("parts", [])).strip()
        msg_usuario = Mensagem(
            message_id=mensagem.get("messageId") or f"msg-{secrets.token_hex(6)}",
            role=mensagem.get("role", "ROLE_USER"),
            parts=list(mensagem.get("parts", [])),
            task_id=task_id,
        )
        if task_id:
            return await self._continuar(task_id, texto, msg_usuario, traceparent)
        return await self._abrir(texto, msg_usuario, traceparent)

    async def _abrir(
        self, texto: str, msg_usuario: Mensagem, traceparent: str | None
    ) -> dict[str, Any]:
        m = RE_PEDIDO.match(texto)
        if not m:
            raise ErroJsonRpc(
                ERRO_PARAMS,
                "pedido fora do formato: "
                "reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>",
            )
        pedido = {"sala": m[1], "inicio": m[2], "fim": m[3], "responsavel": m[4]}
        task = Task(
            id=f"task-{secrets.token_hex(6)}",
            context_id=f"ctx-{secrets.token_hex(6)}",
            pedido=pedido,
            traceparent=traceparent,
        )
        self.tasks[task.id] = task
        return await self._executar(task, msg_usuario, traceparent)

    async def _continuar(
        self, task_id: str, texto: str, msg_usuario: Mensagem, traceparent: str | None
    ) -> dict[str, Any]:
        task = self._task(task_id)
        if task.terminal():
            raise ErroJsonRpc(
                ERRO_PARAMS,
                f"Task {task_id} em estado terminal ({task.estado}): não aceita novas mensagens",
            )
        m = RE_ESCOLHA.match(texto)
        if not m:
            raise ErroJsonRpc(
                ERRO_PARAMS, "continuação fora do formato: escolha=<id> | escolha=recusar"
            )
        escolha = m[1]
        if task.estado != PAUSADA or task.request_state is None or not task.chave_input:
            raise ErroJsonRpc(ERRO_PARAMS, f"Task {task_id} não está pausada esperando escolha")
        if traceparent:
            task.traceparent = traceparent

        if escolha != "recusar" and escolha not in task.alternativas:
            # Fora do enum: a Task continua INPUT_REQUIRED e a pausa repete a
            # mesma linha de alternativas, byte a byte (check 29).
            task.historico.append(msg_usuario)
            task.msg_status = self._msg_agente(
                task, f"alternativas: {', '.join(task.alternativas)}"
            )
            task.historico.append(task.msg_status)
            return self._task_para_json(task)

        resposta = (
            ElicitResult(action="decline")
            if escolha == "recusar"
            else ElicitResult(action="accept", content={"sala": escolha})
        )
        # A ponte de volta: o requestState preso à Task volta ao servidor MCP
        # num tools/call novo (id novo), com inputResponses na mesma chave.
        return await self._executar(
            task,
            msg_usuario,
            task.traceparent,
            input_responses={task.chave_input: resposta},
            request_state=task.request_state,
        )

    # -- fluxo central ----------------------------------------------------

    async def _executar(
        self,
        task: Task,
        msg_usuario: Mensagem,
        traceparent: str | None,
        input_responses: InputResponses | None = None,
        request_state: str | None = None,
    ) -> dict[str, Any]:
        assert task.pedido is not None
        task.estado = TRABALHANDO
        task.historico.append(msg_usuario)
        try:
            resultado = await self.host.chamar_reserva(
                task.pedido,
                traceparent,
                input_responses=input_responses,
                request_state=request_state,
            )
        except MCPError as e:
            # Erro de protocolo MCP (error JSON-RPC): a Task falha com a mensagem.
            task.estado = FALHOU
            task.msg_status = self._msg_agente(task, f"Erro MCP: {e.message}")
            task.historico.append(task.msg_status)
            self._limpar_ponte(task)
            return self._task_para_json(task)

        if isinstance(resultado, InputRequiredResult):
            # A ponte: input_required do MCP vira TASK_STATE_INPUT_REQUIRED.
            chave, alternativas = parse_input_required(resultado)
            task.estado = PAUSADA
            task.request_state = resultado.request_state
            task.chave_input = chave
            task.alternativas = alternativas
            task.msg_status = self._msg_agente(
                task, f"alternativas: {', '.join(alternativas)}"
            )
            task.historico.append(task.msg_status)
            return self._task_para_json(task)

        if resultado.is_error:
            # Erro de execução da tool: mensagem exata no histórico (check 35).
            task.estado = FALHOU
            texto_tool = " ".join(
                c.text for c in (resultado.content or []) if getattr(c, "text", None)
            )
            task.msg_status = self._msg_agente(task, texto_tool)
            task.historico.append(task.msg_status)
            self._limpar_ponte(task)
            return self._task_para_json(task)

        dados = dict(resultado.structured_content or {})
        if dados.get("reservado") is False:
            # Recusa (action decline): conclui sem reservar → CANCELED.
            task.estado = CANCELADA
            task.msg_status = self._msg_agente(task, "Reserva recusada.")
            task.historico.append(task.msg_status)
            self._limpar_ponte(task)
            return self._task_para_json(task)

        # Sucesso: artifact "reserva" com o JSON da reserva + política (wire 10).
        if not dados.get("politica"):
            dados["politica"] = await self.host.versao_politica(traceparent)
        task.estado = CONCLUIDA
        id_reserva = str(dados.get("reserva") or "")
        sala = str(dados.get("sala") or task.pedido["sala"])
        task.msg_status = self._msg_agente(task, f"Reserva {id_reserva} confirmada na {sala}.")
        task.historico.append(task.msg_status)
        task.artifacts.append(
            {
                "artifactId": f"art-{secrets.token_hex(6)}",
                "name": "reserva",
                "parts": [{"text": json.dumps(dados, ensure_ascii=False)}],
            }
        )
        self._limpar_ponte(task)
        return self._task_para_json(task)

    @staticmethod
    def _limpar_ponte(task: Task) -> None:
        task.request_state = None
        task.chave_input = None

    # -- GetTask ----------------------------------------------------------

    def obter_task(self, id_: str) -> dict[str, Any]:
        return self._task_para_json(self._task(id_))


# ---------------------------------------------------------------------------
# ASGI (A2A) — JSON-RPC 2.0 próprio, sem SDK de A2A
# ---------------------------------------------------------------------------

AGENT_CARD: dict[str, Any] = {
    "name": "Central de Salas",
    "description": "Reserva salas de reuniao da Hill Valley Tech.",
    "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
    "version": "1.0.0",
    "supportedInterfaces": [
        {
            "url": "http://127.0.0.1:7300/a2a",
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
        }
    ],
    "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": "reservar-sala",
            "name": "Reservar sala",
            "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
            "tags": ["salas", "agenda"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
            "examples": [
                "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
            ],
        }
    ],
}


def _erro_resposta(id_: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}})


async def card_endpoint(request: Request) -> JSONResponse:
    return JSONResponse(AGENT_CARD)


async def a2a_endpoint(request: Request) -> JSONResponse:
    agente: Agente = request.app.state.agente
    bruto = await request.body()
    try:
        corpo = json.loads(bruto)
    except json.JSONDecodeError:
        return _erro_resposta(None, ERRO_PARSE, "corpo não é JSON válido")
    if not isinstance(corpo, dict):
        return _erro_resposta(None, ERRO_REQUISICAO, "request JSON-RPC 2.0 inválido")
    id_ = corpo.get("id")
    metodo = corpo.get("method")
    if corpo.get("jsonrpc") != "2.0" or not isinstance(metodo, str):
        return _erro_resposta(id_, ERRO_REQUISICAO, "request JSON-RPC 2.0 inválido")
    try:
        if metodo == "SendMessage":
            mensagem = (corpo.get("params") or {}).get("message")
            if not isinstance(mensagem, dict):
                raise ErroJsonRpc(ERRO_PARAMS, "params.message ausente")
            task = await agente.enviar_mensagem(mensagem, request.headers.get("traceparent"))
        elif metodo == "GetTask":
            id_task = (corpo.get("params") or {}).get("id")
            if not isinstance(id_task, str) or not id_task:
                raise ErroJsonRpc(ERRO_PARAMS, "params.id ausente")
            task = agente.obter_task(id_task)
        else:
            raise ErroJsonRpc(ERRO_METODO, f"método desconhecido: {metodo}")
    except ErroJsonRpc as e:
        return _erro_resposta(id_, e.code, e.message)
    except Exception as e:  # noqa: BLE001 — barreira do endpoint A2A
        return _erro_resposta(id_, ERRO_INTERNO, f"erro interno: {e}")
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": {"task": task}})


def criar_app(agente: Agente, lifespan: Any = None) -> Starlette:
    app = Starlette(
        routes=[
            Route("/.well-known/agent-card.json", card_endpoint, methods=["GET"]),
            Route("/a2a", a2a_endpoint, methods=["POST"]),
        ],
        lifespan=lifespan,
    )
    app.state.agente = agente
    return app


# ---------------------------------------------------------------------------
# Subida
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Agente A2A Central de Salas (host MCP)")
    parser.add_argument("--porta", type=int, default=int(os.environ.get("AGENTE_PORT", "7300")))
    parser.add_argument(
        "--mcp-url", default=os.environ.get("MCP_URL", "http://localhost:7301/mcp")
    )
    args = parser.parse_args()

    AGENT_CARD["supportedInterfaces"][0]["url"] = f"http://127.0.0.1:{args.porta}/a2a"

    host = HostMcp(url=args.mcp_url)
    agente = Agente(host)

    @asynccontextmanager
    async def lifespan(app: Starlette) -> AsyncIterator[None]:
        await host.conectar()
        # Aquece a descoberta e a leitura da política; se o servidor MCP ainda
        # não estiver no ar (T1 em desenvolvimento), segue lazy na 1ª mensagem.
        try:
            tools = await host.descobrir_tools()
            versao = await host.versao_politica()
            print(f"[agente] tools descobertas: {tools}", file=sys.stderr)
            print(f"[agente] versao da politica: {versao}", file=sys.stderr)
        except Exception as e:  # noqa: BLE001 — subida tolerante à T1 ausente
            print(f"[agente] servidor MCP indisponível no startup ({e}); retry lazily", file=sys.stderr)
        try:
            yield
        finally:
            await host.fechar()

    app = criar_app(agente, lifespan=lifespan)
    config = uvicorn.Config(
        app, host="0.0.0.0", port=args.porta, log_level="info", lifespan="on"
    )
    uvicorn.Server(config).run()


if __name__ == "__main__":
    main()
