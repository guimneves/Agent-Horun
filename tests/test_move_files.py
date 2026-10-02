"""move_files: renomear/mover um grupo de arquivos (ex. .B00 + .B00~ de uma
análise do Rock-Eval), tudo ou nada, nunca sobrescrevendo."""

from __future__ import annotations

import base64
import json
import os

import pytest

from agent.client import Task
from agent.runner import execute_task
from agent.tasks import TaskError, move_files
from tests.test_tasks import config  # noqa: F401 — fixture


def _write(root, rel, content=b"x"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)
    return p


def _moves(old, new, *, with_backup=True):
    m = [{"from": f"{old}.B00", "to": f"{new}.B00"}]
    if with_backup:
        m.append({"from": f"{old}.B00~", "to": f"{new}.B00~", "optional": True})
    return m


def test_renames_b00_and_backup_together_without_touching_content(config):
    jobs = config.root("jobs")
    _write(jobs, "J1/BULK ROCK/a_1.B00", b"novo")
    _write(jobs, "J1/BULK ROCK/a_1.B00~", b"anterior")
    result = move_files(config, "jobs", _moves("J1/BULK ROCK/a_1", "J1/BULK ROCK/a_X_1"))
    assert result.paths == ["J1/BULK ROCK/a_X_1.B00", "J1/BULK ROCK/a_X_1.B00~"]
    assert (jobs / "J1/BULK ROCK/a_X_1.B00").read_bytes() == b"novo"
    assert (jobs / "J1/BULK ROCK/a_X_1.B00~").read_bytes() == b"anterior"
    assert not (jobs / "J1/BULK ROCK/a_1.B00").exists()


def test_moves_to_another_job_creating_the_method_folder(config):
    jobs = config.root("jobs")
    _write(jobs, "J1/BULK ROCK/a_1.B00")
    move_files(config, "jobs", _moves("J1/BULK ROCK/a_1", "J2/BULK ROCK/a_1"))
    assert (jobs / "J2/BULK ROCK/a_1.B00").is_file()


def test_missing_optional_backup_is_skipped(config):
    jobs = config.root("jobs")
    _write(jobs, "J1/BULK ROCK/a_1.B00")
    result = move_files(config, "jobs", _moves("J1/BULK ROCK/a_1", "J1/BULK ROCK/b_1"))
    assert result.paths == ["J1/BULK ROCK/b_1.B00"]


def test_never_overwrites_and_moves_nothing_if_any_target_exists(config):
    jobs = config.root("jobs")
    _write(jobs, "J1/BULK ROCK/a_1.B00", b"a")
    _write(jobs, "J1/BULK ROCK/a_1.B00~", b"a~")
    _write(jobs, "J1/BULK ROCK/b_1.B00~", b"outro")  # só o backup do destino já existe
    with pytest.raises(TaskError, match="já existe"):
        move_files(config, "jobs", _moves("J1/BULK ROCK/a_1", "J1/BULK ROCK/b_1"))
    assert (jobs / "J1/BULK ROCK/a_1.B00").read_bytes() == b"a"  # nada foi movido
    assert (jobs / "J1/BULK ROCK/b_1.B00~").read_bytes() == b"outro"


def test_failure_in_the_middle_rolls_back_the_previous_moves(config, monkeypatch):
    jobs = config.root("jobs")
    _write(jobs, "J1/BULK ROCK/a_1.B00")
    _write(jobs, "J1/BULK ROCK/a_1.B00~")
    real_rename = os.rename
    calls = {"n": 0}

    def flaky_rename(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:  # o segundo arquivo está "aberto no GeoWorks"
            raise PermissionError("arquivo em uso")
        return real_rename(src, dst)

    monkeypatch.setattr(os, "rename", flaky_rename)
    with pytest.raises(TaskError, match="arquivo aberto"):
        move_files(config, "jobs", _moves("J1/BULK ROCK/a_1", "J1/BULK ROCK/b_1"))
    monkeypatch.setattr(os, "rename", real_rename)
    assert (jobs / "J1/BULK ROCK/a_1.B00").is_file() and (jobs / "J1/BULK ROCK/a_1.B00~").is_file()
    assert not (jobs / "J1/BULK ROCK/b_1.B00").exists()


@pytest.mark.parametrize("bad", ["../fora.B00", "/abs.B00"])
def test_paths_stay_inside_the_root(config, bad):
    _write(config.root("jobs"), "J1/a.B00")
    with pytest.raises(TaskError):
        move_files(config, "jobs", [{"from": "J1/a.B00", "to": bad}])


def test_missing_required_source_is_an_error(config):
    with pytest.raises(TaskError, match="não encontrado"):
        move_files(config, "jobs", _moves("J1/x", "J1/y"))


def test_runner_decodes_moves_from_content_base64(config):
    jobs = config.root("jobs")
    _write(jobs, "J1/a_1.B00")
    payload = base64.b64encode(json.dumps({"moves": [{"from": "J1/a_1.B00", "to": "J1/b_1.B00"}]}).encode()).decode()
    result = execute_task(config, Task(id="t1", op="move_files", root="jobs", content_base64=payload))
    assert result == {"ok": True, "paths": ["J1/b_1.B00"]}


def test_runner_reports_bad_payload_instead_of_crashing(config):
    result = execute_task(config, Task(id="t1", op="move_files", root="jobs", content_base64="nao-e-base64!"))
    assert result["ok"] is False


def test_task_ignores_unknown_fields_from_a_newer_server():
    # antes: Task(**t) com um campo extra derrubava o agente (TypeError)
    task = Task.from_dict({"id": "1", "op": "read_file", "root": "data", "path": "x", "campo_novo": 1})
    assert task.op == "read_file" and task.content_base64 is None
