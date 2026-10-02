"""As três primitivas (ler/escrever/listar) + a lista de permissões —
tudo local, sem precisar de servidor nenhum rodando."""

from __future__ import annotations

import base64

import pytest

from agent.config import RootConfig, ServerConfig
from agent.tasks import TaskError, list_files, read_file, write_file


@pytest.fixture()
def config(tmp_path):
    # um servidor (módulo) com duas pastas de permissão total — o padrão de
    # quem usa o formato antigo do config.json
    data_dir = tmp_path / "data"
    jobs_dir = tmp_path / "jobs"
    data_dir.mkdir()
    jobs_dir.mkdir()
    return ServerConfig(
        url="https://example.invalid/m/re7s",
        roots={"data": RootConfig(data_dir.resolve()), "jobs": RootConfig(jobs_dir.resolve())},
        device_token="tok",
    )


def test_write_then_read_round_trip(config):
    content = "AAAA-MM-DD\tXPTO\t1\r\n"
    write_file(config, "data", "TABSAMPLE.txt", base64.b64encode(content.encode("utf-8")).decode())

    result = read_file(config, "data", "TABSAMPLE.txt")
    assert base64.b64decode(result.content_base64).decode("utf-8") == content


def test_write_creates_intermediate_folders(config):
    write_file(config, "jobs", "IFP160000/BULK ROCK/a.B00", base64.b64encode(b"conteudo").decode())
    result = read_file(config, "jobs", "IFP160000/BULK ROCK/a.B00")
    assert base64.b64decode(result.content_base64) == b"conteudo"


def test_read_missing_file_raises_task_error(config):
    with pytest.raises(TaskError, match="não encontrado"):
        read_file(config, "data", "nope.txt")


def test_unknown_root_raises_task_error(config):
    with pytest.raises(TaskError, match="root desconhecido"):
        read_file(config, "nao-existe", "x.txt")


def test_path_traversal_is_rejected(config):
    # tenta escapar do root "data" pra fora, via "..". A validação central
    # é justamente essa — mesmo mecanismo do _resolve_token do RE7S.
    with pytest.raises(TaskError, match="fora do root"):
        read_file(config, "data", "../jobs/segredo.txt")


def test_write_invalid_base64_raises_task_error(config):
    with pytest.raises(TaskError, match="base64"):
        write_file(config, "data", "x.txt", "isto não é base64 válido!!")


def test_write_is_atomic_no_partial_file_left_on_success(config):
    write_file(config, "data", "TABSAMPLE.txt", base64.b64encode(b"conteudo final").decode())
    leftovers = list((config.root("data")).glob(".horun_agent_*"))
    assert leftovers == []


def test_list_files_filters_by_glob_and_is_relative_posix(config):
    jobs = config.root("jobs")
    (jobs / "IFP160000" / "BULK ROCK").mkdir(parents=True)
    (jobs / "IFP160000" / "BULK ROCK" / "a.B00").write_text("x")
    (jobs / "IFP160000" / "BULK ROCK" / "a.B00~").write_text("backup, deve ser ignorado pelo glob abaixo")
    (jobs / "IFP160000" / "BULK ROCK" / "readme.txt").write_text("nao é .B00")

    result = list_files(config, "jobs", "**/*.B00")
    assert result.paths == ["IFP160000/BULK ROCK/a.B00"]


def test_list_files_unknown_root_raises_task_error(config):
    with pytest.raises(TaskError, match="root desconhecido"):
        list_files(config, "nao-existe", "*")
