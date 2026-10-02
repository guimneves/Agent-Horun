"""As operações que o agente sabe fazer — ler (inteiro ou em pedaços),
escrever (atômico), listar arquivos, listar a árvore (pastas + arquivos com
tamanho) e mover/renomear (em grupo, tudo ou nada) — sempre confinadas a um
`root` permitido pela configuração local e respeitando o modo de cada root
(`read` / `read-move` / `read-write`, ver config.py). Nenhuma lógica de
negócio aqui: quem entende o conteúdo é sempre o backend do módulo.

- Confinamento: o caminho pedido é normalizado e precisa continuar dentro
  do root (`..`, caminho absoluto e letra de unidade são recusados).
- Caminhos longos no Windows: as pastas reais passam de 260 caracteres
  (ex. as do drive do Financeiro); sem o prefixo `\\\\?\\` o Windows recusa.
  Toda operação de disco passa por `_os_path`, e os caminhos devolvidos ao
  servidor nunca levam o prefixo (sempre relativos ao root, em posix).
- Escrita por substituição atômica (temporário no mesmo diretório +
  `os.replace`), pra nunca deixar o software do equipamento ler um arquivo
  pela metade.
- Erros com `code` (PROTOCOL.md): not_found, outside_root, unknown_root,
  read_only, too_large.
"""

from __future__ import annotations

import base64
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from agent.config import ConfigError, ServerConfig

_WINDOWS = sys.platform == "win32"
MAX_TREE_ENTRIES = 50_000


