"""O loop principal: garante que a instalação está enrolada, depois fica
perguntando ao servidor "tem tarefa pra mim?" pra sempre — cada erro de
rede vira só um log e uma espera, nunca uma queda do processo (ele
precisa sobreviver dias/semanas rodando como serviço, sem ninguém
olhando)."""

from __future__ import annotations

import base64
import json
import logging
import time

from agent.client import ClientError, HorunAgentClient, Task
from agent.config import AgentConfig
from agent.tasks import TaskError, list_files, move_files, read_file, write_file

logger = logging.getLogger("horun_agent")


def ensure_enrolled(config: AgentConfig, client: HorunAgentClient) -> None:
    if config.enrolled:
        return
    if not config.enroll_code:
        raise RuntimeError(
            "instalação não enrolada e 'enroll_code' vazio no config.json — "
            "peça um código de uso único a um admin do módulo."
        )
    logger.info("enrolando instalação (device_name=%s)...", config.device_name)
    config.device_token = client.enroll(config.enroll_code, config.device_name)
    config.save()
    logger.info("enrolado com sucesso — device_token salvo em %s", config.path)


def execute_task(config: AgentConfig, task: Task) -> dict:
    """Devolve exatamente o corpo que vai em `POST .../result` (ver
    PROTOCOL.md) — nunca deixa uma exceção subir, sempre vira
    `{"ok": false, "error": ...}` se a tarefa falhar."""
    try:
        if task.op == "read_file":
            result = read_file(config, task.root, task.path or "")
            return {"ok": True, "content_base64": result.content_base64}
        if task.op == "write_file":
            write_file(config, task.root, task.path or "", task.content_base64 or "")
            return {"ok": True}
        if task.op == "list_files":
            result = list_files(config, task.root, task.glob or "*")
            return {"ok": True, "paths": result.paths}
        if task.op == "move_files":
            # os movimentos vêm em JSON dentro de content_base64 (campo que já
            # existe no protocolo) — assim um agente antigo, que não conhece
            # esta operação, só responde "operação desconhecida" em vez de
            # quebrar com um campo novo
            try:
                payload = json.loads(base64.b64decode(task.content_base64 or "", validate=True))
            except (ValueError, TypeError) as exc:
                raise TaskError(f"movimentos inválidos: {exc}") from exc
            result = move_files(config, task.root, payload.get("moves", []) if isinstance(payload, dict) else payload)
            return {"ok": True, "paths": result.paths}
        return {"ok": False, "error": f"operação desconhecida: {task.op!r}"}
    except TaskError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # nunca deixa uma tarefa individual matar o loop
        logger.exception("erro inesperado executando tarefa %s", task.id)
        return {"ok": False, "error": f"erro inesperado: {exc}"}


def run_forever(config: AgentConfig) -> None:
    client = HorunAgentClient(config.server_url)
    ensure_enrolled(config, client)

    logger.info(
        "agente em execução — servidor=%s, roots=%s, intervalo=%ss",
        config.server_url, sorted(config.roots), config.poll_interval_seconds,
    )
    while True:
        try:
            tasks = client.poll_tasks(config.device_token)
        except Exception as exc:  # noqa: BLE001 — nada na consulta pode derrubar o serviço
            logger.warning("consulta ao servidor falhou, tentando de novo em breve: %s", exc)
            time.sleep(config.poll_interval_seconds)
            continue

        for task in tasks:
            logger.info("executando tarefa %s (%s %s/%s)", task.id, task.op, task.root, task.path or task.glob)
            result = execute_task(config, task)
            try:
                client.report_result(config.device_token, task.id, result)
            except ClientError as exc:
                # o servidor vai reoferecer essa tarefa numa próxima consulta
                # se nunca receber o resultado — não é uma falha fatal aqui.
                logger.warning("falha ao reportar resultado de %s, será reofertada: %s", task.id, exc)

        time.sleep(config.poll_interval_seconds)
