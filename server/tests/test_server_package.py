"""Pacote do servidor (horun_agent_server) testado de ponta a ponta com o
agente DE VERDADE (agent/), sem rede: um app FastAPI mínimo monta as rotas
como um módulo faria, e um "agente" numa thread consulta as tarefas pelo
TestClient e executa com `agent.runner.execute_task` sobre pastas
temporárias."""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine, select

from agent import __version__ as AGENT_VERSION
from agent.client import Task
from agent.config import RootConfig, ServerConfig
from agent.runner import execute_task
from horun_agent_server import bridge, models
from horun_agent_server.routes import build_router
from horun_agent_server.settings import settings


class Admin:
    id = 7
    username = "admin"


@pytest.fixture()
def engine():
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture()
def client(engine):
    def get_session():
        with Session(engine) as s:
            yield s

    app = FastAPI()
    app.include_router(build_router(
        get_session=get_session, admin_dependency=lambda: Admin(), actor_of=lambda a: (a.id, a.username),
    ))
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_settings():
    saved = (settings.task_timeout_seconds, settings.fail_fast_when_offline, settings.timeout_provider)
    yield
    settings.task_timeout_seconds, settings.fail_fast_when_offline, settings.timeout_provider = saved


def _enroll(client, *, version: str | None = AGENT_VERSION) -> dict:
    code = client.post("/agent/enroll-codes").json()["code"]
    token = client.post("/agent/enroll", json={"enroll_code": code, "device_name": "PC"}).json()["device_token"]
    headers = {"Authorization": f"Bearer {token}"}
    if version:
        headers["X-Horun-Agent-Version"] = version
    client.get("/agent/tasks", headers=headers)  # primeira consulta: registra versão e "visto em"
    return headers


@pytest.fixture()
def server_cfg(tmp_path):
    for name in ("data", "raw", "drive"):
        (tmp_path / name).mkdir()
    return ServerConfig(url="https://h", roots={
        "data": RootConfig((tmp_path / "data").resolve(), "read-write"),
        "raw": RootConfig((tmp_path / "raw").resolve(), "read-move"),
        "drive": RootConfig((tmp_path / "drive").resolve(), "read"),
    })


def _run_with_agent(client, headers, server_cfg, fn):
    """Roda `fn()` (que chama o bridge e fica esperando) numa thread e,
    enquanto isso, faz o papel do agente: consulta, executa, reporta."""
    result: dict = {}

    def target():
        try:
            result["value"] = fn()
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    t = threading.Thread(target=target)
    t.start()
    deadline = time.monotonic() + 10
    while t.is_alive() and time.monotonic() < deadline:
        for raw in client.get("/agent/tasks", headers=headers).json()["tasks"]:
            out = execute_task(server_cfg, Task.from_dict(raw))
            client.post(f"/agent/tasks/{raw['id']}/result", json=out, headers=headers)
        time.sleep(0.02)
    t.join(5)
    if "error" in result:
        raise result["error"]
    return result.get("value")


# ---------------------------------------------------------------- enrolamento


def test_enroll_code_is_long_unambiguous_and_single_use(client):
    body = client.post("/agent/enroll-codes").json()
    code = body["code"]
    assert len(code) == 10 and not set(code) & set("0O1IL") and body["expires_at"]
    first = client.post("/agent/enroll", json={"enroll_code": code.lower(), "device_name": "PC"})
    assert first.status_code == 200
    assert client.post("/agent/enroll", json={"enroll_code": code, "device_name": "PC2"}).status_code == 409
    assert client.post("/agent/enroll", json={"enroll_code": "NAOEXISTE1", "device_name": "PC"}).status_code == 404


def test_expired_enroll_code_is_refused(client, engine):
    code = client.post("/agent/enroll-codes").json()["code"]
    with Session(engine) as s:
        entry = s.exec(select(models.AgentEnrollCode)).one()
        entry.expires_at = models.utcnow() - timedelta(minutes=1)
        s.add(entry)
        s.commit()
    assert client.post("/agent/enroll", json={"enroll_code": code, "device_name": "PC"}).status_code == 410


def test_device_version_is_recorded(client):
    _enroll(client)
    devices = client.get("/agent/devices").json()
    assert devices[0]["agent_version"] == AGENT_VERSION


# ---------------------------------------------------------------- compatibilidade


def test_old_agent_receives_exactly_the_six_legacy_fields(client, engine):
    headers = _enroll(client, version=None)  # agente 0.1/0.2: não manda versão
    bridge._enqueue(engine, ttl_seconds=30, op="read_file", root="data", path="x", args={"offset": 0, "length": 5})
    tasks = client.get("/agent/tasks", headers=headers).json()["tasks"]
    assert set(tasks[0]) == {"id", "op", "root", "path", "content_base64", "glob"}


