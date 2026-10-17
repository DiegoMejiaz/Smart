"""Extractor de cuotas de Betano.

Betano es una SPA cuyo frontend consume JSON interno servido bajo el mismo
dominio: la ruta de cualquier página (p.ej. ``/sport/futbol/``) tiene un espejo
en ``/api/sport/futbol/`` que devuelve el estado de la página. Además, el HTML
inicial embebe ese mismo estado en ``window["initial_state"]``.

Estrategia (de más barata a más cara):
  1. API JSON interna  ->  2. HTML + initial_state  ->  3. Navegador headless (opcional)

El parser NO depende de una ruta fija dentro del JSON: recorre el árbol y
reconoce "objetos evento" por su forma (tienen ``markets`` y ``startTime``).
Así sobrevive a cambios menores de maquetación del sitio.

AVISO: los endpoints internos no son una API pública; pueden cambiar sin aviso
y su uso puede estar restringido por los Términos de Servicio de Betano en tu
jurisdicción. Verifícalo antes de operar en producción.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from tipster.core.models import Event, Market, MarketType, Selection, Sport
from tipster.scrapers.base import OddsProvider
from tipster.scrapers.http_client import FetchError, HttpClient

log = logging.getLogger(__name__)

API_QUERY = {"req": "la,s,stnf,c,mb"}  # mismos flags que usa el frontend
INITIAL_STATE_RE = re.compile(r'window\[["\']initial_state["\']\]\s*=\s*(\{.*?\})\s*;?\s*</script>', re.S)

SPORT_IDS = {
    "FOOT": Sport.FOOTBALL, "SOCC": Sport.FOOTBALL,
    "BASK": Sport.BASKETBALL,
    "TENN": Sport.TENNIS,
}
SPORT_SLUGS = {
    "futbol": Sport.FOOTBALL, "football": Sport.FOOTBALL, "fussball": Sport.FOOTBALL, "futebol": Sport.FOOTBALL,
    "baloncesto": Sport.BASKETBALL, "basquetbol": Sport.BASKETBALL, "basketball": Sport.BASKETBALL, "basquetebol": Sport.BASKETBALL,
    "tenis": Sport.TENNIS, "tennis": Sport.TENNIS, "tenis-de-campo": Sport.TENNIS,
}

# Códigos de tipo de mercado de Betano (cuando vienen en el JSON).
MARKET_CODES = {
    "MRES": MarketType.MATCH_WINNER,   # 1X2
    "H2HT": MarketType.MATCH_WINNER,   # ganador (sin empate)
    "MR12": MarketType.MATCH_WINNER,
    "HCTG": MarketType.TOTALS,         # total goles más/menos
    "OUHG": MarketType.TOTALS,
    "TPTS": MarketType.TOTALS,
    "TGOU": MarketType.TOTALS,
    "AHCP": MarketType.HANDICAP,
    "HCAP": MarketType.HANDICAP,
    "SPRD": MarketType.HANDICAP,
    "BTSC": MarketType.BTTS,
    "BTTS": MarketType.BTTS,
    "DBLC": MarketType.DOUBLE_CHANCE,
}
# Fallback por nombre (es/pt/en). El orden importa: lo más específico primero.
MARKET_NAME_PATTERNS: list[tuple[re.Pattern[str], MarketType]] = [
    (re.compile(r"ambos equipos marcan|ambas marcam|both teams to score", re.I), MarketType.BTTS),
    (re.compile(r"doble oportunidad|dupla hip|double chance", re.I), MarketType.DOUBLE_CHANCE),
    (re.compile(r"h[áa]ndicap|handicap|spread", re.I), MarketType.HANDICAP),
    (re.compile(r"m[áa]s/menos|mais/menos|over/under|total de (goles|puntos|juegos|gols)|^total", re.I), MarketType.TOTALS),
    (re.compile(r"resultado final|resultado do jogo|ganador del partido|ganador|vencedor|match result|1x2|moneyline", re.I), MarketType.MATCH_WINNER),
]
# Mercados de periodo/parciales que NO queremos (1ª parte, cuartos, sets...).
EXCLUDED_MARKET_RE = re.compile(r"1[ªa°]? ?(parte|mitad|tiempo|cuarto|set)|2[ªa°]? ?(parte|mitad|tiempo)|primer|segundo|half|quarter|set \d|córner|corner|tarjeta|card", re.I)
# Nombres alternativos de campos vistos en distintas versiones del frontend.
TIME_KEYS = ("startTime", "startTimeUtc", "startDate", "start", "kickOffTime", "date")
MARKET_KEYS = ("markets", "mainMarkets", "marketGroups")
SELECTION_KEYS = ("selections", "outcomes", "odds")
NUM_RE = re.compile(r"[-+]?\d+(?:[.,]\d+)?")


class BetanoScraper(OddsProvider):
    name = "betano"

    def __init__(
        self,
        http: HttpClient,
        base_url: str,
        sports: list[str],
        league_paths: list[str] | None = None,
        hours_ahead: int = 36,
        use_browser_fallback: bool = False,
        max_leagues: int = 5,
    ) -> None:
        self.http = http
        self.max_leagues = max_leagues
        self.base_url = base_url.rstrip("/")
        self.sports = sports
        self.league_paths = league_paths or []
        self.hours_ahead = hours_ahead
        self.use_browser_fallback = use_browser_fallback

    # ------------------------------------------------------------------ fetch
    def _page_paths(self) -> list[str]:
        paths = [f"/sport/{s.strip('/')}/" for s in self.sports]
        paths += [p if p.startswith("/") else f"/{p}" for p in self.league_paths]
        return paths

    async def fetch_events(self) -> list[Event]:
        events: dict[str, Event] = {}
        pending = self._page_paths()
        visited: set[str] = set()
        while pending:
            path = pending.pop(0)
            if path in visited:
                continue
            visited.add(path)
            sport = _sport_from_path(path) or Sport.OTHER

            # La portada de un deporte es un menú: se reutiliza su lista de ligas (24h)
            # para no gastar una petición por ciclo.
            cached = _LEAGUE_CACHE.get(path)
            if cached and time.monotonic() - cached[0] < LEAGUE_CACHE_TTL:
                pending.extend(p for p in cached[1] if p not in visited)
                continue

            try:
                payload = await self._fetch_page_state(path)
            except FetchError as exc:
                log.error("Betano: no se pudo extraer %s: %s", path, exc)
                continue
            parsed = self.parse_payload(payload, default_sport=sport)
            if parsed:
                log.info("Betano %s: %d eventos parseados", path, len(parsed))
                for ev in parsed:
                    events.setdefault(ev.event_id, ev)
                continue

            leagues = extract_top_league_paths(payload, self.base_url)[: self.max_leagues]
            if leagues:
                _LEAGUE_CACHE[path] = (time.monotonic(), leagues)
                log.info("Betano %s: página de navegación; siguiendo %d ligas destacadas: %s",
                         path, len(leagues), ", ".join(leagues))
                pending.extend(p for p in leagues if p not in visited)
            else:
                log.warning("Betano %s: estructura no reconocida. Resumen para diagnóstico:\n%s",
                            path, describe_payload(payload))

        horizon = datetime.now(timezone.utc) + timedelta(hours=self.hours_ahead)
        result = [e for e in events.values() if e.start_time <= horizon]
        log.info("Betano: %d eventos únicos dentro de %dh", len(result), self.hours_ahead)
        return result

    async def _fetch_page_state(self, path: str) -> Any:
        referer = f"{self.base_url}{path}"
        # 1) API JSON interna
        try:
            return await self.http.get(f"{self.base_url}/api{path}", params=API_QUERY, referer=referer,
                                       unblocker=True)
        except FetchError as exc:
            log.warning("Betano API JSON falló para %s (%s); probando HTML", path, exc)

        # 2) HTML con initial_state
        try:
            html = await self.http.get(
                referer, expect_json=False, headers={"Accept": "text/html,application/xhtml+xml"},
                unblocker=True,
            )
            state = extract_initial_state(html)
            if state is not None:
                return state
            log.warning("Betano: initial_state no encontrado en %s", path)
        except FetchError as exc:
            log.warning("Betano HTML falló para %s (%s)", path, exc)

        # 3) Navegador headless (opcional, más lento)
        if self.use_browser_fallback:
            from tipster.scrapers.browser import fetch_initial_state_with_browser

            state = await fetch_initial_state_with_browser(referer)
            if state is not None:
                return state
        raise FetchError(f"Todas las estrategias fallaron para {path}")

    # ------------------------------------------------------------------ parse
    def parse_payload(self, payload: Any, default_sport: Sport = Sport.OTHER) -> list[Event]:
        events: list[Event] = []
        for raw in _iter_event_dicts(payload):
            try:
                ev = self._parse_event(raw, default_sport)
            except Exception as exc:  # un evento corrupto no debe tumbar el ciclo
                log.debug("Betano: evento descartado (%s): %s", exc, str(raw)[:200])
                continue
            if ev and ev.markets:
                events.append(ev)
        return events

    def _parse_event(self, raw: dict[str, Any], default_sport: Sport) -> Event | None:
        if raw.get("liveNow") or raw.get("isLive") or raw.get("live"):
            return None  # solo pre-partido

        home, away = _participants(raw)
        if not home or not away:
            return None

        start = _parse_time(next((raw[k] for k in TIME_KEYS if raw.get(k) is not None), None))
        if start is None or start < datetime.now(timezone.utc):
            return None

        sport = SPORT_IDS.get(str(raw.get("sportId", "")).upper()) or _sport_from_path(str(raw.get("url", ""))) or default_sport
        competition = " - ".join(
            x for x in (raw.get("regionName"), raw.get("leagueName") or raw.get("leagueDescription")) if x
        ) or "Desconocida"

        markets: list[Market] = []
        for m in _first_list(raw, MARKET_KEYS):
            markets.extend(self._parse_market(m, home, away, sport))

        url = raw.get("url")
        return Event(
            event_id=str(raw.get("id") or raw.get("betRadarId") or f"{home}-{away}-{start.isoformat()}"),
            source=self.name,
            sport=sport,
            competition=competition,
            home=home,
            away=away,
            start_time=start,
            markets=markets,
            url=f"{self.base_url}{url}" if url and url.startswith("/") else url,
        )

    def _parse_market(self, m: dict[str, Any], home: str, away: str, sport: Sport) -> list[Market]:
        name = str(m.get("name") or m.get("marketName") or "")
        if EXCLUDED_MARKET_RE.search(name):
            return []
        mtype = MARKET_CODES.get(str(m.get("type", "")).upper())
        if mtype is None:
            mtype = next((t for rx, t in MARKET_NAME_PATTERNS if rx.search(name)), None)
        if mtype is None:
            return []

        by_line: dict[float | None, list[Selection]] = defaultdict(list)
        for s in _first_list(m, SELECTION_KEYS):
            sel = _parse_selection(s, mtype, home, away, m.get("handicap"))
            if sel is not None:
                by_line[sel.line if mtype in (MarketType.TOTALS, MarketType.HANDICAP) else None].append(sel)

        markets = []
        for line, sels in by_line.items():
            if mtype == MarketType.HANDICAP:
                # Línea del mercado expresada desde el local.
                home_sel = next((s for s in sels if s.key == "home"), None)
                line = home_sel.line if home_sel else line
            if _is_complete(mtype, sels, sport):
                markets.append(Market(type=mtype, name=name, selections=sels, line=line))
        return markets


# ---------------------------------------------------------------------- helpers
def extract_initial_state(html: str) -> Any | None:
    match = INITIAL_STATE_RE.search(html)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        log.warning("initial_state no es JSON válido: %s", exc)
        return None


LEAGUE_CACHE_TTL = 24 * 3600
_LEAGUE_CACHE: dict[str, tuple[float, list[str]]] = {}


def extract_top_league_paths(payload: Any, base_url: str = "") -> list[str]:
    """Rutas de las ligas destacadas (``topLeagues``) de una portada de deporte."""
    data = payload.get("data", payload) if isinstance(payload, dict) else {}
    leagues = data.get("topLeagues") if isinstance(data, dict) else None
    if not isinstance(leagues, list):
        return []
    paths = []
    for lg in leagues:
        url = lg.get("url") if isinstance(lg, dict) else None
        if not url:
            continue
        if url.startswith("http"):
            url = url.replace(base_url, "", 1) if base_url and url.startswith(base_url) else ""
        if url.startswith("/") and url not in paths:
            paths.append(url)
    return paths


def _first_list(d: dict[str, Any], keys: tuple[str, ...]) -> list[Any]:
    for k in keys:
        if isinstance(d.get(k), list):
            return d[k]
    return []


def describe_payload(payload: Any, max_chars: int = 1800) -> str:
    """Resumen compacto de la forma del JSON (claves y ejemplos) para ajustar el parser."""
    lines: list[str] = []
    if isinstance(payload, dict):
        lines.append(f"tipo=dict claves={list(payload)[:25]}")
    elif isinstance(payload, list):
        lines.append(f"tipo=list len={len(payload)}")
    else:
        lines.append(f"tipo={type(payload).__name__} inicio={str(payload)[:300]!r}")

    # Rutas a listas de dicts y las claves de su primer elemento (lo que suele ser eventos/mercados).
    seen = 0
    stack: list[tuple[str, Any]] = [("$", payload)]
    while stack and seen < 15:
        path, node = stack.pop()
        if isinstance(node, dict):
            for k, v in list(node.items())[:40]:
                stack.append((f"{path}.{k}", v))
        elif isinstance(node, list) and node:
            first = node[0]
            if isinstance(first, dict):
                lines.append(f"{path}[{len(node)}] -> claves {list(first)[:20]}")
                seen += 1
                stack.append((f"{path}[0]", first))
    text = "\n".join(lines)
    return text[:max_chars]


def _iter_event_dicts(node: Any) -> Iterator[dict[str, Any]]:
    """Recorre el JSON y produce cualquier dict con forma de evento."""
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if any(isinstance(cur.get(k), list) for k in MARKET_KEYS) and any(k in cur for k in TIME_KEYS):
                yield cur
                continue
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def _participants(raw: dict[str, Any]) -> tuple[str | None, str | None]:
    parts = raw.get("participants") or []
    names = [p.get("name") for p in parts if isinstance(p, dict) and p.get("name")]
    if len(names) >= 2:
        return names[0].strip(), names[1].strip()
    name = str(raw.get("name") or raw.get("shortName") or "")
    for sep in (" - ", " vs ", " vs. ", " v "):
        if sep in name:
            a, b = name.split(sep, 1)
            return a.strip(), b.strip()
    return None, None


def _parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = value / 1000 if value > 1e11 else value  # Betano usa milisegundos
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _sport_from_path(path: str) -> Sport | None:
    low = path.lower()
    for slug, sport in SPORT_SLUGS.items():
        if f"/{slug}" in low:
            return sport
    return None


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except ValueError:
        return None


def _parse_selection(s: dict[str, Any], mtype: MarketType, home: str, away: str, market_handicap: Any) -> Selection | None:
    odds = _to_float(s.get("price") or s.get("odds") or s.get("decimal") or s.get("odd") or s.get("value"))
    if odds is None or odds <= 1.0:
        return None
    label = str(s.get("name") or s.get("fullName") or "").strip()
    full = str(s.get("fullName") or label)
    low = label.lower()
    line = _to_float(s.get("handicap"))
    if line is None:
        nums = NUM_RE.findall(label)
        line = _to_float(nums[-1]) if nums else _to_float(market_handicap)

    key: str | None = None
    if mtype == MarketType.MATCH_WINNER:
        if low in ("1", "local", "home") or _same_team(full, home):
            key = "home"
        elif low in ("x", "empate", "draw"):
            key = "draw"
        elif low in ("2", "visitante", "away") or _same_team(full, away):
            key = "away"
        line = None
    elif mtype == MarketType.TOTALS:
        if low.startswith(("más", "mas", "over", "mais", "+")):
            key = "over"
        elif low.startswith(("menos", "under", "-")):
            key = "under"
    elif mtype == MarketType.HANDICAP:
        if low.startswith("1") or _same_team(full, home) or _same_team(label, home):
            key = "home"
        elif low.startswith("2") or _same_team(full, away) or _same_team(label, away):
            key = "away"
    elif mtype == MarketType.BTTS:
        key = {"sí": "yes", "si": "yes", "sim": "yes", "yes": "yes", "no": "no", "não": "no"}.get(low)
        line = None
    elif mtype == MarketType.DOUBLE_CHANCE:
        key = {"1X": "1X", "X2": "X2", "12": "12"}.get(re.sub(r"[^12X]", "", label.upper()))
        line = None

    if key is None:
        return None
    return Selection(key=key, label=label, odds=round(odds, 3), line=line)


def _same_team(candidate: str, team: str) -> bool:
    a, b = candidate.lower().strip(), team.lower().strip()
    return bool(a) and (a == b or a.startswith(b) or b.startswith(a))


def _is_complete(mtype: MarketType, sels: list[Selection], sport: Sport) -> bool:
    keys = {s.key for s in sels}
    if mtype == MarketType.MATCH_WINNER:
        return {"home", "away"} <= keys and (sport != Sport.FOOTBALL or "draw" in keys)
    if mtype == MarketType.TOTALS:
        return keys == {"over", "under"}
    if mtype == MarketType.HANDICAP:
        return keys == {"home", "away"}
    if mtype == MarketType.BTTS:
        return keys == {"yes", "no"}
    if mtype == MarketType.DOUBLE_CHANCE:
        return len(keys) >= 1
    return False
