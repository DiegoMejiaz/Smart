"""Orquestador: un ciclo = extraer -> validar -> analizar -> justificar -> publicar."""
from __future__ import annotations

import asyncio
import logging
import signal
import time
from datetime import datetime, timedelta, timezone

from tipster.analysis.matcher import find_matches
from tipster.analysis.value_engine import EngineConfig, ValueEngine
from tipster.core.config import Settings
from tipster.core.models import Event, Evidence, Market, MarketType, Pick, Selection, Sport
from tipster.llm.justifier import Justifier
from tipster.notifier.formatter import format_signal
from tipster.notifier.telegram_bot import TelegramPublisher
from tipster.scrapers.betano import BetanoScraper
from tipster.scrapers.http_client import HttpClient
from tipster.scrapers.odds_api import OddsApiProvider
from tipster.stats.football import FootballStatsProvider
from tipster.storage.db import SignalStore

log = logging.getLogger(__name__)


class TipsterAgent:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.engine = ValueEngine(EngineConfig(
            min_odds=settings.min_odds, max_odds=settings.max_odds, min_edge=settings.min_edge,
            min_probability=settings.min_probability, max_overround_reference=settings.max_overround_reference,
            kelly_fraction=settings.kelly_fraction,
        ))
        self.justifier = Justifier(settings.llm_model, settings.llm_effort, settings.llm_enabled,
                                   settings.anthropic_api_key)
        self.publisher = TelegramPublisher(settings.telegram_bot_token, settings.telegram_channel_id,
                                           settings.telegram_dry_run)
        self.store = SignalStore(settings.db_path)
        self._stop = asyncio.Event()

    # ----------------------------------------------------------------- loop
    async def run_forever(self) -> None:
        self._install_signal_handlers()
        if not await self.publisher.check():
            raise SystemExit("No se pudo validar Telegram; revisa TELEGRAM_BOT_TOKEN")
        if self.s.send_test_signal_on_start:
            await self.send_test_signal()
        log.info("Agente iniciado. Intervalo: %ss | Cuotas %.2f-%.2f | EV mín %.1f%%",
                 self.s.poll_interval_seconds, self.s.min_odds, self.s.max_odds, self.s.min_edge * 100)
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                await self.run_cycle()
            except Exception:  # el bucle nunca debe morir por un ciclo fallido
                log.exception("Ciclo fallido; se reintentará en el siguiente intervalo")
            elapsed = time.monotonic() - started
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(self.s.poll_interval_seconds - elapsed, 5))
            except asyncio.TimeoutError:
                pass
        self.store.close()
        log.info("Agente detenido limpiamente")

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._stop.set)
            except NotImplementedError:  # Windows
                pass

    async def send_test_signal(self) -> bool:
        """Publica una señal ficticia y marcada como prueba para verificar Telegram + LLM."""
        pick = build_test_pick()
        pick.justification = await self.justifier.justify(pick)
        text = "🧪 MENSAJE DE PRUEBA — datos ficticios, no apostar\n\n" + format_signal(pick, self.s.timezone)
        ok = await self.publisher.send(text)
        log.info("Señal de prueba %s", "publicada" if ok else "NO publicada")
        return ok

    # ---------------------------------------------------------------- cycle
    async def run_cycle(self) -> list[Pick]:
        cycle_start = time.monotonic()
        sent_today = self.store.count_today()
        if sent_today >= self.s.max_picks_per_day:
            log.info("Límite diario alcanzado (%d); ciclo omitido", sent_today)
            return []

        async with HttpClient(self.s.proxies, self.s.impersonate_browsers, self.s.request_timeout,
                              self.s.request_max_retries, self.s.min_request_delay, self.s.max_request_delay) as http:
            betano = BetanoScraper(http, self.s.betano_base_url, self.s.betano_sports, self.s.betano_league_paths,
                                   self.s.hours_ahead, self.s.betano_use_browser_fallback)
            events = await betano.fetch_events()
            if not events:
                log.warning("Betano no devolvió eventos: posible bloqueo o cambio de estructura")
                return []

            min_start = datetime.now(timezone.utc) + timedelta(minutes=self.s.min_minutes_to_start)
            events = [e for e in events if e.start_time >= min_start]

            references = await self._reference_events(http)
            stats_provider = (FootballStatsProvider(http, self.s.api_football_key)
                              if self.s.api_football_key else None)

            candidates: list[Pick] = []
            for ev in events:
                candidates.extend(await self._analyze_event(ev, references, stats_provider))

        picks = self._select(candidates, self.s.max_picks_per_day - sent_today)
        published: list[Pick] = []
        for pick in picks:
            pick.justification = await self.justifier.justify(pick)
            if await self.publisher.send(format_signal(pick, self.s.timezone)):
                self.store.save(pick)
                published.append(pick)

        log.info("Ciclo completado en %.1fs: %d eventos, %d candidatos, %d publicados",
                 time.monotonic() - cycle_start, len(events), len(candidates), len(published))
        return published

    async def _reference_events(self, http: HttpClient) -> list[Event]:
        if not self.s.odds_api_key:
            log.info("ODDS_API_KEY no configurada: sin validación cruzada de mercado")
            return []
        provider = OddsApiProvider(http, self.s.odds_api_key, self.s.odds_api_sport_keys,
                                   self.s.odds_api_regions, self.s.odds_api_sharp_books)
        refs = await provider.fetch_events()
        log.info("Referencias de mercado: %d (partido x casa)", len(refs))
        return refs

    async def _analyze_event(self, ev: Event, references: list[Event],
                             stats_provider: FootballStatsProvider | None) -> list[Pick]:
        try:
            refs = find_matches(ev, references) if references else []
            # Solo pedimos estadísticas (caras en cuota de API) si hay cuotas en rango.
            in_range = any(self.s.min_odds <= s.odds <= self.s.max_odds for m in ev.markets for s in m.selections)
            stats = None
            if stats_provider and ev.sport == Sport.FOOTBALL and in_range:
                stats = await stats_provider.get_match_stats(ev)
            if not refs and stats is None:
                log.debug("Sin datos de respaldo para %s; no se evalúa", ev.name)
                return []
            picks = self.engine.evaluate(ev, refs, stats)
            for p in picks:
                log.info("Candidato: %s | %s @ %.2f | p=%.3f EV=%+.2f%% stake=%d",
                         ev.name, p.pick_label, p.selection.odds, p.model_probability, p.edge * 100, p.stake)
            return picks
        except Exception:
            log.exception("Error analizando %s", ev.name)
            return []

    def _select(self, candidates: list[Pick], remaining_today: int) -> list[Pick]:
        """Mejor pick por evento, sin duplicados, ordenado por EV ponderado por probabilidad."""
        best_per_event: dict[str, Pick] = {}
        for p in candidates:
            if self.store.already_sent(p) or self.store.event_already_signaled(p.event.name, p.event.start_time.isoformat()):
                continue
            cur = best_per_event.get(p.event.event_id)
            if cur is None or p.edge * p.model_probability > cur.edge * cur.model_probability:
                best_per_event[p.event.event_id] = p
        ranked = sorted(best_per_event.values(), key=lambda p: p.edge * p.model_probability, reverse=True)
        return ranked[: max(0, min(self.s.max_picks_per_cycle, remaining_today))]


