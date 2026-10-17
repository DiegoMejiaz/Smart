"""Logging a consola + archivo rotativo."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-28s | %(message)s"


def setup_logging(level: str = "INFO", log_dir: str = "logs") -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(console)

    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            Path(log_dir) / "tipster.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(file_handler)
    except OSError as exc:  # p.ej. sistema de archivos de solo lectura
        root.warning("No se pudo crear el log en archivo (%s); solo consola", exc)

    # Librerías ruidosas
    for noisy in ("httpx", "httpx2", "telegram", "apscheduler", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
