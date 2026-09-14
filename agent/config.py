"""Configuração de uma instalação do agente — um `config.json` por
computador de equipamento (nunca commitado; ver `config.example.json`).

O que muda de equipamento pra equipamento (RE7S, LECO, futuros) é só
este arquivo — servidor, token e quais pastas o agente tem permissão de
tocar (`roots`). O código do agente é sempre o mesmo (ver README.md)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(os.environ.get("HORUN_AGENT_CONFIG", "config.json"))


class ConfigError(ValueError):
    pass


@dataclass
class AgentConfig:
    path: Path  # onde este config.json vive — necessário pra `save()`
    server_url: str
    device_name: str
    enroll_code: str
    device_token: str
    poll_interval_seconds: float
    roots: dict[str, Path] = field(default_factory=dict)

    @property
    def enrolled(self) -> bool:
        return bool(self.device_token)

    def root(self, name: str) -> Path:
        """Pasta real (absoluta, resolvida) por trás de um `root` lógico
        (ex. "data", "jobs") — ver PROTOCOL.md. `ConfigError` se o
        servidor pedir um root que esta instalação não tem configurado."""
        try:
            return self.roots[name]
        except KeyError:
            raise ConfigError(f"root desconhecido nesta instalação: {name!r}") from None

    def save(self) -> None:
        """Persiste o `device_token` recém-obtido no enrolamento, pra não
        precisar enrolar de novo no próximo start do serviço."""
        data = {
            "server_url": self.server_url,
            "device_name": self.device_name,
            "enroll_code": self.enroll_code,
            "device_token": self.device_token,
            "poll_interval_seconds": self.poll_interval_seconds,
            "roots": {name: str(p) for name, p in self.roots.items()},
        }
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> AgentConfig:
    if not path.exists():
        raise ConfigError(
            f"{path} não encontrado — copie config.example.json pra {path.name} "
            "e preencha server_url/roots antes de rodar o agente."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} não é um JSON válido: {exc}") from exc

    server_url = data.get("server_url", "").rstrip("/")
    if not server_url:
        raise ConfigError(f"{path}: 'server_url' é obrigatório")

    roots_raw = data.get("roots", {})
    if not isinstance(roots_raw, dict) or not roots_raw:
        raise ConfigError(f"{path}: 'roots' precisa ter pelo menos uma pasta configurada")
    roots = {name: Path(value).resolve() for name, value in roots_raw.items()}

    return AgentConfig(
        path=path,
        server_url=server_url,
        device_name=data.get("device_name", ""),
        enroll_code=data.get("enroll_code", ""),
        device_token=data.get("device_token", ""),
        poll_interval_seconds=float(data.get("poll_interval_seconds", 3)),
        roots=roots,
    )
