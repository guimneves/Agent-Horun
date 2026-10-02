"""Rotas /agent/* — as mesmas para todo módulo. O módulo monta com:

    from app.agent_server.routes import build_router
    app.include_router(build_router(
        get_session=get_session,                 # dependência de sessão do módulo
        admin_dependency=require_admin,          # quem pode gerar códigos/revogar
        actor_of=lambda admin: (admin.id, admin.username),  # (id int|None, nome)
    ))

Contrato exato das chamadas do agente: PROTOCOL.md (repositório Agent-Horun).

Compatibilidade com agentes antigos (0.1/0.2, ainda instalados): a lista de
tarefas sempre leva exatamente os 6 campos de antes (id, op, root, path,
content_base64, glob) — o 0.1 quebra com campo a mais ou a menos. Campos
novos (`args`) só vão para agente que se identificou >= 0.3.0 pelo cabeçalho
X-Horun-Agent-Version.

(Sem `from __future__ import annotations` neste arquivo de propósito: as
dependências são definidas dentro de `build_router` e o FastAPI precisa
enxergar os tipos de verdade, não strings.)
"""

import hashlib
import json
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import update
from sqlmodel import Session, select

from .bridge import supports
from .models import AgentDevice, AgentEnrollCode, AgentTask, as_utc
from .settings import settings

VERSION_HEADER = "x-horun-agent-version"
# Sem 0/O/1/I/L: o código é digitado à mão no config.json do equipamento.
_CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
ENROLL_CODE_LENGTH = 10


def generate_device_token() -> str:
    return secrets.token_urlsafe(32)


def hash_device_token(token: str) -> str:
    # token de alta entropia gerado por nós (não é senha escolhida por
    # alguém): basta não guardar em claro, sem custo de bcrypt
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_enroll_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(ENROLL_CODE_LENGTH))


# ---------------------------------------------------------------- schemas

class EnrollCodeOut(BaseModel):
    code: str
    expires_at: Optional[datetime] = None


class DeviceOut(BaseModel):
    id: int
    device_name: str
    enrolled_at: datetime
    last_seen_at: Optional[datetime]
    revoked_at: Optional[datetime]
    agent_version: Optional[str] = None


class EnrollIn(BaseModel):
    enroll_code: str
    device_name: str


class EnrollOut(BaseModel):
    device_token: str


class TaskResultIn(BaseModel):
    ok: bool
    content_base64: Optional[str] = None
    paths: Optional[list[str]] = None
    size: Optional[int] = None
    entries: Optional[list[dict]] = None  # list_tree
    error: Optional[str] = None
    code: Optional[str] = None


_last_cleanup = 0.0


def _split_pending(session: Session, now: datetime) -> list[AgentTask]:
    """Uma consulta só (o agente chama a cada poucos segundos): devolve as
    pendentes válidas e expira as vencidas — quem as criou desistiu, ou o
    backend caiu no meio da espera. Sem prazo = anteriores a esta regra =
    vencidas. Só grava se houver vencida."""
    pending = session.exec(
        select(AgentTask).where(AgentTask.status == "pending").order_by(AgentTask.created_at)
    ).all()
    valid: list[AgentTask] = []
    stale: list[AgentTask] = []
    for t in pending:
        (valid if t.expires_at is not None and as_utc(t.expires_at) > now else stale).append(t)
    for t in stale:
        t.status = "expired"
        t.completed_at = now
        session.add(t)
    if stale:
        session.commit()
    return valid


def _cleanup_old_tasks(session: Session, now: datetime) -> None:
    global _last_cleanup
    if time.monotonic() - _last_cleanup < settings.cleanup_interval_seconds:
        return
    _last_cleanup = time.monotonic()
    cutoff = now - timedelta(days=settings.task_retention_days)
    old = [
        t
        for t in session.exec(select(AgentTask).where(AgentTask.status != "pending")).all()
        if as_utc(t.created_at) < cutoff
    ]
    for t in old:
        session.delete(t)
    if old:
        session.commit()


def _task_payload(task: AgentTask, device: AgentDevice) -> dict:
    payload = {
        "id": task.id, "op": task.op, "root": task.root,
        "path": task.path, "content_base64": task.content_base64, "glob": task.glob,
    }
    if task.args_json and supports(device.agent_version, "read_range"):
        payload["args"] = json.loads(task.args_json)
    return payload


