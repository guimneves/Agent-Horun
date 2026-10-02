"""0.3.0: vários servidores (módulos) por instalação, permissão por pasta,
leitura em pedaços, versão do agente no cabeçalho e isolamento entre
servidores."""

from __future__ import annotations

import base64
import json

import pytest

from agent import __version__
from agent.client import VERSION_HEADER, ClientError, HorunAgentClient, Task
from agent.config import ConfigError, RootConfig, ServerConfig, load_config
from agent.runner import execute_task, serve_once
from agent.tasks import TaskError, list_files, move_files, read_file, write_file


def _cfg(tmp_path, data):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


# ---------- config ----------


def test_multi_server_config_with_modes(tmp_path):
    (tmp_path / "raw").mkdir()
    (tmp_path / "drive").mkdir()
    path = _cfg(tmp_path, {
        "device_name": "PC-1",
        "servers": [
            {"url": "https://h/m/re7s/", "device_token": "t1",
             "roots": {"jobs": {"path": str(tmp_path / "raw"), "mode": "read-move"}, "data": str(tmp_path / "raw")}},
            {"url": "https://h/m/financeiro", "enroll_code": "ABC",
             "roots": {"drive": {"path": str(tmp_path / "drive"), "mode": "read"}}},
        ],
    })
    config = load_config(path)
    re7s, fin = config.servers
    assert re7s.url == "https://h/m/re7s"  # barra final removida
    assert re7s.root_mode("jobs") == "read-move" and re7s.root_mode("data") == "read-write"
    assert fin.root_mode("drive") == "read" and not fin.enrolled


def test_invalid_mode_and_duplicate_server_are_rejected(tmp_path):
    with pytest.raises(ConfigError, match="mode inválido"):
        load_config(_cfg(tmp_path, {"server_url": "https://h", "roots": {"x": {"path": "C:/x", "mode": "tudo"}}}))
    with pytest.raises(ConfigError, match="duas vezes"):
        load_config(_cfg(tmp_path, {"servers": [
            {"url": "https://h/m/a", "roots": {"x": "C:/x"}},
            {"url": "https://h/m/a/", "roots": {"x": "C:/y"}},
        ]}))


def test_legacy_file_is_saved_back_in_the_legacy_format(tmp_path):
    path = _cfg(tmp_path, {"server_url": "https://h/m/re7s", "enroll_code": "ABC", "roots": {"data": str(tmp_path)}})
    config = load_config(path)
    config.servers[0].device_token = "novo"
    config.save()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "servers" not in saved and saved["server_url"] == "https://h/m/re7s"
    assert saved["device_token"] == "novo"
    assert saved["enroll_code"] == ""  # código de uso único some depois do enrolamento
    assert saved["roots"]["data"] == str(tmp_path.resolve())  # read-write continua como texto simples


def test_multi_server_file_keeps_modes_on_save(tmp_path):
    path = _cfg(tmp_path, {"servers": [{"url": "https://h/m/a", "roots": {"d": {"path": str(tmp_path), "mode": "read"}}}]})
    config = load_config(path)
    config.save()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["servers"][0]["roots"]["d"] == {"path": str(tmp_path.resolve()), "mode": "read"}


# ---------- permissões por pasta ----------


@pytest.fixture()
def server(tmp_path):
    for name in ("ro", "mv", "rw"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "a.txt").write_bytes(b"0123456789")
    return ServerConfig(url="https://h", roots={
        "ro": RootConfig((tmp_path / "ro").resolve(), "read"),
        "mv": RootConfig((tmp_path / "mv").resolve(), "read-move"),
        "rw": RootConfig((tmp_path / "rw").resolve(), "read-write"),
    })


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def test_read_only_folder_reads_and_lists_but_never_writes_or_moves(server):
    assert read_file(server, "ro", "a.txt").size == 10
    assert list_files(server, "ro", "*.txt").paths == ["a.txt"]
    with pytest.raises(TaskError, match="sem permissão para gravar"):
        write_file(server, "ro", "b.txt", _b64(b"x"))
    with pytest.raises(TaskError, match="sem permissão para mover"):
        move_files(server, "ro", [{"from": "a.txt", "to": "b.txt"}])
    assert (server.root("ro") / "a.txt").read_bytes() == b"0123456789"


def test_read_move_folder_moves_but_never_writes(server):
    move_files(server, "mv", [{"from": "a.txt", "to": "b.txt"}])
    assert (server.root("mv") / "b.txt").is_file()
    with pytest.raises(TaskError, match="sem permissão para gravar"):
        write_file(server, "mv", "c.txt", _b64(b"x"))


def test_read_write_folder_does_everything(server):
    write_file(server, "rw", "c.txt", _b64(b"x"))
    move_files(server, "rw", [{"from": "c.txt", "to": "d.txt"}])
    assert (server.root("rw") / "d.txt").read_bytes() == b"x"


# ---------- leitura em pedaços ----------


def test_read_range_returns_a_slice_and_the_total_size(server):
    result = read_file(server, "ro", "a.txt", offset=3, length=4)
    assert base64.b64decode(result.content_base64) == b"3456" and result.size == 10


def test_runner_passes_range_args(server):
    out = execute_task(server, Task(id="1", op="read_file", root="ro", path="a.txt", args={"offset": 8, "length": 5}))
    assert out["ok"] and base64.b64decode(out["content_base64"]) == b"89" and out["size"] == 10


@pytest.mark.parametrize("bad", ["../*", "/etc/*", "C:/*"])
def test_list_rejects_escaping_patterns(server, bad):
    with pytest.raises(TaskError, match="padrão"):
        list_files(server, "ro", bad)


# ---------- cliente ----------


def test_every_call_sends_the_agent_version(monkeypatch):
    seen = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"tasks": [], "device_token": "t"}

    def fake(method):
        def call(url, **kwargs):
            seen.append(kwargs["headers"])
            return FakeResponse()
        return call

    import agent.client as client_module

    monkeypatch.setattr(client_module.httpx, "get", fake("get"))
    monkeypatch.setattr(client_module.httpx, "post", fake("post"))
    c = HorunAgentClient("https://h")
    c.enroll("ABC", "pc")
    c.poll_tasks("tok")
    c.report_result("tok", "1", {"ok": True})
    assert all(h[VERSION_HEADER] == __version__ for h in seen)
    assert seen[1]["Authorization"] == "Bearer tok"


def test_task_args_are_read_and_unknown_fields_ignored():
    t = Task.from_dict({"id": "1", "op": "read_file", "root": "r", "args": {"offset": 2}, "futuro": True})
    assert t.args == {"offset": 2}


def test_one_failing_server_does_not_stop_the_others(tmp_path):
    class Boom:
        def poll_tasks(self, _token):
            raise ClientError("fora do ar")

    calls = []

    class Ok:
        def poll_tasks(self, token):
            calls.append(token)
            return []

    from agent.config import AgentConfig

    a = ServerConfig(url="https://a", roots={"x": RootConfig(tmp_path)}, device_token="ta")
    b = ServerConfig(url="https://b", roots={"x": RootConfig(tmp_path)}, device_token="tb")
    config = AgentConfig(path=tmp_path / "c.json", device_name="pc", poll_interval_seconds=1, servers=[a, b])
    serve_once(config, a, Boom())  # não levanta
    serve_once(config, b, Ok())
    assert calls == ["tb"]
