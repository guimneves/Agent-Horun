"""Log simples em arquivo + console — o agente roda sem terminal aberto
(serviço do Windows via NSSM, ver install/README.md), então o arquivo de
log é a única forma de diagnosticar o que ele andou fazendo."""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(log_path: Path = Path("agent.log")) -> logging.Logger:
    logger = logging.getLogger("horun_agent")
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")

    file_handler = RotatingFileHandler(log_path, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(fmt)
    logger.addHandler(console_handler)

    return logger
