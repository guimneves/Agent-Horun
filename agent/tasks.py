"""As três operações que o agente sabe fazer — ler, escrever (atômico) e
listar arquivos — sempre confinadas a um `root` permitido pela
configuração local (ver PROTOCOL.md). Nenhuma lógica de negócio aqui:
quem entende o conteúdo dos arquivos é sempre o backend do módulo, do
lado do servidor.

Path-traversal-safe: mesmo mecanismo já usado no RE7S
(`routes_postrun.py`/`routes_weighing.py`, `_resolve_token`) — o caminho
pedido é resolvido e precisa continuar dentro do root, senão é rejeitado.
Escrita por substituição atômica: mesmo idiom de
`app/modules/tabsample.py` do RE7S (arquivo temporário no mesmo
diretório + `os.replace`), pra nunca deixar o backend do equipamento ler
um arquivo pela metade."""

from __future__ import annotations

import base64
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from agent.config import AgentConfig, ConfigError


class TaskError(ValueError):
    """Erro esperado de uma tarefa (arquivo não encontrado, caminho fora
    do root, root desconhecido) — vira `{"ok": false, "error": ...}` no
    resultado reportado ao servidor, nunca derruba o agente."""


@dataclass
class ReadFileResult:
    content_base64: str


@dataclass
class ListFilesResult:
    paths: list[str]  # relativos ao root, sempre em posix (/ nunca \)


def _resolve_safe(config: AgentConfig, root_name: str, relative_path: str) -> Path:
    try:
        root = config.root(root_name)
    except ConfigError as exc:
        raise TaskError(str(exc)) from exc

    # mesmo cuidado do `_resolve_token` do RE7S: resolve e confirma que o
    # resultado continua dentro do root, antes de tocar em disco.
    candidate = (root / relative_path).resolve()
    if root != candidate and root not in candidate.parents:
        raise TaskError(f"caminho fora do root permitido: {relative_path!r}")
    return candidate


def read_file(config: AgentConfig, root_name: str, relative_path: str) -> ReadFileResult:
    path = _resolve_safe(config, root_name, relative_path)
    if not path.is_file():
        raise TaskError(f"arquivo não encontrado: {relative_path!r}")
    raw = path.read_bytes()
    return ReadFileResult(content_base64=base64.b64encode(raw).decode("ascii"))


def write_file(config: AgentConfig, root_name: str, relative_path: str, content_base64: str) -> None:
    path = _resolve_safe(config, root_name, relative_path)
    try:
        raw = base64.b64decode(content_base64, validate=True)
    except (base64.binascii.Error, ValueError) as exc:
        raise TaskError(f"conteúdo base64 inválido: {exc}") from exc

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".horun_agent_", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        os.replace(tmp_path, path)
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def list_files(config: AgentConfig, root_name: str, glob: str) -> ListFilesResult:
    try:
        root = config.root(root_name)
    except ConfigError as exc:
        raise TaskError(str(exc)) from exc
    if not root.is_dir():
        raise TaskError(f"root {root_name!r} não é uma pasta existente: {root}")

    matches = sorted(p for p in root.glob(glob) if p.is_file())
    return ListFilesResult(paths=[m.relative_to(root).as_posix() for m in matches])
