"""Teste de integração da ponte (fase 1: agente + mock do servidor MCP).

Exercita o mesmo roteiro dos checks 21–36 do validador, por HTTP real contra
o agente com o mock de ``agente/tests/mock_mcp.py`` no lugar da T1. É a
evidência de prontidão enquanto ``servidor-mcp/`` não existe; a prova final
é o validador oficial com a T1 no ar.

    uv run pytest agente/tests/test_ponte.py -q
"""

from __future__ import annotations

import json
import secrets
import urllib.request
from typing import Any

import pytest

DIA = "2026-11-03"


def h(hora: str) -> str:
    return f"{DIA}T{hora}:00-03:00"


def post(url: str, corpo: dict[str, Any], traceparent: str | None = None) -> dict[str, Any]:
    headers = {"Content-Type": "application/json"}
    if traceparent:
        headers["traceparent"] = traceparent
    req = urllib.request.Request(url, data=json.dumps(corpo).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def enviar(url: str, texto: str, task_id: str | None = None, traceparent: str | None = None) -> dict[str, Any]:
    mensagem: dict[str, Any] = {
        "messageId": f"msg-{secrets.token_hex(6)}",
        "role": "ROLE_USER",
        "parts": [{"text": texto}],
    }
    if task_id:
        mensagem["taskId"] = task_id
    return post(url, {"jsonrpc": "2.0", "id": secrets.token_hex(6), "method": "SendMessage", "params": {"message": mensagem}}, traceparent)


def get(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode())


def tarefa(resposta: dict[str, Any]) -> dict[str, Any]:
    return (resposta.get("result") or {}).get("task") or resposta.get("result") or {}


def estado(resposta: dict[str, Any]) -> str:
    return (tarefa(resposta).get("status") or {}).get("state", "")


def mensagem(resposta: dict[str, Any]) -> str:
    status = tarefa(resposta).get("status") or {}
    return " ".join(p.get("text", "") for p in (status.get("message") or {}).get("parts") or [])


def artifact(resposta: dict[str, Any]) -> dict[str, Any]:
    arts = tarefa(resposta).get("artifacts") or []
    if not arts:
        return {}
    try:
        return json.loads(" ".join(p.get("text", "") for p in arts[0].get("parts", [])))
    except json.JSONDecodeError:
        return {}


TRACEPARENT = f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"


@pytest.fixture(scope="module")
def base_url(pytestconfig: Any) -> Any:
    """Sobe mock MCP + agente em portas vivas e devolve a URL base do agente."""
    import subprocess
    import sys
    import time
    from pathlib import Path

    raiz = Path(__file__).resolve().parents[2]
    porta_mcp, porta_agente = "7391", "7390"
    procs: list[subprocess.Popen] = []
    try:
        procs.append(
            subprocess.Popen(
                [sys.executable, str(raiz / "agente" / "tests" / "mock_mcp.py"), "--porta", porta_mcp],
                cwd=raiz,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        time.sleep(0.8)
        procs.append(
            subprocess.Popen(
                [sys.executable, str(raiz / "agente" / "agente.py"), "--porta", porta_agente, "--mcp-url", f"http://127.0.0.1:{porta_mcp}/mcp"],
                cwd=raiz,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        base = f"http://127.0.0.1:{porta_agente}"
        for _ in range(60):
            try:
                get(f"{base}/.well-known/agent-card.json")
                break
            except OSError:
                time.sleep(0.3)
        else:
            raise RuntimeError("agente não subiu")
        yield base
    finally:
        for p in procs:
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()


# ---------------------------------------------------------------------------
# Checks 21–26: card, caminho feliz, artifact, GetTask
# ---------------------------------------------------------------------------


def test_check_21_card_no_well_known(base_url: str) -> None:
    card = get(f"{base_url}/.well-known/agent-card.json")
    assert card["name"] == "Central de Salas"


def test_check_22_interface_jsonrpc_10(base_url: str) -> None:
    card = get(f"{base_url}/.well-known/agent-card.json")
    interfaces = card.get("supportedInterfaces") or []
    jsonrpc = [i for i in interfaces if str(i.get("protocolBinding", "")).upper().startswith("JSONRPC")]
    assert jsonrpc and any(i.get("url") for i in jsonrpc)
    assert any(str(i.get("protocolVersion", "")).startswith("1.0") for i in jsonrpc)
    # zero campos v0.x
    assert "interfaces" not in card
    assert "preferredTransport" not in card
    assert "additionalInterfaces" not in card


def test_check_23_skill_reservar_sala(base_url: str) -> None:
    card = get(f"{base_url}/.well-known/agent-card.json")
    skills = {s.get("id") for s in card.get("skills", [])}
    assert "reservar-sala" in skills


def test_check_24_sala_livre_conclui(base_url: str) -> None:
    r = enviar(f"{base_url}/a2a", f"reservar sala=sala-porao inicio={h('09:00')} fim={h('10:00')} responsavel=Doc", traceparent=TRACEPARENT)
    assert estado(r) == "TASK_STATE_COMPLETED", mensagem(r)


def test_check_25_artifact_reserva_com_politica(base_url: str) -> None:
    r = enviar(f"{base_url}/a2a", f"reservar sala=sala-porao inicio={h('10:00')} fim={h('11:00')} responsavel=Doc")
    t = tarefa(r)
    arts = t.get("artifacts") or []
    assert arts and arts[0].get("name") == "reserva"
    conteudo = artifact(r)
    assert conteudo.get("politica") == "2026-11-01"
    assert conteudo.get("sala") == "sala-porao"


def test_check_26_get_task(base_url: str) -> None:
    r = enviar(f"{base_url}/a2a", f"reservar sala=sala-porao inicio={h('11:00')} fim={h('12:00')} responsavel=Doc")
    tarefa_criada = tarefa(r)
    r2 = post(f"{base_url}/a2a", {"jsonrpc": "2.0", "id": "g1", "method": "GetTask", "params": {"id": tarefa_criada["id"]}})
    t = tarefa(r2)
    assert t.get("id") == tarefa_criada["id"]
    assert t.get("contextId")
    assert (t.get("status") or {}).get("state") == "TASK_STATE_COMPLETED"


# ---------------------------------------------------------------------------
# Checks 27–31: pausa, escolha fora do enum, retomada, task terminal
# ---------------------------------------------------------------------------


def test_checks_27_a_30_pausa_e_retomada(base_url: str) -> None:
    r = enviar(f"{base_url}/a2a", f"reservar sala=sala-garagem inicio={h('14:00')} fim={h('15:00')} responsavel=Marty", traceparent=TRACEPARENT)
    pausada = tarefa(r)
    assert estado(r) == "TASK_STATE_INPUT_REQUIRED", estado(r)
    assert "alternativas: sala-fusca, sala-mirante" in mensagem(r)

    # 29: fora do enum mantém pausada
    r = enviar(f"{base_url}/a2a", "escolha=sala-aquario", task_id=pausada["id"])
    assert estado(r) == "TASK_STATE_INPUT_REQUIRED"

    # 30: retomada conclui na sala escolhida
    r = enviar(f"{base_url}/a2a", "escolha=sala-fusca", task_id=pausada["id"], traceparent=TRACEPARENT)
    assert estado(r) == "TASK_STATE_COMPLETED", mensagem(r)
    assert artifact(r).get("sala") == "sala-fusca"

    # 31: task terminal recusa nova mensagem
    r = enviar(f"{base_url}/a2a", "escolha=sala-mirante", task_id=pausada["id"])
    assert r.get("error")


# ---------------------------------------------------------------------------
# Check 32: recusa → CANCELED
# ---------------------------------------------------------------------------


def test_check_32_recusa_cancela(base_url: str) -> None:
    r = enviar(f"{base_url}/a2a", f"reservar sala=sala-garagem inicio={h('14:30')} fim={h('15:30')} responsavel=Biff")
    recusavel = tarefa(r)
    assert estado(r) == "TASK_STATE_INPUT_REQUIRED"
    r = enviar(f"{base_url}/a2a", "escolha=recusar", task_id=recusavel["id"])
    assert estado(r) == "TASK_STATE_CANCELED", estado(r)


# ---------------------------------------------------------------------------
# Check 33: duas tasks pausadas simultâneas isoladas
# ---------------------------------------------------------------------------


def test_check_33_tasks_simultaneas(base_url: str) -> None:
    a = tarefa(enviar(f"{base_url}/a2a", f"reservar sala=sala-fusca inicio={h('16:00')} fim={h('17:00')} responsavel=Lorraine"))
    b = tarefa(enviar(f"{base_url}/a2a", f"reservar sala=sala-garagem inicio={h('14:00')} fim={h('15:00')} responsavel=George"))
    assert a["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert b["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    ra = enviar(f"{base_url}/a2a", "escolha=sala-mirante", task_id=a["id"])
    rb = enviar(f"{base_url}/a2a", "escolha=sala-mirante", task_id=b["id"])
    assert estado(ra) == "TASK_STATE_COMPLETED" and estado(rb) == "TASK_STATE_COMPLETED"
    ca, cb = artifact(ra), artifact(rb)
    assert ca.get("reserva") != cb.get("reserva")
    assert ca.get("inicio") != cb.get("inicio")


# ---------------------------------------------------------------------------
# Check 35: sala inexistente → FAILED com mensagem exata
# ---------------------------------------------------------------------------


def test_check_35_sala_inexistente_falha(base_url: str) -> None:
    r = enviar(f"{base_url}/a2a", f"reservar sala=sala-delorean inicio={h('09:00')} fim={h('10:00')} responsavel=Doc")
    assert estado(r) == "TASK_STATE_FAILED", estado(r)
    assert "Sala inexistente: sala-delorean" in mensagem(r)


# ---------------------------------------------------------------------------
# Check 36: determinismo da pausa
# ---------------------------------------------------------------------------


def test_check_36_pausa_deterministica(base_url: str) -> None:
    pedido = f"reservar sala=sala-porao inicio={h('09:00')} fim={h('10:00')} responsavel=Doc"  # já reservada no check 24
    m1 = mensagem(enviar(f"{base_url}/a2a", pedido))
    m2 = mensagem(enviar(f"{base_url}/a2a", pedido))
    assert m1 == m2
    assert m1.startswith("alternativas:")
