"""Carregar/salvar o config.json de uma instalação."""

from __future__ import annotations

import json

import pytest

from agent.config import ConfigError, load_config


def _write(tmp_path, **overrides):
    data = {
        "server_url": "https://example.invalid/m/re7s",
        "device_name": "teste",
        "enroll_code": "",
        "device_token": "",
        "poll_interval_seconds": 3,
        "roots": {"data": str(tmp_path / "data")},
        **overrides,
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def test_load_config_reads_roots_as_resolved_paths(tmp_path):
    (tmp_path / "data").mkdir()
    path = _write(tmp_path)
    config = load_config(path)
    assert config.server_url == "https://example.invalid/m/re7s"
    assert config.root("data") == (tmp_path / "data").resolve()
    assert config.enrolled is False


def test_load_config_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError, match="não encontrado"):
        load_config(tmp_path / "nope.json")


def test_load_config_requires_server_url(tmp_path):
    path = _write(tmp_path, server_url="")
    with pytest.raises(ConfigError, match="server_url"):
        load_config(path)


def test_load_config_requires_at_least_one_root(tmp_path):
    path = _write(tmp_path, roots={})
    with pytest.raises(ConfigError, match="roots"):
        load_config(path)


def test_save_persists_device_token(tmp_path):
    (tmp_path / "data").mkdir()
    path = _write(tmp_path)
    config = load_config(path)
    config.device_token = "novo-token"
    config.save()

    reloaded = load_config(path)
    assert reloaded.device_token == "novo-token"
    assert reloaded.enrolled is True
