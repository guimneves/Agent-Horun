"""O loop principal: garante que cada servidor configurado está enrolado,
depois fica perguntando a cada um "tem tarefa pra mim?" pra sempre — cada
erro de rede vira só um log e uma espera, nunca uma queda do processo (ele
precisa sobreviver dias/semanas rodando como serviço, sem ninguém olhando).
Um servidor fora do ar ou com token revogado não atrapalha os outros."""

from __future__ import annotations

import base64
import json
import logging
import time

from agent.client import ClientError, HorunAgentClient, Task
from agent.config import AgentConfig, ServerConfig
from agent.tasks import TaskError, list_files, list_tree, move_files, read_file, write_file

logger = logging.getLogger("horun_agent")


def ensure_enrolled(config: AgentConfig, server: ServerConfig, client: HorunAgentClient) -> bool:
    """True se o servidor está pronto pra uso. Sem token e sem código, avisa
    e pula esse servidor (os outros continuam)."""
    if server.enrolled:
        return True
    if not server.enroll_code:
        logger.error(
            "servidor %s sem device_token e sem enroll_code no config.json — peça um código "
            "de uso único a um admin daquele módulo.",
            server.url,
        )
        return False
    logger.info("enrolando em %s (device_name=%s)...", server.url, config.device_name)
    server.device_token = client.enroll(server.enroll_code, config.device_name)
    config.save()
    logger.info("enrolado em %s — device_token salvo em %s", server.url, config.path)
    return True


def _decode_moves(task: Task) -> list:
    # os movimentos vêm em JSON dentro de content_base64 (campo que já existe
    # no protocolo) — assim um agente antigo, que não conhece a operação, só
    # responde "operação desconhecida" em vez de quebrar com um campo novo
    try:
        payload = json.loads(base64.b64decode(task.content_base64 or "", validate=True))
    except (ValueError, TypeError) as exc:
        raise TaskError(f"movimentos inválidos: {exc}") from exc
    return payload.get("moves", []) if isinstance(payload, dict) else payload


def execute_task(server: ServerConfig, task: Task) -> dict:
    """Devolve exatamente o corpo que vai em `POST .../result` (ver
    PROTOCOL.md) — nunca deixa uma exceção subir, sempre vira
    `{"ok": false, "error": ..., "code": ...}` se a tarefa falhar."""
    args = task.args or {}
    try:
        if task.op == "read_file":
            result = read_file(
                server, task.root, task.path or "",
                offset=int(args.get("offset", 0)),
                length=int(args["length"]) if args.get("length") is not None else None,
            )
            return {"ok": True, "content_base64": result.content_base64, "size": result.size}
        if task.op == "write_file":
            write_file(server, task.root, task.path or "", task.content_base64 or "")
            return {"ok": True}
        if task.op == "list_files":
            result = list_files(server, task.root, task.glob or "*")
            return {"ok": True, "paths": result.paths}
        if task.op == "list_tree":
            entries = list_tree(server, task.root, task.path or "", recursive=bool(args.get("recursive", True)))
            return {
                "ok": True,
                "entries": [{"path": e.path, "is_dir": e.is_dir, "size": e.size} for e in entries],
            }
        if task.op == "move_files":
            result = move_files(server, task.root, _decode_moves(task))
            return {"ok": True, "paths": result.paths}
        return {"ok": False, "error": f"operação desconhecida: {task.op!r}", "code": "unknown_op"}
    except TaskError as exc:
        result = {"ok": False, "error": str(exc)}
        if exc.code:
            result["code"] = exc.code
        return result
    except Exception as exc:  # nunca deixa uma tarefa individual matar o loop
        logger.exception("erro inesperado executando tarefa %s", task.id)
        return {"ok": False, "error": f"erro inesperado: {exc}"}


def serve_once(config: AgentConfig, server: ServerConfig, client: HorunAgentClient) -> None:
    """Uma rodada para UM servidor: enrola se preciso, pega as tarefas,
    executa e reporta. Nada aqui derruba o agente."""
    try:
        if not ensure_enrolled(config, server, client):
            return
        tasks = client.poll_tasks(server.device_token)
    except Exception as exc:  # noqa: BLE001 — nada na consulta pode derrubar o serviço
        logger.warning("servidor %s: consulta falhou, tentando de novo em breve: %s", server.url, exc)
        return

    for task in tasks:
        logger.info(
            "servidor %s: tarefa %s (%s %s/%s)", server.url, task.id, task.op, task.root, task.path or task.glob
        )
        result = execute_task(server, task)
        try:
            client.report_result(server.device_token, task.id, result)
        except ClientError as exc:
            # o servidor vai reoferecer essa tarefa numa próxima consulta se
            # nunca receber o resultado (dentro do prazo dela)
            logger.warning("falha ao reportar resultado de %s, será reofertada: %s", task.id, exc)


def run_forever(config: AgentConfig) -> None:
    if not any(s.enrolled or s.enroll_code for s in config.servers):
        raise RuntimeError(
            "nenhum servidor no config.json tem device_token nem enroll_code — "
            "peça um código de uso único a um admin do módulo."
        )
    clients = {s.url: HorunAgentClient(s.url) for s in config.servers}
    for s in config.servers:
        logger.info(
            "atendendo %s — pastas: %s",
            s.url, ", ".join(f"{n} ({r.mode})" for n, r in sorted(s.roots.items())),
        )
    logger.info("agente em execução — intervalo=%ss", config.poll_interval_seconds)
    while True:
        for server in config.servers:
            serve_once(config, server, clients[server.url])
        time.sleep(config.poll_interval_seconds)
