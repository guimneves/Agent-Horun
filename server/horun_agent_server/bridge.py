"""Ponte entre o backend do módulo e o agente: cria uma `AgentTask` e espera
— consultando o banco em intervalos curtos, no mesmo processo — até o
agente reportar o resultado (`POST /agent/tasks/{id}/result`) ou estourar o
prazo. Do ponto de vista de quem chama, é síncrono.

Regras (vieram de bugs reais no RE7S e no Financeiro):
- sessões de banco curtas e independentes, nunca a sessão do pedido HTTP
  durante a espera (`_poll_once`) — com SQLite + uma conexão compartilhada,
  reter a sessão do chamador causava timeouts intermitentes;
- prazo estourado = tarefa "expired", nunca mais entregue (`_expire`);
- opcionalmente, falha na hora se nenhum agente foi visto recentemente
  (`settings.fail_fast_when_offline`);
- operações/campos novos só para agentes que suportam (`_require_version`):
  ex. leitura em pedaços precisa de agente >= 0.3.0.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import update
from sqlmodel import Session, select

from .models import AgentDevice, AgentTask, as_utc, utcnow
from .settings import settings


class AgentTaskError(RuntimeError):
    """O agente respondeu, mas com falha (arquivo não encontrado, sem
    permissão na pasta, root desconhecido...) — mensagem pronta para virar
    um erro 502/409 na rota que chamou. `code` (agente >= 0.4.0):
    not_found, outside_root, unknown_root, read_only, too_large, unknown_op."""

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


class AgentTimeoutError(AgentTaskError):
    """O agente não reportou dentro do prazo. A tarefa foi expirada; se ele
    a pegou a tempo e terminou depois, a ação PODE ter acontecido."""


class AgentOfflineError(AgentTaskError):
    """Nenhum agente visto recentemente — nada foi enfileirado (só com
    `settings.fail_fast_when_offline`)."""


class AgentTooOldError(AgentTaskError):
    """O agente instalado não suporta a operação pedida — atualizar."""


# Versão mínima do agente para cada recurso. Agente que não manda versão
# (anterior à 0.3.0) é tratado como 0.2.0.
FEATURE_MIN_VERSION = {
    "move_files": "0.2.0",
    "read_range": "0.3.0",
    "list_tree": "0.4.0",
}
_UNKNOWN_VERSION = "0.2.0"


def parse_version(v: str | None) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in (v or _UNKNOWN_VERSION).split(".")[:3])
    except ValueError:
        return tuple(int(p) for p in _UNKNOWN_VERSION.split("."))


def supports(version: str | None, feature: str) -> bool:
    return parse_version(version) >= parse_version(FEATURE_MIN_VERSION[feature])


@dataclass
class _TaskSnapshot:
    status: str
    result_ok: bool | None
    result_content_base64: str | None
    result_paths: str | None
    result_error: str | None
    result_size: int | None = None
    result_code: str | None = None


def _failed(snap: _TaskSnapshot, fallback: str) -> AgentTaskError:
    return AgentTaskError(snap.result_error or fallback, code=snap.result_code)


def _online_devices(bind) -> list[AgentDevice]:
    cutoff = utcnow() - timedelta(seconds=settings.online_window_seconds)
    with Session(bind) as s:
        devices = s.exec(select(AgentDevice).where(AgentDevice.revoked_at.is_(None))).all()
        return [d for d in devices if d.last_seen_at is not None and as_utc(d.last_seen_at) >= cutoff]


def _require_online(bind) -> list[AgentDevice]:
    online = _online_devices(bind)
    if settings.fail_fast_when_offline and not online:
        raise AgentOfflineError(
            "nenhum Agente Horun conectado agora — confira se o PC do equipamento está ligado, "
            "com rede e com o serviço do agente rodando"
        )
    return online


def _require_version(bind, feature: str) -> None:
    """Recusa ANTES de enfileirar se nenhum agente conectado suporta o
    recurso. Sem nenhum agente conectado, deixa seguir (vai expirar ou, com
    fail_fast, já foi recusado)."""
    online = _require_online(bind)
    if online and not any(supports(d.agent_version, feature) for d in online):
        raise AgentTooOldError(
            f"o Agente Horun instalado no equipamento é antigo para esta operação — "
            f"atualize para a versão {FEATURE_MIN_VERSION[feature]} ou mais nova"
        )


def _enqueue(bind, ttl_seconds: float | None = None, args: dict | None = None, **fields) -> int:
    """Sessão curta, aberta e fechada na hora — nunca a sessão do chamador.
    `ttl_seconds` (quanto quem chamou vai esperar) vira o prazo da tarefa."""
    ttl = ttl_seconds if ttl_seconds is not None else settings.timeout()
    with Session(bind) as s:
        task = AgentTask(
            status="pending",
            expires_at=utcnow() + timedelta(seconds=ttl),
            args_json=json.dumps(args) if args else None,
            **fields,
        )
        s.add(task)
        s.commit()
        return task.id


def _poll_once(bind, task_id: int) -> _TaskSnapshot | None:
    with Session(bind) as s:
        task = s.get(AgentTask, task_id)
        if task is None:
            return None
        return _TaskSnapshot(
            status=task.status,
            result_ok=task.result_ok,
            result_content_base64=task.result_content_base64,
            result_paths=task.result_paths,
            result_error=task.result_error,
            result_size=task.result_size,
            result_code=task.result_code,
        )


def _expire(bind, task_id: int) -> bool:
    """Marca como expirada SE ainda pendente, numa instrução só (atômica).
    False = o agente respondeu primeiro."""
    with Session(bind) as s:
        result = s.exec(
            update(AgentTask)
            .where(AgentTask.id == task_id, AgentTask.status == "pending")
            .values(status="expired", completed_at=utcnow())
        )
        s.commit()
        return result.rowcount == 1


def _wait(bind, task_id: int, timeout: float, poll_interval: float = 0.25) -> _TaskSnapshot:
    deadline = time.monotonic() + timeout
    while True:
        snapshot = _poll_once(bind, task_id)
        if snapshot is None:
            raise AgentTaskError(f"tarefa {task_id} sumiu do banco")
        if snapshot.status != "pending":
            return snapshot
        if time.monotonic() >= deadline:
            if not _expire(bind, task_id):
                snapshot = _poll_once(bind, task_id)
                if snapshot is not None and snapshot.status not in ("pending", "expired"):
                    return snapshot
            raise AgentTimeoutError(
                f"o agente não respondeu em {timeout:g}s — confira se está instalado, ligado e com rede. "
                "Nada foi gravado no equipamento por esta tentativa."
            )
        time.sleep(poll_interval)


def _run(session: Session, timeout: float | None, **fields) -> _TaskSnapshot:
    bind = session.get_bind()
    wait_for = timeout if timeout is not None else settings.timeout()
    task_id = _enqueue(bind, ttl_seconds=wait_for, **fields)
    return _wait(bind, task_id, wait_for)


# ------------------------------------------------------------- operações


def _too_large(path: str, size: int, limit: int) -> AgentTaskError:
    return AgentTaskError(
        f"{path}: arquivo de {size / 1024 / 1024:.1f} MB acima do limite de {limit / 1024 / 1024:.0f} MB",
        code="too_large",
    )


def read_bytes(
    session: Session,
    root: str,
    path: str,
    *,
    timeout: float | None = None,
    chunk_size: int | None = None,
    max_bytes: int | None = None,
) -> bytes:
    """Lê um arquivo do equipamento. Com `chunk_size`, em pedaços (uma
    tarefa por pedaço — para arquivos que não cabem numa resposta só);
    precisa de agente >= 0.3.0. `timeout` vale para cada pedaço.
    `max_bytes`: arquivo maior é recusado (`code="too_large"`) — em pedaços,
    já no primeiro, sem baixar o resto."""
    bind = session.get_bind()
    if chunk_size is None:
        _require_online(bind)
        snap = _run(session, timeout, op="read_file", root=root, path=path)
        if not snap.result_ok:
            raise _failed(snap, f"falha ao ler {path!r} via agente")
        data = base64.b64decode(snap.result_content_base64 or "")
        if max_bytes is not None and len(data) > max_bytes:
            raise _too_large(path, len(data), max_bytes)
        return data

    _require_version(bind, "read_range")
    parts: list[bytes] = []
    offset = 0
    while True:
        snap = _run(
            session, timeout, op="read_file", root=root, path=path, args={"offset": offset, "length": chunk_size}
        )
        if not snap.result_ok:
            raise _failed(snap, f"falha ao ler {path!r} via agente")
        total = snap.result_size
        if max_bytes is not None and total is not None and total > max_bytes:
            raise _too_large(path, total, max_bytes)
        piece = base64.b64decode(snap.result_content_base64 or "")
        parts.append(piece)
        offset += len(piece)
        if not piece or total is None or offset >= total:
            return b"".join(parts)


def agent_supports(session: Session, feature: str) -> bool:
    """Algum agente conectado agora suporta `feature`? (ex. para ler em
    pedaços só quando der, e senão o arquivo inteiro.)"""
    return any(supports(d.agent_version, feature) for d in _online_devices(session.get_bind()))


def read_text(session: Session, root: str, path: str, *, timeout: float | None = None) -> str:
    """Lê um arquivo de texto (UTF-8). `AgentTaskError` com "não encontrado"
    na mensagem se não existir (ver `file_not_found`). `session` só serve
    para achar o banco — o trabalho usa sessões próprias, curtas."""
    return read_bytes(session, root, path, timeout=timeout).decode("utf-8")


def write_bytes(session: Session, root: str, path: str, content: bytes, *, timeout: float | None = None) -> None:
    _require_online(session.get_bind())
    encoded = base64.b64encode(content).decode("ascii")
    snap = _run(session, timeout, op="write_file", root=root, path=path, content_base64=encoded)
    if not snap.result_ok:
        raise _failed(snap, f"falha ao escrever {path!r} via agente")


def write_text(session: Session, root: str, path: str, content: str, *, timeout: float | None = None) -> None:
    write_bytes(session, root, path, content.encode("utf-8"), timeout=timeout)


def list_files(session: Session, root: str, glob: str, *, timeout: float | None = None) -> list[str]:
    """Caminhos relativos ao root (sempre posix) que batem com `glob`."""
    _require_online(session.get_bind())
    snap = _run(session, timeout, op="list_files", root=root, glob=glob)
    if not snap.result_ok:
        raise _failed(snap, f"falha ao listar {glob!r} via agente")
    return json.loads(snap.result_paths or "[]")


def list_tree(
    session: Session, root: str, path: str = "", *, recursive: bool = True, timeout: float | None = None
) -> list[dict]:
    """Pastas e arquivos a partir de `path` (agente >= 0.4.0): lista de
    `{"path", "is_dir", "size"}`, caminhos relativos ao ROOT, em posix.
    Pastas vazias aparecem (list_files só vê arquivos)."""
    _require_version(session.get_bind(), "list_tree")
    snap = _run(session, timeout, op="list_tree", root=root, path=path, args={"recursive": recursive})
    if not snap.result_ok:
        raise _failed(snap, f"falha ao listar a pasta {path!r} via agente")
    return json.loads(snap.result_paths or "[]")


def move_files(session: Session, root: str, moves: list[dict], *, timeout: float | None = None) -> list[str]:
    """Move/renomeia um grupo de arquivos, tudo ou nada (agente >= 0.2.0).
    Os movimentos vão em JSON dentro de `content_base64` — de propósito, para
    um agente antigo só responder "operação desconhecida" em vez de quebrar
    com um campo novo. Devolve os destinos efetivamente movidos."""
    _require_version(session.get_bind(), "move_files")
    payload = base64.b64encode(json.dumps({"moves": moves}).encode("utf-8")).decode("ascii")
    snap = _run(session, timeout, op="move_files", root=root, content_base64=payload)
    if not snap.result_ok:
        error = snap.result_error or "falha ao mover os arquivos via agente"
        if "operação desconhecida" in error:
            error = "o Agente Horun instalado no equipamento é antigo — atualize para a versão 0.2.0 ou mais nova"
        raise AgentTaskError(error, code=snap.result_code)
    return json.loads(snap.result_paths or "[]")


def file_not_found(exc: AgentTaskError) -> bool:
    """Distingue "arquivo não existe" (equivalente a `Path.exists() is
    False`) de qualquer outro erro — pelo `code` (agente >= 0.4.0) ou pelo
    texto que os agentes antigos usam."""
    return exc.code == "not_found" or "não encontrado" in str(exc)
