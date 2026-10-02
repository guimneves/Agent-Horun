"""As operações que o agente sabe fazer — ler, escrever (atômico), listar e
mover/renomear (em grupo, tudo ou nada) arquivos — sempre confinadas a um `root` permitido pela
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


@dataclass
class MoveFilesResult:
    paths: list[str]  # destinos efetivamente movidos, relativos ao root (posix)


def move_files(config: AgentConfig, root_name: str, moves: list[dict]) -> MoveFilesResult:
    """Move/renomeia um GRUPO de arquivos dentro do mesmo root, tudo ou nada.

    Usado para renomear uma análise do Rock-Eval: o `.B00` e o `.B00~` (cópia
    anterior guardada pelo GeoWorks) andam sempre juntos. Cada item é
    `{"from": "...", "to": "...", "optional": bool}` — `optional` = pode não
    existir (ex. análise sem `.B00~`), e aí é simplesmente pulado.

    Garantias:
    - nunca sobrescreve: se qualquer destino já existir, nada é movido;
    - tudo ou nada: se um movimento falhar no meio (ex. arquivo aberto no
      GeoWorks), os anteriores são desfeitos;
    - origem e destino confinados ao root (mesma checagem de `_resolve_safe`);
    - o conteúdo dos arquivos nunca é lido nem alterado — só o nome/pasta.
    """
    if not isinstance(moves, list) or not moves:
        raise TaskError("nenhum arquivo para mover")

    planned: list[tuple[Path, Path, str]] = []
    seen_targets: set[Path] = set()
    for item in moves:
        if not isinstance(item, dict) or not item.get("from") or not item.get("to"):
            raise TaskError(f"movimento inválido: {item!r}")
        src = _resolve_safe(config, root_name, item["from"])
        dst = _resolve_safe(config, root_name, item["to"])
        if src == dst:
            raise TaskError(f"origem e destino iguais: {item['from']!r}")
        if not src.is_file():
            if item.get("optional"):
                continue
            raise TaskError(f"arquivo não encontrado: {item['from']!r}")
        if dst.exists() or dst in seen_targets:
            raise TaskError(f"já existe um arquivo com esse nome: {item['to']!r}")
        seen_targets.add(dst)
        planned.append((src, dst, item["to"]))

    if not planned:
        raise TaskError("nenhum dos arquivos de origem existe")

    done: list[tuple[Path, Path]] = []
    try:
        for src, dst, _ in planned:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():  # conferido de novo logo antes — nunca sobrescrever
                raise TaskError(f"já existe um arquivo com esse nome: {dst.name!r}")
            os.rename(src, dst)
            done.append((src, dst))
    except BaseException as exc:
        for src, dst in reversed(done):
            try:
                os.rename(dst, src)
            except OSError:
                pass  # melhor esforço; o erro original é o que importa reportar
        if isinstance(exc, TaskError):
            raise
        if isinstance(exc, OSError):
            raise TaskError(f"não foi possível mover (arquivo aberto em outro programa?): {exc}") from exc
        raise

    root = config.root(root_name)
    return MoveFilesResult(paths=[dst.relative_to(root).as_posix() for _, dst, _ in planned])
