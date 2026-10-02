"""Prazos e opções do lado servidor. Cada módulo ajusta na subida, ex.:

    from app.agent_server.settings import settings as agent_settings
    agent_settings.timeout_provider = lambda: settings.agent_task_timeout_seconds
    agent_settings.fail_fast_when_offline = True
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


@dataclass
class AgentServerSettings:
    # Quanto uma chamada espera o agente responder (vira também o prazo da
    # tarefa). `timeout_provider`, se definido, vence — para o módulo ler da
    # própria configuração (e os testes dele poderem mudar em tempo real).
    task_timeout_seconds: float = 30.0
    timeout_provider: Callable[[], float] | None = None
    # Agente consulta a cada poucos segundos; sem sinal por mais que isso,
    # está desligado ou sem rede.
    online_window_seconds: float = 120.0
    # Recusar na hora (em vez de esperar o prazo inteiro) quando nenhum
    # agente foi visto recentemente. Desligado por padrão: o RE7S sempre
    # esperou; o Financeiro liga.
    fail_fast_when_offline: bool = False
    # Tarefas resolvidas guardam conteúdo (base64) — apagadas depois disso.
    task_retention_days: int = 7
    cleanup_interval_seconds: float = 600.0
    # Código de enrolamento de uso único: vale por este tempo.
    enroll_code_ttl_minutes: int = 60
    # Leitura em pedaços (read_bytes com chunk_size): tamanho padrão.
    read_chunk_bytes: int = 4 * 1024 * 1024

    def timeout(self) -> float:
        return float(self.timeout_provider()) if self.timeout_provider else self.task_timeout_seconds


settings = AgentServerSettings()
