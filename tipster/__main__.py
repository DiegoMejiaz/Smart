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
from pydantic_settings import SettingsError

from tipster.agent import TipsterAgent
from tipster.core.config import get_settings
from tipster.core.logging_setup import setup_logging


def main() -> int:
    parser = argparse.ArgumentParser(description="Tipster AI - agente de value betting para Betano")
    parser.add_argument("--once", action="store_true", help="ejecuta un único ciclo y termina")
    parser.add_argument("--dry-run", action="store_true", help="no publica en Telegram")
    parser.add_argument("--test-signal", action="store_true", help="publica una señal de prueba y termina")
    args = parser.parse_args()

    try:
        settings = get_settings()
    except ValidationError as exc:
        # Sin valores de entrada: nunca imprimir tokens/claves en los logs.
        lines = []
        for err in exc.errors(include_input=False, include_url=False):
            field = ".".join(str(p) for p in err["loc"]).upper()
            msg = "falta la variable" if err["type"] == "missing" else err["msg"]
            lines.append(f"  - {field}: {msg}")
        print("Configuración inválida, revisa tus variables de entorno:\n" + "\n".join(lines), file=sys.stderr)
        return 2
    except SettingsError as exc:
        print(f"Configuración inválida: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        settings.telegram_dry_run = True

    setup_logging(settings.log_level, settings.log_dir)
    log = logging.getLogger("tipster")

    agent = TipsterAgent(settings)
    try:
        if args.test_signal:
            asyncio.run(agent.send_test_signal())
        elif args.once:
            asyncio.run(agent.run_cycle())
        else:
            asyncio.run(agent.run_forever())
    except KeyboardInterrupt:
        log.info("Interrumpido por el usuario")
    return 0


if __name__ == "__main__":
    sys.exit(main())
