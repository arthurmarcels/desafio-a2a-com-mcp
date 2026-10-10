#!/usr/bin/env python3
"""Mock de apoio do servidor MCP (contrato dos checks 01–20) — SUÍTE DE TESTES.

Não é produção: existe para exercitar o agente (checks 21–36) enquanto a T1
(``servidor-mcp/``) não está no ar. Reproduz o contrato dos wires 01–06:

* ``tools/list`` com as três tools;
* ``tools/call`` com ``isError`` para sala inexistente / política violada;
* ``resources/read`` de ``politica://uso`` (conteúdo de ``dados/``);
* ciclo MRTR: conflito → ``input_required`` com elicitation form + enum das
  alternativas (capacidade ≥ 12, capacidade crescente, empate por id) e
  ``requestState`` opaco (HMAC); retry → ``complete`` selando os argumentos
  originais; ``decline`` → ``reservado: false``.

    uv run python agente/tests/mock_mcp.py [--porta 7301]
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

PROTOCOLO = "2026-07-28"
POLITICA = "2026-11-01"

RAIZ = Path(__file__).resolve().parents[2]
ARQ_SALAS = RAIZ / "dados" / "salas.json"
ARQ_RESERVAS = RAIZ / "dados" / "reservas.json"
ARQ_POLITICA = RAIZ / "dados" / "politica-de-uso.md"

SECRET_BYTES = secrets.token_bytes(32)  # em memória: o mock não precisa sobreviver a restart
TTL_REQUEST_STATE = 15 * 60

ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"


def log(msg: str) -> None:
    print(f"[mock-mcp] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Domínio (espelha a regra que a T1 implementa de verdade)
# ---------------------------------------------------------------------------


def carregar_salas() -> list[dict[str, Any]]:
    return json.loads(ARQ_SALAS.read_text())


def hora_de(iso: str) -> str:
    """``2026-11-03T14:00:00-03:00`` → ``14:00`` (o domínio é um único dia de prova)."""
    return iso[11:16]


def janela_violada(inicio: str, fim: str) -> bool:
    return not ("08:00" <= hora_de(inicio) <= "20:00" and "08:00" <= hora_de(fim) <= "20:00")


def minutos(hora: str) -> int:
    h, m = hora.split(":")
    return int(h) * 60 + int(m)


def duracao_acima(inicio: str, fim: str) -> bool:
    return minutos(hora_de(fim)) - minutos(hora_de(inicio)) > 120


def intervalo_invalido(inicio: str, fim: str) -> bool:
    return fim <= inicio


def sobrepoe(a_ini: str, a_fim: str, b_ini: str, b_fim: str) -> bool:
    return a_ini < b_fim and b_ini < a_fim


class Estado:
    def __init__(self) -> None:
        self.salas = {s["id"]: s for s in carregar_salas()}
        self.reservas: list[dict[str, str]] = json.loads(ARQ_RESERVAS.read_text())
        self.contador_reserva = len(self.reservas)

    def conflito(self, sala: str, inicio: str, fim: str) -> bool:
        return any(
            r["sala"] == sala and sobrepoe(inicio, fim, r["inicio"], r["fim"])
            for r in self.reservas
        )

    def alternativas(self, sala_pedida: str, inicio: str, fim: str) -> list[str]:
        cap_min = self.salas[sala_pedida]["capacidade"]
        livres = [
            s
            for s in self.salas.values()
            if s["id"] != sala_pedida
            and s["capacidade"] >= cap_min
            and not self.conflito(s["id"], inicio, fim)
        ]
        livres.sort(key=lambda s: (s["capacidade"], s["id"]))
        return [s["id"] for s in livres[:3]]

    def reservar(self, sala: str, inicio: str, fim: str, responsavel: str) -> dict[str, Any]:
        self.contador_reserva += 1
        reserva = {
            "reserva": f"res-{self.contador_reserva:04d}",
            "reservado": True,
            "sala": sala,
            "inicio": inicio,
            "fim": fim,
            "responsavel": responsavel,
            "politica": POLITICA,
            "motivo": None,
        }
        self.reservas.append({"id": reserva["reserva"], "sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel})
        return reserva


ESTADO = Estado()


# ---------------------------------------------------------------------------
# requestState opaco (HMAC + expiração + argumentos selados)
# ---------------------------------------------------------------------------


def selar(pedido: dict[str, str]) -> str:
    corpo = json.dumps(
        {"args": pedido, "exp": int(time.time()) + TTL_REQUEST_STATE, "n": secrets.token_hex(8)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    mac = hmac.new(SECRET_BYTES, corpo, hashlib.sha256).digest()
    return "v1." + corpo.hex() + "." + mac.hex()


def abrir(estado: str) -> dict[str, Any] | None:
    """Devolve o payload selado ou ``None`` se inválido/expirado/adulterado."""
    try:
        prefixo, corpo_hex, mac_hex = estado.split(".")
        if prefixo != "v1":
            return None
        corpo = bytes.fromhex(corpo_hex)
        esperado = hmac.new(SECRET_BYTES, corpo, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(esperado, mac_hex):
            return None
        payload = json.loads(corpo)
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except (ValueError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Ferramentas
# ---------------------------------------------------------------------------


def resultado_erro(mensagem: str) -> dict[str, Any]:
    return {
        "content": [{"text": mensagem, "type": "text"}],
        "isError": True,
        "resultType": "complete",
        "cacheScope": "private",
        "ttlMs": 0,
    }


def resultado_ok(dados: dict[str, Any] | None = None) -> dict[str, Any]:
    corpo: dict[str, Any] = {
        "isError": False,
        "resultType": "complete",
        "cacheScope": "private",
        "ttlMs": 0,
    }
    if dados is not None:
        corpo["content"] = [{"text": json.dumps(dados, indent=2), "type": "text"}]
        corpo["structuredContent"] = dados
    return corpo


def validar_politica(sala: str, inicio: str, fim: str) -> str | None:
    if sala not in ESTADO.salas:
        return f"Sala inexistente: {sala}"
    if intervalo_invalido(inicio, fim):
        return ERRO_INTERVALO
    if janela_violada(inicio, fim):
        return ERRO_JANELA
    if duracao_acima(inicio, fim):
        return ERRO_DURACAO
    return None


def tool_reservar(arguments: dict[str, Any], selado: dict[str, Any] | None) -> dict[str, Any]:
    # No retry, os argumentos selados vencem: o cliente não é confiável.
    args = copy.deepcopy(selado["args"]) if selado else arguments
    sala, inicio, fim = args["sala"], args["inicio"], args["fim"]
    if (erro := validar_politica(sala, inicio, fim)):
        return resultado_erro(erro)
    if ESTADO.conflito(sala, inicio, fim):
        alts = ESTADO.alternativas(sala, inicio, fim)
        if not alts:
            return resultado_erro(ERRO_SEM_ALTERNATIVA)
        return {
            "inputRequests": {
                "__main__:escolha_de_sala": {
                    "method": "elicitation/create",
                    "params": {
                        "message": "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.",
                        "mode": "form",
                        "requestedSchema": {
                            "properties": {
                                "sala": {
                                    "description": "Sala alternativa escolhida",
                                    **({"enum": alts} if len(alts) > 1 else {"const": alts[0]}),
                                    "title": "Sala",
                                    "type": "string",
                                }
                            },
                            "required": ["sala"],
                            "type": "object",
                        },
                    },
                }
            },
            "requestState": selar({"sala": sala, "inicio": inicio, "fim": fim, "responsavel": args["responsavel"]}),
            "resultType": "input_required",
        }
    reserva = ESTADO.reservar(sala, inicio, fim, args["responsavel"])
    return resultado_ok(reserva)


def tool_reservar_retry(
    input_responses: dict[str, Any], selado: dict[str, Any]
) -> dict[str, Any]:
    resposta = next(iter(input_responses.values()))
    action = resposta.get("action")
    if action in ("decline", "cancel"):
        dados = {
            "reserva": None, "reservado": False, "sala": None, "inicio": None,
            "fim": None, "responsavel": None, "politica": None, "motivo": "recusado",
        }
        return resultado_ok(dados)
    escolha = (resposta.get("content") or {}).get("sala")
    args = selado["args"]
    sala, inicio, fim = escolha, args["inicio"], args["fim"]
    if (erro := validar_politica(sala, inicio, fim)):
        return resultado_erro(erro)
    if ESTADO.conflito(sala, inicio, fim):
        return resultado_erro("Sem alternativas disponiveis no intervalo")
    reserva = ESTADO.reservar(sala, inicio, fim, args["responsavel"])
    return resultado_ok(reserva)


# ---------------------------------------------------------------------------
# ASGI
# ---------------------------------------------------------------------------


TOOLS: list[dict[str, Any]] = [
    {
        "name": "listar_salas",
        "description": "Lista as salas de reunião.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "consultar_disponibilidade",
        "description": "Consulta a disponibilidade de uma sala num intervalo.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sala": {"type": "string"},
                "inicio": {"type": "string"},
                "fim": {"type": "string"},
            },
            "required": ["sala", "inicio", "fim"],
        },
    },
    {
        "name": "reservar_sala",
        "description": "Reserva uma sala num intervalo, com MRTR em caso de conflito.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sala": {"type": "string"},
                "inicio": {"type": "string"},
                "fim": {"type": "string"},
                "responsavel": {"type": "string"},
            },
            "required": ["sala", "inicio", "fim", "responsavel"],
        },
    },
]


def _erro(id_: Any, code: int, message: str, status: int = 200, data: Any = None) -> JSONResponse:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": err}, status_code=status)


async def mcp_endpoint(request: Request) -> JSONResponse:
    try:
        corpo = json.loads(await request.body())
    except json.JSONDecodeError:
        return _erro(None, -32700, "Parse error", 400)
    id_ = corpo.get("id")
    metodo = corpo.get("method")
    params = corpo.get("params") or {}
    meta = params.get("_meta") or {}

    traceparent = meta.get("traceparent")
    log(f"method={metodo} id={id_} traceparent={traceparent}")

    # _meta obrigatório (checks 04/05)
    if "io.modelcontextprotocol/protocolVersion" not in meta:
        return _erro(id_, -32602, "Missing params._meta['io.modelcontextprotocol/protocolVersion']", 400)
    if "io.modelcontextprotocol/clientCapabilities" not in meta:
        return _erro(id_, -32602, "Missing params._meta['io.modelcontextprotocol/clientCapabilities']", 400)

    # Headers espelhados (o agente precisa enviar)
    esperado_method = request.headers.get("Mcp-Method", "")
    if esperado_method and esperado_method != metodo:
        return _erro(id_, -32020, f"Header Mcp-Method mismatch: {esperado_method} != {metodo}", 400)

    if metodo == "tools/list":
        return JSONResponse(
            {"jsonrpc": "2.0", "id": id_, "result": {"tools": TOOLS, "cacheScope": "private", "resultType": "complete", "ttlMs": 0}}
        )

    if metodo == "resources/read":
        uri = params.get("uri", "")
        esperado_name = request.headers.get("Mcp-Name", "")
        if esperado_name and esperado_name != uri:
            return _erro(id_, -32020, f"Header Mcp-Name mismatch: {esperado_name} != {uri}", 400)
        if uri != "politica://uso":
            return _erro(id_, -32602, f"Resource not found: {uri}", 400)
        texto = ARQ_POLITICA.read_text()
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": id_,
                "result": {
                    "cacheScope": "private",
                    "contents": [{"mimeType": "text/markdown", "text": texto, "uri": uri}],
                    "resultType": "complete",
                    "ttlMs": 0,
                },
            }
        )

    if metodo == "tools/call":
        nome = params.get("name", "")
        esperado_name = request.headers.get("Mcp-Name", "")
        if esperado_name and esperado_name != nome:
            return _erro(id_, -32020, f"Header Mcp-Name mismatch: {esperado_name} != {nome}", 400)
        if nome not in {t["name"] for t in TOOLS}:
            return _erro(id_, -32602, f"Unknown tool: {nome}", 400)
        arguments = params.get("arguments") or {}
        input_responses = params.get("inputResponses")
        request_state = params.get("requestState")

        selado: dict[str, Any] | None = None
        if request_state:
            selado = abrir(request_state)
            if selado is None:
                return _erro(id_, -32602, "requestState invalido ou expirado", 400)

        if nome == "reservar_sala":
            if selado and input_responses:
                resultado = tool_reservar_retry(input_responses, selado)
            elif selado:
                resultado = tool_reservar(arguments, selado)
            else:
                # capability elicitation form é pré-requisito do MRTR (check 15)
                caps = meta.get("io.modelcontextprotocol/clientCapabilities") or {}
                if "elicitation" not in caps or "form" not in (caps.get("elicitation") or {}):
                    return _erro(
                        id_,
                        -32021,
                        "Client did not declare the form elicitation capability required by resolver '__main__:escolha_de_sala'",
                        400,
                        {"requiredCapabilities": {"elicitation": {"form": {}}}},
                    )
                resultado = tool_reservar(arguments, None)
            return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": resultado})

        if nome == "listar_salas":
            dados = list(ESTADO.salas.values())
            return JSONResponse(
                {"jsonrpc": "2.0", "id": id_, "result": resultado_ok({"salas": dados})}
            )

        if nome == "consultar_disponibilidade":
            sala, inicio, fim = arguments.get("sala", ""), arguments.get("inicio", ""), arguments.get("fim", "")
            if (erro := validar_politica(sala, inicio, fim)):
                return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": resultado_erro(erro)})
            livre = not ESTADO.conflito(sala, inicio, fim)
            dados = {"sala": sala, "inicio": inicio, "fim": fim, "livre": livre}
            return JSONResponse(
                {"jsonrpc": "2.0", "id": id_, "result": resultado_ok(dados)}
            )

    return _erro(id_, -32601, f"Method not found: {metodo}", 400)


def criar_app() -> Starlette:
    return Starlette(routes=[Route("/mcp", mcp_endpoint, methods=["POST"])])


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock do servidor MCP (suíte de testes da T2)")
    parser.add_argument("--porta", type=int, default=int(os.environ.get("MCP_PORT", "7301")))
    args = parser.parse_args()
    config = uvicorn.Config(criar_app(), host="127.0.0.1", port=args.porta, log_level="warning")
    uvicorn.Server(config).run()


if __name__ == "__main__":
    main()