def build_router(
    *,
    get_session: Callable[..., Any],
    admin_dependency: Callable[..., Any],
    actor_of: Callable[[Any], tuple[Optional[int], Optional[str]]],
    prefix: str = "/agent",
) -> APIRouter:
    SessionDep = Annotated[Session, Depends(get_session)]
    AdminDep = Annotated[Any, Depends(admin_dependency)]

    def device_auth(request: Request, session: SessionDep) -> AgentDevice:
        """Autentica uma chamada do agente (`Authorization: Bearer <token>`)
        e registra quando foi visto e qual versão está rodando."""
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        if not token:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token do agente ausente")
        device = session.exec(select(AgentDevice).where(AgentDevice.token_hash == hash_device_token(token))).first()
        if device is None or device.revoked_at is not None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token do agente inválido ou revogado")
        device.last_seen_at = datetime.now(timezone.utc)
        version = request.headers.get(VERSION_HEADER)
        if version:
            device.agent_version = version[:20]
        session.add(device)
        session.commit()
        return device

    DeviceDep = Annotated[AgentDevice, Depends(device_auth)]
    router = APIRouter(prefix=prefix, tags=["agent"])

    # ------------------------------------------------ administração

    @router.post("/enroll-codes", response_model=EnrollCodeOut)
    def create_enroll_code(admin: AdminDep, session: SessionDep):
        """Código de uso único para instalar um agente novo (vai no
        `enroll_code` do config.json do equipamento). Expira em
        `settings.enroll_code_ttl_minutes`."""
        created_by_id, created_by_name = actor_of(admin)
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.enroll_code_ttl_minutes)
        code = generate_enroll_code()
        session.add(AgentEnrollCode(
            code=code, created_by_id=created_by_id, created_by_name=created_by_name, expires_at=expires_at,
        ))
        session.commit()
        return EnrollCodeOut(code=code, expires_at=expires_at)

    @router.get("/devices", response_model=list[DeviceOut])
    def list_devices(admin: AdminDep, session: SessionDep):
        """Quais instalações já enrolaram, quando cada uma foi vista pela
        última vez e qual versão do agente está rodando."""
        devices = session.exec(select(AgentDevice).order_by(AgentDevice.enrolled_at.desc())).all()
        return [
            DeviceOut(
                id=d.id, device_name=d.device_name, enrolled_at=d.enrolled_at, last_seen_at=d.last_seen_at,
                revoked_at=d.revoked_at, agent_version=d.agent_version,
            )
            for d in devices
        ]

    @router.post("/devices/{device_id}/revoke")
    def revoke_device(device_id: int, admin: AdminDep, session: SessionDep):
        device = session.get(AgentDevice, device_id)
        if device is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Instalação não encontrada")
        device.revoked_at = datetime.now(timezone.utc)
        session.add(device)
        session.commit()
        return {"ok": True}

    # ------------------------------------------------ agente

    @router.post("/enroll", response_model=EnrollOut)
    def enroll(payload: EnrollIn, session: SessionDep):
        code = payload.enroll_code.strip().upper()
        entry = session.exec(select(AgentEnrollCode).where(AgentEnrollCode.code == code)).first()
        if entry is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Código de enrolamento não encontrado")
        if entry.used_at is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "Código de enrolamento já foi usado")
        now = datetime.now(timezone.utc)
        if entry.expires_at is not None and as_utc(entry.expires_at) < now:
            raise HTTPException(status.HTTP_410_GONE, "Código de enrolamento expirado — gere um novo")

        # consome o código numa instrução só: duas instalações com o mesmo
        # código ao mesmo tempo não podem as duas virar dispositivos
        claimed = session.exec(
            update(AgentEnrollCode)
            .where(AgentEnrollCode.id == entry.id, AgentEnrollCode.used_at.is_(None))
            .values(used_at=now)
        )
        session.commit()
        if claimed.rowcount != 1:
            raise HTTPException(status.HTTP_409_CONFLICT, "Código de enrolamento já foi usado")

        token = generate_device_token()
        device = AgentDevice(device_name=payload.device_name, token_hash=hash_device_token(token))
        session.add(device)
        session.commit()
        session.refresh(device)
        session.refresh(entry)
        entry.used_by_device_id = device.id
        session.add(entry)
        session.commit()
        return EnrollOut(device_token=token)

    @router.get("/tasks")
    def get_pending_tasks(device: DeviceDep, session: SessionDep):
        """Tarefas pendentes dentro do prazo. Simplificação deliberada:
        qualquer agente autenticado do módulo vê qualquer tarefa — cada
        módulo tem hoje um equipamento só."""
        now = datetime.now(timezone.utc)
        tasks = _split_pending(session, now)
        out = {"tasks": [_task_payload(t, device) for t in tasks]}
        _cleanup_old_tasks(session, now)
        return out

    @router.post("/tasks/{task_id}/result")
    def report_task_result(task_id: int, payload: TaskResultIn, device: DeviceDep, session: SessionDep):
        task = session.get(AgentTask, task_id)
        if task is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Tarefa não encontrada")
        if task.status == "expired" and task.result_ok is None:
            # pegou a tempo, respondeu depois do prazo: o usuário já viu erro.
            # Guarda o resultado só para diagnóstico; continua "expired".
            task.result_ok = payload.ok
            task.result_error = payload.error
            task.result_code = payload.code
            session.add(task)
            session.commit()
            return {"ok": True}
        if task.status != "pending":
            return {"ok": True}  # reenvio — idempotente

        task.result_ok = payload.ok
        task.result_content_base64 = payload.content_base64
        listed = payload.entries if payload.entries is not None else payload.paths
        task.result_paths = json.dumps(listed) if listed is not None else None
        task.result_size = payload.size
        task.result_error = payload.error
        task.result_code = payload.code
        task.status = "done" if payload.ok else "error"
        task.completed_at = datetime.now(timezone.utc)
        session.add(task)
        session.commit()
        return {"ok": True}

    return router
