"""Modelos de dominio compartidos por todos los módulos."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Sport(str, Enum):
    FOOTBALL = "football"
    BASKETBALL = "basketball"
    TENNIS = "tennis"
    OTHER = "other"


class MarketType(str, Enum):
    MATCH_WINNER = "match_winner"   # 1X2 o moneyline
    TOTALS = "totals"               # más/menos
    HANDICAP = "handicap"           # hándicap / spread
    BTTS = "btts"                   # ambos marcan
    DOUBLE_CHANCE = "double_chance"


@dataclass(frozen=True)
class Selection:
    """Una opción apostable dentro de un mercado (p.ej. 'Más de 2.5')."""
    key: str                 # clave normalizada: home|draw|away|over|under|yes|no|1X|X2|12
    label: str               # texto original de la casa
    odds: float
    line: float | None = None  # 2.5 en totales, -1.5 en hándicap


@dataclass
class Market:
    type: MarketType
    name: str
    selections: list[Selection]
    line: float | None = None

    def overround(self) -> float:
        return sum(1.0 / s.odds for s in self.selections if s.odds > 1.0)


@dataclass
class Event:
    event_id: str
    source: str              # "betano", "odds_api"...
    sport: Sport
    competition: str
    home: str
    away: str
    start_time: datetime     # siempre timezone-aware (UTC)
    markets: list[Market] = field(default_factory=list)
    url: str | None = None

    @property
    def name(self) -> str:
        return f"{self.home} vs {self.away}"

    def market(self, mtype: MarketType, line: float | None = None) -> Market | None:
        for m in self.markets:
            if m.type == mtype and (line is None or m.line == line):
                return m
        return None


@dataclass
class Evidence:
    """Datos objetivos que respaldan un pick; alimentan al LLM y a los logs."""
    facts: list[str] = field(default_factory=list)
    metrics: dict[str, float | str] = field(default_factory=dict)


@dataclass
class Pick:
    event: Event
    market: Market
    selection: Selection
    model_probability: float
    implied_probability: float
    edge: float               # EV = p * cuota - 1
    stake: int                # 1-5
    pick_label: str           # texto del pronóstico ("Gana Real Madrid", "Más de 2.5 goles")
    evidence: Evidence
    justification: str = ""

    @property
    def dedupe_key(self) -> str:
        return f"{self.event.source}:{self.event.event_id}:{self.market.type.value}:{self.selection.key}:{self.selection.line}"
