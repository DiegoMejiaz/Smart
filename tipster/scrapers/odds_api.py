"""Cuotas de referencia desde The Odds API (https://the-odds-api.com).

Se usan para validar el mercado: las casas "sharp" (Pinnacle, exchanges) tienen
márgenes bajos y precios eficientes; su probabilidad sin margen (de-vig) es el
mejor estimador público de la probabilidad real de un resultado.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from tipster.core.models import Event, Market, MarketType, Selection, Sport
from tipster.scrapers.base import OddsProvider
from tipster.scrapers.http_client import FetchError, HttpClient

log = logging.getLogger(__name__)

BASE_URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds"
MARKET_MAP = {"h2h": MarketType.MATCH_WINNER, "totals": MarketType.TOTALS, "spreads": MarketType.HANDICAP}


def sport_from_odds_key(key: str) -> Sport:
    if key.startswith("soccer"):
        return Sport.FOOTBALL
    if key.startswith("basketball"):
        return Sport.BASKETBALL
    if key.startswith("tennis"):
        return Sport.TENNIS
    return Sport.OTHER


class OddsApiProvider(OddsProvider):
    """Devuelve un Event por (partido, casa) para poder calcular consenso."""
    name = "odds_api"

    def __init__(self, http: HttpClient, api_key: str, sport_keys: list[str], regions: str = "eu",
                 bookmakers: list[str] | None = None) -> None:
        self.http = http
        self.api_key = api_key
        self.sport_keys = sport_keys
        self.regions = regions
        self.bookmakers = bookmakers or []

    async def fetch_events(self) -> list[Event]:
        out: list[Event] = []
        for sport_key in self.sport_keys:
            params = {
                "apiKey": self.api_key,
                "markets": "h2h,totals,spreads",
                "oddsFormat": "decimal",
                "dateFormat": "iso",
            }
            if self.bookmakers:
                params["bookmakers"] = ",".join(self.bookmakers)
            else:
                params["regions"] = self.regions
            try:
                data = await self.http.get(BASE_URL.format(sport=sport_key), params=params, throttle=False)
            except FetchError as exc:
                log.error("Odds API %s falló: %s", sport_key, exc)
                continue
            if not isinstance(data, list):
                log.error("Odds API %s: respuesta inesperada %s", sport_key, str(data)[:200])
                continue
            for item in data:
                out.extend(self._parse(item, sport_key))
            log.info("Odds API %s: %d partidos", sport_key, len(data))
        return out

    def _parse(self, item: dict[str, Any], sport_key: str) -> list[Event]:
        home, away = item.get("home_team"), item.get("away_team")
        if not home or not away:
            return []
        start = datetime.fromisoformat(item["commence_time"].replace("Z", "+00:00")).astimezone(timezone.utc)
        events = []
        for bm in item.get("bookmakers", []):
            markets: list[Market] = []
            for m in bm.get("markets", []):
                mtype = MARKET_MAP.get(m.get("key"))
                if mtype is None:
                    continue
                markets.extend(_parse_market(mtype, m.get("outcomes", []), home, away))
            if markets:
                events.append(Event(
                    event_id=f"{item['id']}:{bm['key']}",
                    source=f"{self.name}:{bm['key']}",
                    sport=sport_from_odds_key(sport_key),
                    competition=item.get("sport_title", sport_key),
                    home=home, away=away, start_time=start, markets=markets,
                ))
        return events


def _parse_market(mtype: MarketType, outcomes: list[dict[str, Any]], home: str, away: str) -> list[Market]:
    if mtype == MarketType.MATCH_WINNER:
        sels = []
        for o in outcomes:
            key = "home" if o["name"] == home else "away" if o["name"] == away else "draw"
            sels.append(Selection(key=key, label=o["name"], odds=float(o["price"])))
        return [Market(mtype, "h2h", sels)]

    grouped: dict[float, list[Selection]] = {}
    for o in outcomes:
        point = o.get("point")
        if point is None:
            continue
        if mtype == MarketType.TOTALS:
            key = "over" if o["name"].lower() == "over" else "under"
            grouped.setdefault(float(point), []).append(Selection(key, o["name"], float(o["price"]), float(point)))
        else:
            key = "home" if o["name"] == home else "away"
            home_line = float(point) if key == "home" else -float(point)
            grouped.setdefault(home_line, []).append(Selection(key, o["name"], float(o["price"]), float(point)))
    return [Market(mtype, mtype.value, sels, line) for line, sels in grouped.items() if len(sels) == 2]
