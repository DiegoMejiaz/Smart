"""Punto de entrada.

    python -m tipster            # bucle continuo (Docker / systemd)
    python -m tipster --once     # un solo ciclo (cron)
    python -m tipster --dry-run  # no publica en Telegram, solo loguea
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from pydantic import ValidationError

from tipster.agent import TipsterAgent
from tipster.core.config import get_settings
from tipster.core.logging_setup import setup_logging


def main() -> int:
    parser = argparse.ArgumentParser(description="Tipster AI - agente de value betting para Betano")
    parser.add_argument("--once", action="store_true", help="ejecuta un único ciclo y termina")
    parser.add_argument("--dry-run", action="store_true", help="no publica en Telegram")
    args = parser.parse_args()

    try:
        settings = get_settings()
    except ValidationError as exc:
        print(f"Configuración inválida, revisa tu .env:\n{exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        settings.telegram_dry_run = True

    setup_logging(settings.log_level, settings.log_dir)
    log = logging.getLogger("tipster")

    agent = TipsterAgent(settings)
    try:
        if args.once:
            asyncio.run(agent.run_cycle())
        else:
            asyncio.run(agent.run_forever())
    except KeyboardInterrupt:
        log.info("Interrumpido por el usuario")
    return 0


if __name__ == "__main__":
    sys.exit(main())
