"""As operações que o agente sabe fazer — ler (inteiro ou um pedaço),
escrever (atômico), listar e mover/renomear (em grupo, tudo ou nada) —
sempre confinadas a um `root` permitido pela configuração local e
respeitando o modo de cada root (`read` / `read-move` / `read-write`, ver
config.py). Nenhuma lógica de negócio aqui: quem entende o conteúdo dos
arquivos é sempre o backend do módulo, do lado do servidor.

Path-traversal-safe: o caminho pedido é resolvido e precisa continuar
dentro do root, senão é rejeitado. Escrita por substituição atômica
(arquivo temporário no mesmo diretório + `os.replace`), pra nunca deixar o
software do equipamento ler um arquivo pela metade."""

from __future__ import annotations

import base64
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from agent.config import ConfigError, ServerConfig


class TaskError(ValueError):
    """Erro esperado de uma tarefa (arquivo não encontrado, caminho fora
    do root, root desconhecido, permissão) — vira `{"ok": false, "error":
    ...}` no resultado reportado ao servidor, nunca derruba o agente."""


@dataclass
class ReadFileResult:
    content_base64: str
    size: int  # tamanho TOTAL do arquivo (para leituras em pedaços)


@dataclass
class ListFilesResult:
    paths: list[str]  # relativos ao root, sempre em posix (/ nunca \)


@dataclass
class MoveFilesResult:
    paths: list[str]  # destinos efetivamente movidos, relativos ao root (posix)


_CAN_WRITE = {"read-write"}
_CAN_MOVE = {"read-write", "read-move"}


def _root(server: ServerConfig, root_name: str) -> Path:
    try:
        return server.root(root_name)
    except ConfigError as exc:
        raise TaskError(str(exc)) from exc


def _require_mode(server: ServerConfig, root_name: str, allowed: set[str], action: str) -> None:
    mode = server.root_mode(root_name)
    if mode not in allowed:
        raise TaskError(f"sem permissão para {action} em {root_name!r} (pasta configurada como {mode!r})")


def _resolve_safe(server: ServerConfig, root_name: str, relative_path: str) -> Path:
    root = _root(server, root_name)
    candidate = (root / relative_path).resolve()
    if root != candidate and root not in candidate.parents:
        raise TaskError(f"caminho fora do root permitido: {relative_path!r}")
    return candidate


def read_file(
    server: ServerConfig, root_name: str, relative_path: str, *, offset: int = 0, length: int | None = None
) -> ReadFileResult:
    """Lê o arquivo inteiro, ou `length` bytes a partir de `offset` (para
    arquivos grandes, que não cabem numa resposta só)."""
    path = _resolve_safe(server, root_name, relative_path)
    if not path.is_file():
        raise TaskError(f"arquivo não encontrado: {relative_path!r}")
    if offset < 0 or (length is not None and length < 0):
        raise TaskError("offset/length inválidos")
    size = path.stat().st_size
    with path.open("rb") as f:
        f.seek(offset)
        raw = f.read() if length is None else f.read(length)
    return ReadFileResult(content_base64=base64.b64encode(raw).decode("ascii"), size=size)


def write_file(server: ServerConfig, root_name: str, relative_path: str, content_base64: str) -> None:
    path = _resolve_safe(server, root_name, relative_path)
    _require_mode(server, root_name, _CAN_WRITE, "gravar")
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


def list_files(server: ServerConfig, root_name: str, glob: str) -> ListFilesResult:
    root = _root(server, root_name)
    if not root.is_dir():
        raise TaskError(f"root {root_name!r} não é uma pasta existente: {root}")
    if glob.startswith(("/", "\\")) or ".." in glob.replace("\\", "/").split("/") or ":" in glob:
        raise TaskError(f"padrão de busca inválido: {glob!r}")
    matches = sorted(p for p in root.glob(glob) if p.is_file())
    return ListFilesResult(paths=[m.relative_to(root).as_posix() for m in matches])


def move_files(server: ServerConfig, root_name: str, moves: list[dict]) -> MoveFilesResult:
    """Move/renomeia um GRUPO de arquivos dentro do mesmo root, tudo ou nada.

    Usado para renomear uma análise do Rock-Eval: o `.B00` e o `.B00~` (cópia
    anterior guardada pelo GeoWorks) andam sempre juntos. Cada item é
    `{"from": "...", "to": "...", "optional": bool}` — `optional` = pode não
    existir (ex. análise sem `.B00~`), e aí é simplesmente pulado.

    Garantias: nunca sobrescreve (se qualquer destino já existir, nada é
    movido); tudo ou nada (falha no meio desfaz os anteriores); origem e
    destino confinados ao root; o conteúdo nunca é lido nem alterado.
    """
    _root(server, root_name)
    _require_mode(server, root_name, _CAN_MOVE, "mover/renomear")
    if not isinstance(moves, list) or not moves:
        raise TaskError("nenhum arquivo para mover")

    planned: list[tuple[Path, Path, str]] = []
    seen_targets: set[Path] = set()
    for item in moves:
        if not isinstance(item, dict) or not item.get("from") or not item.get("to"):
            raise TaskError(f"movimento inválido: {item!r}")
        src = _resolve_safe(server, root_name, item["from"])
        dst = _resolve_safe(server, root_name, item["to"])
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

    root = _root(server, root_name)
    return MoveFilesResult(paths=[dst.relative_to(root).as_posix() for _, dst, _ in planned])
