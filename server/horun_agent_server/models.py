"""Tabelas do lado servidor do agente. Nomes de tabela e colunas IGUAIS aos
que o RE7S já tinha em produção (agentenrollcode, agentdevice, agenttask) —
trocar para o pacote não exige migração de dados. As colunas que vieram
depois estão em `MIGRATIONS`: o módulo passa cada uma pelo `_ensure_column`
dele na subida (create_all não adiciona coluna a tabela existente).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime | None) -> datetime | None:
    """SQLite devolve datetime sem fuso; tudo aqui é gravado em UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class AgentEnrollCode(SQLModel, table=True):
    """Código de uso único que um admin do módulo gera (`POST
    /agent/enroll-codes`) para uma instalação nova do agente trocar por uma
    credencial permanente (`POST /agent/enroll`). Vale por
    `settings.enroll_code_ttl_minutes` e é consumido de forma atômica."""

    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True, unique=True)
    # Quem gerou: id numérico (módulos com tabela de usuário própria, como o
    # RE7S) e/ou nome (módulos que só conhecem a identidade do Core).
    created_by_id: Optional[int] = None
    created_by_name: Optional[str] = None
    created_at: datetime = Field(default_factory=utcnow)
    expires_at: Optional[datetime] = None  # None = código antigo, sem prazo
    used_at: Optional[datetime] = None
    used_by_device_id: Optional[int] = Field(default=None, foreign_key="agentdevice.id")


class AgentDevice(SQLModel, table=True):
    """Uma instalação do agente já enrolada (o PC de um equipamento).
    `token_hash` autentica as chamadas do agente (`Authorization: Bearer`) —
    só o hash é guardado. `agent_version` vem do cabeçalho
    X-Horun-Agent-Version (agente >= 0.3.0); None = agente mais antigo."""

    id: Optional[int] = Field(default=None, primary_key=True)
    device_name: str
    token_hash: str = Field(index=True, unique=True)
    enrolled_at: datetime = Field(default_factory=utcnow)
    last_seen_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None
    agent_version: Optional[str] = None


class AgentTask(SQLModel, table=True):
    """Uma tarefa de arquivo (ler, escrever, listar, mover) pendente ou já
    resolvida. Criada só pelo `bridge`, nunca por uma rota pública.

    `root` é um nome lógico de pasta (ex. "data", "jobs", "drive") — o
    servidor nunca sabe o caminho real do disco do equipamento, só o
    `config.json` daquela instalação sabe.

    `status`: "pending" | "done" | "error" | "expired". "expired" = quem
    esperava desistiu (prazo `expires_at`); a tarefa NUNCA mais é entregue —
    senão uma gravação que o usuário viu falhar aconteceria depois."""

    id: Optional[int] = Field(default=None, primary_key=True)
    op: str  # "read_file" | "write_file" | "list_files" | "move_files"
    root: str
    path: Optional[str] = None
    content_base64: Optional[str] = None  # write_file: conteúdo; move_files: JSON dos movimentos
    glob: Optional[str] = None
    args_json: Optional[str] = None  # parâmetros extras (ex. offset/length) — só para agente >= 0.3.0
    status: str = Field(default="pending", index=True)
    result_ok: Optional[bool] = None
    result_content_base64: Optional[str] = None
    result_paths: Optional[str] = None  # JSON list[str]; em list_tree, JSON list[{path, is_dir, size}]
    result_size: Optional[int] = None  # read_file: tamanho total do arquivo
    result_error: Optional[str] = None
    result_code: Optional[str] = None  # not_found, outside_root, unknown_root, read_only, too_large...
    created_at: datetime = Field(default_factory=utcnow, index=True)
    completed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


# (tabela, coluna, tipo estilo SQLite) — o módulo traduz para o banco dele
# (ver `add_column_ddl` no module-template do Horun Core).
MIGRATIONS: list[tuple[str, str, str]] = [
    ("agentenrollcode", "created_by_name", "VARCHAR"),
    ("agentenrollcode", "expires_at", "DATETIME"),
    ("agentdevice", "agent_version", "VARCHAR"),
    ("agenttask", "expires_at", "DATETIME"),
    ("agenttask", "args_json", "VARCHAR"),
    ("agenttask", "result_size", "INTEGER"),
    ("agenttask", "result_code", "VARCHAR"),
]
