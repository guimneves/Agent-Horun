"""Cliente HTTP do agente — só chama pra fora, nunca escuta nada (ver
PROTOCOL.md). Uma classe fininha em cima do httpx, pra `runner.py` não
precisar saber o formato exato de cada chamada."""

from __future__ import annotations

from dataclasses import dataclass

import httpx


class ClientError(RuntimeError):
    """Erro de comunicação com o servidor — trate como "tenta de novo
    depois", nunca como motivo pra derrubar o agente."""


@dataclass
class Task:
    id: str
    op: str  # "read_file" | "write_file" | "list_files"
    root: str
    path: str | None
    content_base64: str | None
    glob: str | None


class HorunAgentClient:
    def __init__(self, server_url: str, timeout: float = 15.0):
        self.server_url = server_url.rstrip("/")
        self._timeout = timeout

    def enroll(self, enroll_code: str, device_name: str) -> str:
        try:
            r = httpx.post(
                f"{self.server_url}/agent/enroll",
                json={"enroll_code": enroll_code, "device_name": device_name},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise ClientError(f"falha ao enrolar: {exc}") from exc
        if r.status_code != 200:
            raise ClientError(f"enrolamento recusado pelo servidor ({r.status_code}): {r.text}")
        token = r.json().get("device_token")
        if not token:
            raise ClientError("resposta de enrolamento sem device_token")
        return token

    def poll_tasks(self, device_token: str) -> list[Task]:
        try:
            r = httpx.get(
                f"{self.server_url}/agent/tasks",
                headers={"Authorization": f"Bearer {device_token}"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise ClientError(f"falha ao consultar tarefas: {exc}") from exc
        if r.status_code != 200:
            raise ClientError(f"consulta de tarefas recusada ({r.status_code}): {r.text}")
        return [Task(**t) for t in r.json().get("tasks", [])]

    def report_result(self, device_token: str, task_id: str, result: dict) -> None:
        try:
            r = httpx.post(
                f"{self.server_url}/agent/tasks/{task_id}/result",
                headers={"Authorization": f"Bearer {device_token}"},
                json=result,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise ClientError(f"falha ao reportar resultado de {task_id}: {exc}") from exc
        if r.status_code != 200:
            raise ClientError(f"servidor recusou resultado de {task_id} ({r.status_code}): {r.text}")
