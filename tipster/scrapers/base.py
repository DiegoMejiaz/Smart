"""Interfaz común para cualquier fuente de cuotas."""
from __future__ import annotations

import abc

from tipster.core.models import Event


class OddsProvider(abc.ABC):
    name: str

    @abc.abstractmethod
    async def fetch_events(self) -> list[Event]:
        """Devuelve eventos pre-partido con sus mercados normalizados."""
