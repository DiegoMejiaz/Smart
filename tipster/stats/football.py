"""Estadísticas de fútbol vía API-Football (api-sports.io, v3).

Proporciona: promedios de goles local/visitante, forma reciente, H2H,
lesiones/sanciones y xG de los últimos partidos.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from tipster.analysis.matcher import team_similarity
from tipster.core.models import Event
from tipster.scrapers.http_client import FetchError, HttpClient

log = logging.getLogger(__name__)

BASE = "https://v3.football.api-sports.io"
PLAN_BLOCK_SECONDS = 24 * 3600
_disabled_until = 0.0  # compartido entre ciclos: evita gastar cuota si el plan no sirve


class PlanLimitError(FetchError):
    """El plan de API-Football no cubre lo pedido (temporada, cuota diaria...)."""


@dataclass
class TeamProfile:
    name: str
    goals_for_avg: float | None = None       # en su condición (local/visitante)
    goals_against_avg: float | None = None
    form: str = ""                            # últimos resultados, p.ej. "WWDLW"
    xg_for_avg: float | None = None
    xg_against_avg: float | None = None
    xg_sample: int = 0
    injuries: list[str] = field(default_factory=list)


@dataclass
class H2H:
    matches: int = 0
    home_wins: int = 0                        # victorias del local actual
    draws: int = 0
    away_wins: int = 0
    avg_goals: float = 0.0
    btts_rate: float = 0.0


@dataclass
class FootballMatchStats:
    home: TeamProfile
    away: TeamProfile
    h2h: H2H
    league_name: str = ""

    def expected_goals(self) -> tuple[float, float] | None:
        """Lambdas para Poisson mezclando goles reales y xG, ajustadas por bajas."""
        h, a = self.home, self.away
        if None in (h.goals_for_avg, h.goals_against_avg, a.goals_for_avg, a.goals_against_avg):
            return None
        lam_h = (h.goals_for_avg + a.goals_against_avg) / 2  # type: ignore[operator]
        lam_a = (a.goals_for_avg + h.goals_against_avg) / 2  # type: ignore[operator]
        if h.xg_for_avg is not None and a.xg_against_avg is not None:
            lam_h = 0.5 * lam_h + 0.5 * (h.xg_for_avg + a.xg_against_avg) / 2
        if a.xg_for_avg is not None and h.xg_against_avg is not None:
            lam_a = 0.5 * lam_a + 0.5 * (a.xg_for_avg + h.xg_against_avg) / 2
        # Cada baja resta ~3% de capacidad ofensiva (tope 12%). Heurística conservadora.
        lam_h *= 1 - min(0.03 * len(h.injuries), 0.12)
        lam_a *= 1 - min(0.03 * len(a.injuries), 0.12)
        return max(lam_h, 0.15), max(lam_a, 0.15)

    def facts(self) -> list[str]:
        out: list[str] = []
        h, a = self.home, self.away
        if h.goals_for_avg is not None:
            out.append(f"{h.name} promedia {h.goals_for_avg:.2f} goles a favor y {h.goals_against_avg:.2f} en contra como local esta temporada")
        if a.goals_for_avg is not None:
            out.append(f"{a.name} promedia {a.goals_for_avg:.2f} goles a favor y {a.goals_against_avg:.2f} en contra como visitante")
        for t in (h, a):
            if t.xg_for_avg is not None:
                out.append(f"{t.name}: xG medio {t.xg_for_avg:.2f} generado / {t.xg_against_avg:.2f} concedido en sus últimos {t.xg_sample} partidos")
            if t.form:
                out.append(f"Forma reciente de {t.name}: {t.form[-5:]}")
            if t.injuries:
                out.append(f"Bajas de {t.name}: {', '.join(t.injuries[:5])}")
        if self.h2h.matches:
            out.append(
                f"H2H últimos {self.h2h.matches}: {self.h2h.home_wins}V {h.name}, {self.h2h.draws}E, "
                f"{self.h2h.away_wins}V {a.name}; media de {self.h2h.avg_goals:.1f} goles y "
                f"ambos marcan en el {self.h2h.btts_rate:.0%}"
            )
        return out


class FootballStatsProvider:
    def __init__(self, http: HttpClient, api_key: str, xg_matches: int = 3, cache_ttl: int = 6 * 3600) -> None:
        self.http = http
        self.headers = {"x-apisports-key": api_key}
        self.xg_matches = xg_matches
        self.cache_ttl = cache_ttl
        self._cache: dict[str, tuple[float, Any]] = {}

    async def _get(self, path: str, **params: Any) -> Any:
        key = f"{path}?{sorted(params.items())}"
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < self.cache_ttl:
            return hit[1]
        data = await self.http.get(f"{BASE}{path}", params=params, headers=self.headers, throttle=False)
        errors = data.get("errors")
        if errors:
            text = str(errors)
            if isinstance(errors, dict) and ("plan" in errors or "requests" in errors):
                raise PlanLimitError(text)
            raise FetchError(f"API-Football {path}: {text}")
        resp = data.get("response", [])
        self._cache[key] = (time.monotonic(), resp)
        return resp

    @staticmethod
    def available() -> bool:
        return time.monotonic() >= _disabled_until

    async def get_match_stats(self, event: Event) -> FootballMatchStats | None:
        global _disabled_until
        if not self.available():
            return None
        try:
            fixture = await self._find_fixture(event)
            if fixture is None:
                log.info("API-Football: sin fixture para %s", event.name)
                return None
            league, season = fixture["league"]["id"], fixture["league"]["season"]
            home_t, away_t = fixture["teams"]["home"], fixture["teams"]["away"]

            home = await self._team_profile(home_t, league, season, "home")
            away = await self._team_profile(away_t, league, season, "away")
            await self._injuries(fixture["fixture"]["id"], home, away, home_t["id"])
            h2h = await self._h2h(home_t["id"], away_t["id"])
            return FootballMatchStats(home=home, away=away, h2h=h2h, league_name=fixture["league"]["name"])
        except PlanLimitError as exc:
            _disabled_until = time.monotonic() + PLAN_BLOCK_SECONDS
            log.warning("API-Football desactivado 24h: tu plan no lo permite (%s). El agente sigue "
                        "usando solo el mercado de referencia. Con un plan de pago se activa solo.", exc)
            return None
        except (FetchError, KeyError, TypeError, ValueError) as exc:
            log.warning("API-Football: error obteniendo stats de %s: %s", event.name, exc)
            return None

    async def _find_fixture(self, event: Event) -> dict[str, Any] | None:
        fixtures = await self._get("/fixtures", date=event.start_time.strftime("%Y-%m-%d"), timezone="UTC")
        best, best_score = None, 0.0
        for fx in fixtures:
            score = min(team_similarity(event.home, fx["teams"]["home"]["name"]),
                        team_similarity(event.away, fx["teams"]["away"]["name"]))
            if score > best_score:
                best, best_score = fx, score
        return best if best_score >= 80 else None

    async def _team_profile(self, team: dict[str, Any], league: int, season: int, venue: str) -> TeamProfile:
        prof = TeamProfile(name=team["name"])
        stats = await self._get("/teams/statistics", league=league, season=season, team=team["id"])
        if isinstance(stats, dict):  # este endpoint devuelve un objeto, no lista
            goals = stats.get("goals", {})
            gf = goals.get("for", {}).get("average", {}).get(venue)
            ga = goals.get("against", {}).get("average", {}).get(venue)
            prof.goals_for_avg = float(gf) if gf not in (None, "") else None
            prof.goals_against_avg = float(ga) if ga not in (None, "") else None
            prof.form = stats.get("form") or ""
        if self.xg_matches > 0:
            await self._fill_xg(prof, team["id"])
        return prof

    async def _fill_xg(self, prof: TeamProfile, team_id: int) -> None:
        xg_for, xg_against = [], []
        try:
            last = await self._get("/fixtures", team=team_id, last=self.xg_matches)
            for fx in last:
                rows = await self._get("/fixtures/statistics", fixture=fx["fixture"]["id"])
                for row in rows:
                    xg = next((s["value"] for s in row.get("statistics", []) if s.get("type") == "expected_goals"), None)
                    if xg in (None, ""):
                        continue
                    (xg_for if row["team"]["id"] == team_id else xg_against).append(float(xg))
        except (FetchError, KeyError, ValueError) as exc:
            log.debug("xG no disponible para %s: %s", prof.name, exc)
        if xg_for and xg_against:
            prof.xg_for_avg = sum(xg_for) / len(xg_for)
            prof.xg_against_avg = sum(xg_against) / len(xg_against)
            prof.xg_sample = min(len(xg_for), len(xg_against))

    async def _injuries(self, fixture_id: int, home: TeamProfile, away: TeamProfile, home_id: int) -> None:
        try:
            rows = await self._get("/injuries", fixture=fixture_id)
        except FetchError as exc:
            log.debug("Lesiones no disponibles: %s", exc)
            return
        for r in rows:
            label = f"{r['player']['name']} ({r['player'].get('reason') or r['player'].get('type')})"
            (home if r["team"]["id"] == home_id else away).injuries.append(label)

    async def _h2h(self, home_id: int, away_id: int, last: int = 8) -> H2H:
        h = H2H()
        try:
            rows = await self._get("/fixtures/headtohead", h2h=f"{home_id}-{away_id}", last=last)
        except FetchError:
            return h
        total_goals = btts = 0
        for r in rows:
            gh, ga = r["goals"]["home"], r["goals"]["away"]
            if gh is None or ga is None:
                continue
            h.matches += 1
            total_goals += gh + ga
            btts += gh > 0 and ga > 0
            winner_id = None if gh == ga else (r["teams"]["home"]["id"] if gh > ga else r["teams"]["away"]["id"])
            if winner_id is None:
                h.draws += 1
            elif winner_id == home_id:
                h.home_wins += 1
            else:
                h.away_wins += 1
        if h.matches:
            h.avg_goals = total_goals / h.matches
            h.btts_rate = btts / h.matches
        return h
