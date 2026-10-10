"""Servidor MCP do desafio "A Ponte" — salas de reunião da Hill Valley Tech.

Transporte Streamable HTTP stateless (era 2026-07-28: envelope por request em
``params._meta``, sem handshake ``initialize``), três tools, um resource, MRTR
na reserva via resolver de elicitation e ``requestState`` protegido por
AES-256-GCM (chave de ``REQUEST_STATE_SECRET``, TTL 15 min).

Sobe com ``uv run python servidor-mcp/servidor.py`` a partir da raiz do repo.
Requer ``REQUEST_STATE_SECRET`` (>= 32 bytes) no ambiente; veja o README.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from collections.abc import Callable
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, create_model

from mcp.server.elicitation import (
    AcceptedElicitation,
    CancelledElicitation,
    DeclinedElicitation,
    ElicitationResult,
)
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp.server.mcpserver.server import MCPServer
from mcp.server.request_state import RequestStateSecurity

# ---------------------------------------------------------------------------
# Constantes de domínio e mensagens de erro (byte a byte com o enunciado)
# ---------------------------------------------------------------------------

RAIZ = Path(__file__).resolve().parent.parent

MSG_SALA = "Sala inexistente: {sala}"
MSG_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
MSG_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
MSG_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
MSG_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"
MSG_ELICITACAO = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."

JANELA_INICIO = dt_time(8, 0)
JANELA_FIM = dt_time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
FUSO_SAO_PAULO = timezone(timedelta(hours=-3))


# ---------------------------------------------------------------------------
# Estado em memória (reservas) — semeado dos dados do starter
# ---------------------------------------------------------------------------


class Sala(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class Reserva(BaseModel):
    id: str
    sala: str
    inicio: str
    fim: str
    responsavel: str


def _carregar(nome: str, modelo: type[BaseModel]) -> list[BaseModel]:
    with (RAIZ / "dados" / nome).open(encoding="utf-8") as fh:
        return [modelo(**item) for item in json.load(fh)]


SALAS: list[Sala] = _carregar("salas.json", Sala)  # type: ignore[assignment]
RESERVAS: list[Reserva] = _carregar("reservas.json", Reserva)  # type: ignore[assignment]
SALAS_POR_ID = {s.id: s for s in SALAS}

with (RAIZ / "dados" / "politica-de-uso.md").open(encoding="utf-8") as fh:
    POLITICA_TEXTO = fh.read()

POLITICA_VERSAO = POLITICA_TEXTO.splitlines()[0].removeprefix("versao:").strip()

_sequencia = len(RESERVAS)
_trava_sequencia = threading.Lock()


def _proxima_reserva() -> str:
    global _sequencia
    with _trava_sequencia:
        _sequencia += 1
        return f"res-{_sequencia:04d}"


# ---------------------------------------------------------------------------
# Regras de negócio: política de uso, conflitos e alternativas
# ---------------------------------------------------------------------------


def validar_politica(sala: str, inicio: str, fim: str) -> tuple[Sala, datetime, datetime]:
    """Valida sala e política; devolve a sala e os instantes normalizados.

    Levanta ``ToolError`` com a mensagem exata do enunciado — o caminho do SDK
    preserva o texto (prefixado com "Error executing tool <nome>:").
    """
    if sala not in SALAS_POR_ID:
        raise ToolError(MSG_SALA.format(sala=sala))
    try:
        instante_inicio = datetime.fromisoformat(inicio)
        instante_fim = datetime.fromisoformat(fim)
    except (TypeError, ValueError) as exc:
        raise ToolError(MSG_INTERVALO) from exc
    if instante_fim <= instante_inicio:
        raise ToolError(MSG_INTERVALO)
    # A janela vale no horario de Sao Paulo: converte antes de comparar.
    inicio_sp = instante_inicio.astimezone(FUSO_SAO_PAULO).time()
    fim_sp = instante_fim.astimezone(FUSO_SAO_PAULO).time()
    if inicio_sp < JANELA_INICIO or fim_sp > JANELA_FIM:
        raise ToolError(MSG_JANELA)
    if instante_fim - instante_inicio > DURACAO_MAXIMA:
        raise ToolError(MSG_DURACAO)
    return SALAS_POR_ID[sala], instante_inicio, instante_fim


def conflitos_da_sala(sala: str, inicio: datetime, fim: datetime) -> list[Reserva]:
    return [
        r
        for r in RESERVAS
        if r.sala == sala and inicio < datetime.fromisoformat(r.fim) and datetime.fromisoformat(r.inicio) < fim
    ]


def alternativas(inicio: datetime, fim: datetime, capacidade_pedida: int) -> list[Sala]:
    """Salas livres no intervalo com capacidade >=, máx 3, capacidade crescente, empate por id."""
    livres = [
        s
        for s in SALAS
        if s.capacidade >= capacidade_pedida and not conflitos_da_sala(s.id, inicio, fim)
    ]
    livres.sort(key=lambda s: (s.capacidade, s.id))
    return livres[:3]


# ---------------------------------------------------------------------------
# Schemas de saída (publicam outputSchema e alimentam o structuredContent)
# ---------------------------------------------------------------------------


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


# ---------------------------------------------------------------------------
# Resolver de MRTR: a pergunta da elicitation
# ---------------------------------------------------------------------------


def escolha_de_sala(sala: str, inicio: str, fim: str) -> Any:
    """Decide se há o que perguntar antes de executar ``reservar_sala``.

    Intervalo livre: nada a perguntar (devolve ``None``; a tool conclui).
    Conflito com alternativas: devolve ``Elicit`` com o enum das alternativas.
    Conflito sem alternativas: ``ToolError`` com a mensagem do enunciado —
    sem elicitation, a tool devolve erro de execução.
    """
    sala_obj, instante_inicio, instante_fim = validar_politica(sala, inicio, fim)
    if not conflitos_da_sala(sala_obj.id, instante_inicio, instante_fim):
        return None
    opcoes = alternativas(instante_inicio, instante_fim, sala_obj.capacidade)
    if not opcoes:
        raise ToolError(MSG_SEM_ALTERNATIVA)
    enum_de_salas = Literal[tuple(s.id for s in opcoes)]  # type: ignore[valid-type]
    modelo = create_model(
        "EscolhaDeSala",
        sala=(enum_de_salas, Field(..., description="Sala alternativa escolhida")),
    )
    return Elicit(MSG_ELICITACAO, modelo)


# ---------------------------------------------------------------------------
# Ferramentas
# ---------------------------------------------------------------------------


def listar_salas() -> ListaDeSalas:
    """Lista todas as salas com capacidade e recursos."""
    return ListaDeSalas(salas=[SalaOut(**s.model_dump()) for s in SALAS])


def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    """Diz se uma sala esta livre no intervalo, e quais reservas conflitam."""
    sala_obj, instante_inicio, instante_fim = validar_politica(sala, inicio, fim)
    conflitos = conflitos_da_sala(sala_obj.id, instante_inicio, instante_fim)
    return Disponibilidade(
        sala=sala_obj.id,
        livre=not conflitos,
        conflitos=[ConflitoOut(**c.model_dump()) for c in conflitos],
    )


def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult[Any], Resolve(escolha_de_sala)],
) -> ReservaOut:
    """Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar."""
    sala_obj, instante_inicio, instante_fim = validar_politica(sala, inicio, fim)

    if not conflitos_da_sala(sala_obj.id, instante_inicio, instante_fim):
        # Caminho livre: o resolver devolveu None e nada foi perguntado.
        return _criar_reserva(sala_obj.id, inicio, fim, responsavel)

    # Caminho com conflito: o resolver perguntou (ou a resposta ja esta selada
    # no requestState e foi restaurada pelo framework).
    if isinstance(escolha, DeclinedElicitation | CancelledElicitation):
        return ReservaOut(reservado=False, motivo="recusado")
    if not isinstance(escolha, AcceptedElicitation) or escolha.data is None:
        raise ToolError(MSG_SEM_ALTERNATIVA)
    escolhida = SALAS_POR_ID[escolha.data.sala]
    if conflitos_da_sala(escolhida.id, instante_inicio, instante_fim):
        raise ToolError(MSG_SEM_ALTERNATIVA)
    return _criar_reserva(escolhida.id, inicio, fim, responsavel)


def _criar_reserva(sala: str, inicio: str, fim: str, responsavel: str) -> ReservaOut:
    RESERVAS.append(Reserva(id=_proxima_reserva(), sala=sala, inicio=inicio, fim=fim, responsavel=responsavel))
    return ReservaOut(
        reserva=RESERVAS[-1].id,
        reservado=True,
        sala=sala,
        inicio=inicio,
        fim=fim,
        responsavel=responsavel,
        politica=POLITICA_VERSAO,
    )


# ---------------------------------------------------------------------------
# Log em stderr de cada request recebido (método, id, traceparent)
# ---------------------------------------------------------------------------


def _log_stderr(linha: str) -> None:
    print(linha, file=sys.stderr, flush=True)


def _log_request_bruto(corpo: bytes) -> None:
    """Extrai método, id e traceparent do corpo bruto de um POST e registra no stderr."""
    try:
        mensagem = json.loads(corpo)
    except (ValueError, TypeError):
        _log_stderr("request method=? id=? traceparent=- (corpo nao parseavel)")
        return
    if not isinstance(mensagem, dict):
        _log_stderr("request method=? id=? traceparent=- (corpo nao e objeto)")
        return
    meta = ((mensagem.get("params") or {}).get("_meta") or {}) if isinstance(mensagem.get("params"), dict) else {}
    traceparent = meta.get("traceparent", "-")
    _log_stderr(f"request method={mensagem.get('method')} id={mensagem.get('id')!r} traceparent={traceparent}")


class LogRequestsASGI:
    """Envolve a app ASGI para registrar em stderr cada request MCP recebido.

    O log acontece na borda do transporte, antes da escada de validação, então
    cobre inclusive requests rejeitados por envelope incompleto ou capability
    faltante — o ServerMiddleware do SDK só roda após essa escada.
    """

    def __init__(self, inner: Callable) -> None:
        self._inner = inner

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[no-untyped-def]
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self._inner(scope, receive, send)
            return

        pedacos: list[bytes] = []
        completo = False

        async def receive_com_captura() -> Any:
            nonlocal completo
            mensagem = await receive()
            if mensagem["type"] == "http.request" and not completo:
                if mensagem.get("body"):
                    pedacos.append(mensagem["body"])
                if not mensagem.get("more_body", False):
                    completo = True
                    _log_request_bruto(b"".join(pedacos))
            return mensagem

        await self._inner(scope, receive_com_captura, send)


# ---------------------------------------------------------------------------
# Montagem e execução
# ---------------------------------------------------------------------------


def _segredo() -> str:
    valor = os.environ.get("REQUEST_STATE_SECRET")
    if not valor or len(valor.encode("utf-8")) < 32:
        _log_stderr(
            "ERRO: REQUEST_STATE_SECRET ausente ou com menos de 32 bytes. "
            'Gere um com: python3 -c "import secrets; print(secrets.token_hex(32))"'
        )
        raise SystemExit(2)
    return valor


def main() -> None:
    import uvicorn

    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "7301"))

    servidor = MCPServer(
        "central-de-salas",
        version="1.0.0",
        request_state_security=RequestStateSecurity(keys=[_segredo()], ttl=900.0, bind_principal=None),
    )
    servidor.add_tool(listar_salas, name="listar_salas")
    servidor.add_tool(consultar_disponibilidade, name="consultar_disponibilidade")
    servidor.add_tool(reservar_sala, name="reservar_sala")

    @servidor.resource("politica://uso", name="politica-de-uso", mime_type="text/markdown")
    def politica() -> str:
        return POLITICA_TEXTO

    app = LogRequestsASGI(
        servidor.streamable_http_app(streamable_http_path="/mcp", json_response=True, host=host)
    )
    _log_stderr(f"servidor-mcp ouvindo em http://{host}:{port}/mcp (protocolo 2026-07-28)")
    uvicorn.run(app, host=host, port=port, log_level="warning")  # type: ignore[arg-type]


if __name__ == "__main__":
    main()
