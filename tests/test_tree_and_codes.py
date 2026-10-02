"""0.3.0 — o que faltava para o Financeiro usar o mesmo agente: list_tree,
código de erro, limite de leitura, `read_only: true` e caminhos longos do
Windows (o drive do Financeiro tem pastas acima de 260 caracteres)."""

from __future__ import annotations

import base64
import json
import os
import sys

import pytest

from agent.client import Task
from agent.config import ConfigError, RootConfig, ServerConfig, load_config
from agent.runner import execute_task
from agent.tasks import TaskError, _makedirs, _os_path, list_files, list_tree, read_file, write_file


@pytest.fixture()
def server(tmp_path):
    drive = tmp_path / "drive"
    (drive / "2026" / "Notas").mkdir(parents=True)
    (drive / "2026" / "Vazia").mkdir()
    (drive / "2026" / "Notas" / "nf1.pdf").write_bytes(b"x" * 10)
    (drive / "leia.txt").write_bytes(b"oi")
    return ServerConfig(
        url="https://example.invalid/m/financeiro",
        roots={"drive": RootConfig(drive.resolve(), mode="read")},
        device_token="tok",
    )


def test_list_tree_returns_folders_including_empty_ones_and_sizes(server):
    entries = {e.path: (e.is_dir, e.size) for e in list_tree(server, "drive", "")}
    assert entries == {
        "2026": (True, None),
        "2026/Notas": (True, None),
        "2026/Notas/nf1.pdf": (False, 10),
        "2026/Vazia": (True, None),
        "leia.txt": (False, 2),
    }


def test_list_tree_of_a_subfolder_keeps_paths_relative_to_the_root(server):
    paths = [e.path for e in list_tree(server, "drive", "2026", recursive=False)]
    assert paths == ["2026/Notas", "2026/Vazia"]


def test_list_tree_rejects_escaping_the_root(server):
    with pytest.raises(TaskError) as exc:
        list_tree(server, "drive", "../")
    assert exc.value.code == "outside_root"


def test_errors_carry_a_code_through_the_runner(server):
    def run(**kw):
        return execute_task(server, Task(id=1, **kw))

    assert run(op="read_file", root="drive", path="nao_existe.pdf")["code"] == "not_found"
    assert run(op="read_file", root="outro", path="a")["code"] == "unknown_root"
    assert run(op="read_file", root="drive", path="../segredo")["code"] == "outside_root"
    w = run(op="write_file", root="drive", path="a.txt", content_base64="YQ==")
    assert w["ok"] is False and w["code"] == "read_only"
    assert run(op="xyz", root="drive")["code"] == "unknown_op"


def test_list_tree_through_the_runner(server):
    result = execute_task(server, Task(id=1, op="list_tree", root="drive", path="2026", args={"recursive": False}))
    assert result == {
        "ok": True,
        "entries": [
            {"path": "2026/Notas", "is_dir": True, "size": None},
            {"path": "2026/Vazia", "is_dir": True, "size": None},
        ],
    }


def test_read_bigger_than_the_limit_is_refused_but_chunks_work(server):
    server.max_read_bytes = 4
    with pytest.raises(TaskError) as exc:
        read_file(server, "drive", "2026/Notas/nf1.pdf")
    assert exc.value.code == "too_large"
    chunk = read_file(server, "drive", "2026/Notas/nf1.pdf", offset=8, length=4)
    assert base64.b64decode(chunk.content_base64) == b"xx" and chunk.size == 10


def _write_config(tmp_path, roots, **extra):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"server_url": "https://x.invalid/m/fin", "roots": roots, **extra}), encoding="utf-8")
    return path


def test_read_only_true_is_an_alias_of_mode_read(tmp_path):
    cfg = load_config(_write_config(tmp_path, {"drive": {"path": str(tmp_path), "read_only": True}}))
    assert cfg.servers[0].root_mode("drive") == "read"


def test_read_only_conflicting_with_mode_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="read_only"):
        load_config(_write_config(tmp_path, {"drive": {"path": str(tmp_path), "read_only": True, "mode": "read-write"}}))


def test_max_read_bytes_comes_from_the_config(tmp_path):
    cfg = load_config(_write_config(tmp_path, {"d": str(tmp_path)}, max_read_bytes=1024))
    assert cfg.servers[0].max_read_bytes == 1024
    cfg.save()
    assert json.loads(cfg.path.read_text(encoding="utf-8"))["max_read_bytes"] == 1024


@pytest.mark.skipif(sys.platform != "win32", reason="limite de 260 caracteres é do Windows")
def test_paths_longer_than_260_characters_work(tmp_path):
    root = tmp_path / "drive"
    root.mkdir()
    server = ServerConfig(url="https://x.invalid", roots={"drive": RootConfig(root.resolve())}, device_token="t")
    deep = "/".join(["Pasta com nome comprido de verdade %02d" % i for i in range(8)])
    rel = deep + "/Nota fiscal.pdf"
    assert len(str(root / rel)) > 260

    write_file(server, "drive", rel, base64.b64encode(b"pdf").decode())
    assert base64.b64decode(read_file(server, "drive", rel).content_base64) == b"pdf"
    assert list_files(server, "drive", "**/*.pdf").paths == [rel]
    tree = [e.path for e in list_tree(server, "drive", "")]
    assert rel in tree and not any(p.startswith("\\\\?\\") or "\\" in p for p in tree)


def test_makedirs_creates_every_missing_level(tmp_path):
    target = os.path.join(str(tmp_path), "a", "b", "c")
    _makedirs(target)
    assert os.path.isdir(_os_path(target))
    _makedirs(target)  # já existe: nada a fazer