def build_test_pick() -> Pick:
    over = Selection("over", "Más de 2.5", 1.95, 2.5)
    market = Market(MarketType.TOTALS, "Total de goles", [over, Selection("under", "Menos de 2.5", 1.85, 2.5)], 2.5)
    event = Event(
        event_id="test", source="test", sport=Sport.FOOTBALL, competition="Liga de Ejemplo",
        home="Equipo A", away="Equipo B", start_time=datetime.now(timezone.utc) + timedelta(hours=6),
        markets=[market],
    )
    evidence = Evidence(
        facts=[
            "El mercado de referencia (pinnacle) sin margen asigna 55.0% a esta selección, frente al 51.3% implícito en la cuota de Betano",
            "Equipo A promedia 2.10 goles a favor y 1.20 en contra como local esta temporada",
            "Equipo B promedia 1.40 goles a favor y 1.70 en contra como visitante",
            "Bajas de Equipo B: Defensa 1 (Lesión), Defensa 2 (Sanción)",
        ],
        metrics={"prob_estimada": 0.55, "prob_implicita_betano": 0.5128, "valor_esperado": 0.0725,
                 "prob_mercado_sharp": 0.55, "calidad_datos": "datos ficticios de prueba"},
    )
    return Pick(event=event, market=market, selection=over, model_probability=0.55,
                implied_probability=1 / 1.95, edge=0.0725, stake=3, pick_label="Más de 2.5 goles",
                evidence=evidence)