class TaskError(ValueError):
    """Erro esperado de uma tarefa — vira `{"ok": false, "error": ...,
    "code": ...}` no resultado reportado ao servidor, nunca derruba o
    agente."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


@dataclass
class ReadFileResult:
    content_base64: str
    size: int  # tamanho TOTAL do arquivo (para leituras em pedaços)


@dataclass
class ListFilesResult:
    paths: list[str]  # relativos ao root, sempre em posix (/ nunca \)


@dataclass
class TreeEntry:
    path: str  # relativo ao root, posix
    is_dir: bool
    size: int | None


@dataclass
class MoveFilesResult:
    paths: list[str]  # destinos efetivamente movidos, relativos ao root (posix)


_CAN_WRITE = {"read-write"}
_CAN_MOVE = {"read-write", "read-move"}


# ------------------------------------------------------------ caminhos


def _os_path(path: str | Path) -> str:
    """Caminho pronto para o sistema de arquivos. No Windows, com o prefixo
    `\\\\?\\` — sem ele, caminhos acima de 260 caracteres falham."""
    absolute = os.path.abspath(os.fspath(path))
    if _WINDOWS and not absolute.startswith("\\\\?\\"):
        if absolute.startswith("\\\\"):
            return "\\\\?\\UNC\\" + absolute[2:]
        return "\\\\?\\" + absolute
    return absolute


def _makedirs(path: str) -> None:
    """`os.makedirs` que funciona com caminho longo (o original se perde no
    prefixo `\\\\?\\` ao subir pelos pais)."""
    missing: list[str] = []
    current = os.path.abspath(path)
    while not os.path.isdir(_os_path(current)):
        missing.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    for folder in reversed(missing):
        os.mkdir(_os_path(folder))


def _root(server: ServerConfig, root_name: str) -> str:
    try:
        return os.path.abspath(server.root(root_name))
    except ConfigError as exc:
        raise TaskError(str(exc), code="unknown_root") from exc


def _require_mode(server: ServerConfig, root_name: str, allowed: set[str], action: str) -> None:
    mode = server.root_mode(root_name)
    if mode not in allowed:
        raise TaskError(
            f"sem permissão para {action} em {root_name!r} (pasta configurada como {mode!r})", code="read_only"
        )


def _resolve_safe(server: ServerConfig, root_name: str, relative_path: str) -> str:
    """Caminho absoluto (sem prefixo) dentro do root, ou TaskError."""
    root = _root(server, root_name)
    rel = (relative_path or "").replace("\\", "/")
    if rel.startswith("/") or ":" in rel:
        raise TaskError(f"caminho fora do root permitido: {relative_path!r}", code="outside_root")
    candidate = os.path.abspath(os.path.join(root, *[p for p in rel.split("/") if p]))
    if os.path.normcase(candidate) != os.path.normcase(root) and not os.path.normcase(candidate).startswith(
        os.path.normcase(root.rstrip("\\/")) + os.sep
    ):
        raise TaskError(f"caminho fora do root permitido: {relative_path!r}", code="outside_root")
    return candidate


def _plain(path: str) -> str:
    """Tira o prefixo `\\\\?\\` (o inverso de `_os_path`)."""
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def _relative(root: str, absolute: str) -> str:
    return os.path.relpath(_plain(absolute), _plain(root)).replace("\\", "/")


def _glob_regex(pattern: str) -> re.Pattern:
    """Glob simples, igual ao do pathlib: `**/` = qualquer nível de pasta,
    `*` e `?` sem atravessar `/`."""
    out = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out + r"\Z", re.IGNORECASE if _WINDOWS else 0)


def _walk(start: str):
    """os.walk que aguenta caminho longo: devolve (pasta_sem_prefixo, subpastas, arquivos)."""
    for dirpath, dirnames, filenames in os.walk(_os_path(start)):
        dirnames.sort()
        yield _plain(dirpath), dirnames, sorted(filenames)


# ------------------------------------------------------------ operações


def read_file(
    server: ServerConfig, root_name: str, relative_path: str, *, offset: int = 0, length: int | None = None
) -> ReadFileResult:
    """Lê o arquivo inteiro, ou `length` bytes a partir de `offset`. Nunca
    mais que `server.max_read_bytes` por resposta (`too_large`)."""
    path = _resolve_safe(server, root_name, relative_path)
    fs = _os_path(path)
    if not os.path.isfile(fs):
        raise TaskError(f"arquivo não encontrado: {relative_path!r}", code="not_found")
    if offset < 0 or (length is not None and length < 0):
        raise TaskError("offset/length inválidos")
    size = os.path.getsize(fs)
    wanted = (size - offset) if length is None else min(length, max(size - offset, 0))
    if wanted > server.max_read_bytes:
        raise TaskError(
            f"pedaço grande demais ({wanted} bytes; limite {server.max_read_bytes}) — leia em pedaços",
            code="too_large",
        )
    with open(fs, "rb") as f:
        f.seek(offset)
        raw = f.read(wanted)
    return ReadFileResult(content_base64=base64.b64encode(raw).decode("ascii"), size=size)


def write_file(server: ServerConfig, root_name: str, relative_path: str, content_base64: str) -> None:
    path = _resolve_safe(server, root_name, relative_path)
    _require_mode(server, root_name, _CAN_WRITE, "gravar")
    try:
        raw = base64.b64decode(content_base64, validate=True)
    except (base64.binascii.Error, ValueError) as exc:
        raise TaskError(f"conteúdo base64 inválido: {exc}") from exc

    folder = os.path.dirname(path)
    _makedirs(folder)
    fd, tmp_path = tempfile.mkstemp(dir=_os_path(folder), prefix=".horun_agent_", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
        os.replace(tmp_path, _os_path(path))
    except BaseException:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def list_files(server: ServerConfig, root_name: str, glob: str) -> ListFilesResult:
    root = _root(server, root_name)
    if not os.path.isdir(_os_path(root)):
        raise TaskError(f"root {root_name!r} não é uma pasta existente: {root}", code="not_found")
    if glob.startswith(("/", "\\")) or ".." in glob.replace("\\", "/").split("/") or ":" in glob:
        raise TaskError(f"padrão de busca inválido: {glob!r}", code="outside_root")
    pattern = glob.replace("\\", "/")
    matcher = _glob_regex(pattern)
    # começa a varredura na parte fixa do padrão ("Job/**/*.B00" -> só a
    # pasta Job), em vez de percorrer o root inteiro
    fixed = []
    for part in pattern.split("/")[:-1]:
        if any(c in part for c in "*?["):
            break
        fixed.append(part)
    start = os.path.join(root, *fixed) if fixed else root
    if not os.path.isdir(_os_path(start)):
        return ListFilesResult(paths=[])
    found: list[str] = []
    for folder, _dirs, files in _walk(start):
        for name in files:
            rel = _relative(root, os.path.join(folder, name))
            if matcher.match(rel):
                found.append(rel)
    return ListFilesResult(paths=sorted(found))


def list_tree(server: ServerConfig, root_name: str, relative_path: str = "", *, recursive: bool = True) -> list[TreeEntry]:
    """Pastas E arquivos (com tamanho) a partir de `relative_path` — pastas
    vazias também aparecem (list_files só vê arquivos). Caminhos relativos ao
    ROOT, sem a própria pasta pedida, ordenados. Mais de MAX_TREE_ENTRIES →
    `too_large`."""
    root = _root(server, root_name)
    start = _resolve_safe(server, root_name, relative_path)
    if not os.path.isdir(_os_path(start)):
        raise TaskError(f"pasta não encontrada: {relative_path!r}", code="not_found")
    entries: list[TreeEntry] = []
    for folder, dirs, files in _walk(start):
        for name in dirs:
            entries.append(TreeEntry(_relative(root, os.path.join(folder, name)), True, None))
        for name in files:
            full = os.path.join(folder, name)
            entries.append(TreeEntry(_relative(root, full), False, os.path.getsize(_os_path(full))))
        if len(entries) > MAX_TREE_ENTRIES:
            raise TaskError(f"mais de {MAX_TREE_ENTRIES} itens — peça uma pasta menor", code="too_large")
        if not recursive:
            break
    entries.sort(key=lambda e: e.path)
    return entries


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
    root = _root(server, root_name)
    _require_mode(server, root_name, _CAN_MOVE, "mover/renomear")
    if not isinstance(moves, list) or not moves:
        raise TaskError("nenhum arquivo para mover")

    planned: list[tuple[str, str]] = []
    seen_targets: set[str] = set()
    for item in moves:
        if not isinstance(item, dict) or not item.get("from") or not item.get("to"):
            raise TaskError(f"movimento inválido: {item!r}")
        src = _resolve_safe(server, root_name, item["from"])
        dst = _resolve_safe(server, root_name, item["to"])
        if os.path.normcase(src) == os.path.normcase(dst):
            raise TaskError(f"origem e destino iguais: {item['from']!r}")
        if not os.path.isfile(_os_path(src)):
            if item.get("optional"):
                continue
            raise TaskError(f"arquivo não encontrado: {item['from']!r}", code="not_found")
        key = os.path.normcase(dst)
        if os.path.exists(_os_path(dst)) or key in seen_targets:
            raise TaskError(f"já existe um arquivo com esse nome: {item['to']!r}")
        seen_targets.add(key)
        planned.append((src, dst))

    if not planned:
        raise TaskError("nenhum dos arquivos de origem existe", code="not_found")

    done: list[tuple[str, str]] = []
    try:
        for src, dst in planned:
            _makedirs(os.path.dirname(dst))
            if os.path.exists(_os_path(dst)):  # conferido de novo logo antes — nunca sobrescrever
                raise TaskError(f"já existe um arquivo com esse nome: {os.path.basename(dst)!r}")
            os.rename(_os_path(src), _os_path(dst))
            done.append((src, dst))
    except BaseException as exc:
        for src, dst in reversed(done):
            try:
                os.rename(_os_path(dst), _os_path(src))
            except OSError:
                pass  # melhor esforço; o erro original é o que importa reportar
        if isinstance(exc, TaskError):
            raise
        if isinstance(exc, OSError):
            raise TaskError(f"não foi possível mover (arquivo aberto em outro programa?): {exc}") from exc
        raise

    return MoveFilesResult(paths=[_relative(root, dst) for _, dst in planned])
