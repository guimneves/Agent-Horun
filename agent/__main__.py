"""Ponto de entrada: `python -m agent`. Mesmo comando roda em desenvolvimento
(terminal aberto) e dentro do serviço do Windows instalado via NSSM (ver
install/README.md) — NSSM só chama esse mesmo comando em segundo plano."""

from __future__ import annotations

import sys

from agent.config import ConfigError, load_config
from agent.logging_setup import setup_logging
from agent.runner import run_forever


def main() -> int:
    logger = setup_logging()
    try:
        config = load_config()
    except ConfigError as exc:
        logger.error(str(exc))
        return 1

    try:
        run_forever(config)
    except KeyboardInterrupt:
        logger.info("encerrado (Ctrl+C)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
