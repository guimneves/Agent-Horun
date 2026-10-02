"""Configuração de uma instalação do agente — um `config.json` por
computador de equipamento (nunca commitado; ver `config.example.json`).

O que muda de equipamento pra equipamento (RE7S, LECO, Financeiro...) é só
este arquivo — para quais servidores (módulos) o agente trabalha, o token de
cada um e quais pastas ele pode tocar em cada um (`roots`), com que
permissão. O código do agente é sempre o mesmo.

Dois formatos aceitos:

1. **Atual (um módulo)** — o de antes da 0.3.0, continua valendo igual:
   `{"server_url", "device_token", "enroll_code", "roots": {"data": "C:\\..."}}`.
   Pastas escritas assim têm permissão total ("read-write"), como sempre.

2. **Vários módulos** — `{"servers": [{"url", "device_token", "enroll_code",
   "roots": {...}}, ...]}`: um mesmo PC atendendo mais de um módulo, cada um
   com o seu token e as suas pastas.

Em qualquer formato, cada pasta pode ser só o caminho (texto) ou
`{"path": "C:\\...", "mode": "read" | "read-move" | "read-write"}`:

- `read`: só ler e listar (ex. o drive do Financeiro, a balança);
- `read-move`: ler, listar e renomear/mover — nunca alterar conteúdo (ex.
  RE7raw-data do Rock-Eval);
- `read-write`: tudo (ex. a pasta do TABSAMPLE.txt).

`{"path": ..., "read_only": true}` é sinônimo de `"mode": "read"` (formato
do contrato original do Financeiro). `max_read_bytes` (raiz do arquivo,
padrão 8 MiB) limita quanto o agente devolve numa única leitura — o servidor
lê arquivos maiores em pedaços.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(os.environ.get("HORUN_AGENT_CONFIG", "config.json"))

MODES = ("read", "read-move", "read-write")
DEFAULT_MAX_READ_BYTES = 8 * 1024 * 1024


class ConfigError(ValueError):
    pass


@dataclass
class RootConfig:
    path: Path
    mode: str = "read-write"


@dataclass
class ServerConfig:
    """Um módulo atendido por esta instalação."""

    url: str
    roots: dict[str, RootConfig] = field(default_factory=dict)
    device_token: str = ""
    enroll_code: str = ""
    max_read_bytes: int = DEFAULT_MAX_READ_BYTES

    @property
    def enrolled(self) -> bool:
        return bool(self.device_token)

    def root(self, name: str) -> Path:
        """Pasta real (absoluta, resolvida) por trás de um `root` lógico
        (ex. "data", "jobs") — ver PROTOCOL.md. `ConfigError` se o servidor
        pedir um root que esta instalação não tem configurado."""
        try:
            return self.roots[name].path
        except KeyError:
            raise ConfigError(f"root desconhecido nesta instalação: {name!r}") from None

    def root_mode(self, name: str) -> str:
        self.root(name)  # mesmo erro para root desconhecido
        return self.roots[name].mode


@dataclass
class AgentConfig:
    path: Path  # onde este config.json vive — necessário pra `save()`
    device_name: str
    poll_interval_seconds: float
    servers: list[ServerConfig]
    # lido no formato antigo (um servidor só)? `save()` regrava no mesmo
    # formato, para não surpreender quem edita o arquivo à mão
    single_server_format: bool = False
    max_read_bytes: int = DEFAULT_MAX_READ_BYTES

    def save(self) -> None:
        """Persiste o `device_token` recém-obtido no enrolamento, pra não
        precisar enrolar de novo no próximo start do serviço. Depois do
        enrolamento, o `enroll_code` (de uso único) é apagado do arquivo."""

        def roots_out(server: ServerConfig) -> dict:
            out = {}
            for name, root in server.roots.items():
                out[name] = str(root.path) if root.mode == "read-write" else {"path": str(root.path), "mode": root.mode}
            return out

        if self.single_server_format and len(self.servers) == 1:
            s = self.servers[0]
            data = {
                "server_url": s.url,
                "device_name": self.device_name,
                "enroll_code": "" if s.enrolled else s.enroll_code,
                "device_token": s.device_token,
                "poll_interval_seconds": self.poll_interval_seconds,
                "roots": roots_out(s),
            }
            if self.max_read_bytes != DEFAULT_MAX_READ_BYTES:
                data["max_read_bytes"] = self.max_read_bytes
        else:
            data = {
                "device_name": self.device_name,
                "poll_interval_seconds": self.poll_interval_seconds,
                "servers": [
                    {
                        "url": s.url,
                        "enroll_code": "" if s.enrolled else s.enroll_code,
                        "device_token": s.device_token,
                        "roots": roots_out(s),
                    }
                    for s in self.servers
                ],
            }
            if self.max_read_bytes != DEFAULT_MAX_READ_BYTES:
                data["max_read_bytes"] = self.max_read_bytes
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _parse_roots(raw: object, where: str) -> dict[str, RootConfig]:
    if not isinstance(raw, dict) or not raw:
        raise ConfigError(f"{where}: 'roots' precisa ter pelo menos uma pasta configurada")
    roots: dict[str, RootConfig] = {}
    for name, value in raw.items():
        if isinstance(value, str):
            roots[name] = RootConfig(path=Path(value).resolve())
            continue
        if not isinstance(value, dict) or not value.get("path"):
            raise ConfigError(f"{where}: pasta {name!r} precisa de 'path'")
        mode = value.get("mode") or ("read" if value.get("read_only") else "read-write")
        if value.get("read_only") and mode != "read":
            raise ConfigError(f"{where}: pasta {name!r} com read_only=true e mode {mode!r} — use só um dos dois")
        if mode not in MODES:
            raise ConfigError(f"{where}: pasta {name!r} com mode inválido {mode!r} (use {', '.join(MODES)})")
        roots[name] = RootConfig(path=Path(value["path"]).resolve(), mode=mode)
    return roots


def _parse_server(raw: dict, where: str, url_key: str) -> ServerConfig:
    url = str(raw.get(url_key, "")).rstrip("/")
    if not url:
        raise ConfigError(f"{where}: '{url_key}' é obrigatório")
    return ServerConfig(
        url=url,
        roots=_parse_roots(raw.get("roots", {}), where),
        device_token=raw.get("device_token", ""),
        enroll_code=raw.get("enroll_code", ""),
    )


def load_config(path: Path = DEFAULT_CONFIG_PATH) -> AgentConfig:
    if not path.exists():
        raise ConfigError(
            f"{path} não encontrado — copie config.example.json pra {path.name} "
            "e preencha os servidores/pastas antes de rodar o agente."
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} não é um JSON válido: {exc}") from exc

    if "servers" in data:
        raw_servers = data["servers"]
        if not isinstance(raw_servers, list) or not raw_servers:
            raise ConfigError(f"{path}: 'servers' precisa ter pelo menos um servidor")
        servers = [_parse_server(s, f"{path} (servidor {i + 1})", "url") for i, s in enumerate(raw_servers)]
        urls = [s.url for s in servers]
        if len(set(urls)) != len(urls):
            raise ConfigError(f"{path}: o mesmo servidor aparece duas vezes em 'servers'")
        single = False
    else:
        servers = [_parse_server(data, str(path), "server_url")]
        single = True

    try:
        max_read = int(data.get("max_read_bytes", DEFAULT_MAX_READ_BYTES))
    except (TypeError, ValueError):
        raise ConfigError(f"{path}: 'max_read_bytes' precisa ser um número inteiro") from None
    if max_read <= 0:
        raise ConfigError(f"{path}: 'max_read_bytes' precisa ser positivo")
    for server in servers:
        server.max_read_bytes = max_read

    return AgentConfig(
        path=path,
        device_name=data.get("device_name", ""),
        poll_interval_seconds=float(data.get("poll_interval_seconds", 3)),
        servers=servers,
        single_server_format=single,
        max_read_bytes=max_read,
    )
