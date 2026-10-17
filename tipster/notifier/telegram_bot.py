"""Publicación de señales en un canal de Telegram (python-telegram-bot)."""
from __future__ import annotations

import asyncio
import logging

from telegram import Bot, LinkPreviewOptions
from telegram.error import Forbidden, NetworkError, RetryAfter, TelegramError, TimedOut

log = logging.getLogger(__name__)


class TelegramPublisher:
    def __init__(self, token: str, channel_id: str, dry_run: bool = False, max_attempts: int = 4) -> None:
        self.bot = Bot(token=token)
        self.channel_id = channel_id
        self.dry_run = dry_run
        self.max_attempts = max_attempts

    async def check(self) -> bool:
        """Valida el token al arrancar."""
        if self.dry_run:
            return True
        try:
            me = await self.bot.get_me()
            log.info("Telegram conectado como @%s -> canal %s", me.username, self.channel_id)
            return True
        except TelegramError as exc:
            hint = ""
            if "not found" in str(exc).lower() or "unauthorized" in str(exc).lower():
                hint = (" -> Telegram no reconoce este token: cópialo de nuevo desde @BotFather "
                        "(/mybots → tu bot → API Token) o genera uno nuevo con /revoke")
            log.error("Token de Telegram inválido o sin red: %s%s", exc, hint)
            return False

    async def send(self, text: str) -> bool:
        if self.dry_run:
            log.info("[DRY-RUN] Señal:\n%s", text)
            return True
        for attempt in range(1, self.max_attempts + 1):
            try:
                msg = await self.bot.send_message(
                    chat_id=self.channel_id, text=text, link_preview_options=LinkPreviewOptions(is_disabled=True)
                )
                log.info("Señal publicada (message_id=%s)", msg.message_id)
                return True
            except RetryAfter as exc:
                ra = exc.retry_after
                wait = ra.total_seconds() if hasattr(ra, "total_seconds") else float(ra)
                log.warning("Telegram flood control: esperando %.0fs", wait)
                await asyncio.sleep(wait + 1)
            except (TimedOut, NetworkError) as exc:
                log.warning("Telegram intento %d/%d falló: %s", attempt, self.max_attempts, exc)
                await asyncio.sleep(2 ** attempt)
            except Forbidden as exc:
                log.error("El bot no tiene permiso en %s (¿es administrador del canal?): %s", self.channel_id, exc)
                return False
            except TelegramError as exc:
                log.error("Error de Telegram no recuperable: %s", exc)
                return False
        return False