def test_new_agent_receives_args(client, engine):
    headers = _enroll(client)
    bridge._enqueue(engine, ttl_seconds=30, op="read_file", root="data", path="x", args={"offset": 3, "length": 5})
    assert client.get("/agent/tasks", headers=headers).json()["tasks"][0]["args"] == {"offset": 3, "length": 5}


# ---------------------------------------------------------------- ponta a ponta


def test_write_then_read_round_trip(client, engine, server_cfg):
    headers = _enroll(client)
    with Session(engine) as s:
        _run_with_agent(client, headers, server_cfg, lambda: bridge.write_text(s, "data", "TAB.txt", "olá\r\n", timeout=5))
        assert _run_with_agent(client, headers, server_cfg, lambda: bridge.read_text(s, "data", "TAB.txt", timeout=5)) == "olá\r\n"


def test_read_in_chunks_reassembles_a_big_file(client, engine, server_cfg):
    headers = _enroll(client)
    content = bytes(range(256)) * 40  # 10 KB
    (server_cfg.root("drive") / "nota.pdf").write_bytes(content)
    with Session(engine) as s:
        got = _run_with_agent(
            client, headers, server_cfg, lambda: bridge.read_bytes(s, "drive", "nota.pdf", timeout=5, chunk_size=1024)
        )
    assert got == content
    with Session(engine) as s:
        assert len(s.exec(select(models.AgentTask)).all()) == 10  # um pedaço por tarefa


def test_chunked_read_refused_for_an_old_agent(client, engine):
    _enroll(client, version=None)
    with Session(engine) as s, pytest.raises(bridge.AgentTooOldError, match="0.3.0"):
        bridge.read_bytes(s, "drive", "nota.pdf", timeout=1, chunk_size=1024)


def test_move_respects_folder_permissions(client, engine, server_cfg):
    headers = _enroll(client)
    (server_cfg.root("raw") / "a_1.B00").write_text("x")
    (server_cfg.root("drive") / "doc.pdf").write_text("x")
    with Session(engine) as s:
        moved = _run_with_agent(client, headers, server_cfg, lambda: bridge.move_files(
            s, "raw", [{"from": "a_1.B00", "to": "a_x_1.B00"}], timeout=5,
        ))
        assert moved == ["a_x_1.B00"]
        with pytest.raises(bridge.AgentTaskError, match="sem permissão"):
            _run_with_agent(client, headers, server_cfg, lambda: bridge.move_files(
                s, "drive", [{"from": "doc.pdf", "to": "outro.pdf"}], timeout=5,
            ))
        with pytest.raises(bridge.AgentTaskError, match="sem permissão para gravar"):
            _run_with_agent(client, headers, server_cfg, lambda: bridge.write_text(s, "raw", "z.txt", "x", timeout=5))


def test_missing_file_is_recognizable(client, engine, server_cfg):
    headers = _enroll(client)
    with Session(engine) as s, pytest.raises(bridge.AgentTaskError) as info:
        _run_with_agent(client, headers, server_cfg, lambda: bridge.read_text(s, "data", "nao.txt", timeout=5))
    assert bridge.file_not_found(info.value)


# ---------------------------------------------------------------- prazos


def test_timeout_expires_the_task_and_it_is_never_delivered(client, engine):
    headers = _enroll(client)
    with Session(engine) as s, pytest.raises(bridge.AgentTimeoutError, match="Nada foi gravado"):
        bridge.write_text(s, "data", "TAB.txt", "antigo", timeout=0.1)
    assert client.get("/agent/tasks", headers=headers).json()["tasks"] == []


def test_fail_fast_when_no_agent_is_online(engine):
    settings.fail_fast_when_offline = True
    with Session(engine) as s, pytest.raises(bridge.AgentOfflineError):
        bridge.read_text(s, "data", "x", timeout=5)
    with Session(engine) as s:
        assert s.exec(select(models.AgentTask)).all() == []  # nada foi enfileirado


def test_timeout_provider_wins(engine):
    settings.timeout_provider = lambda: 0.05
    with Session(engine) as s, pytest.raises(bridge.AgentTimeoutError, match="0.05s"):
        bridge.read_text(s, "data", "x")


def test_migrations_list_matches_the_models():
    for table, column, _ in models.MIGRATIONS:
        assert column in SQLModel.metadata.tables[table].columns
