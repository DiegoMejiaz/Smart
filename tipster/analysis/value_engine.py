"""Motor de selección de apuestas de valor con filtro de cuotas factibles.

Lógica:
  1. Solo se evalúan selecciones con cuota en [MIN_ODDS, MAX_ODDS].
  2. Probabilidad "real" estimada combinando:
       - Consenso de mercado sharp sin margen (Pinnacle/exchanges), y/o
       - Modelo estadístico propio (Poisson + Dixon-Coles con goles, xG, bajas).
     Sin ninguna de las dos fuentes NO se apuesta (nunca se opera a ciegas).
  3. Valor esperado: EV = p * cuota - 1. Se exige EV >= MIN_EDGE y p >= MIN_PROBABILITY.
  4. EV sospechosamente alto (> MAX_EDGE) => probable error de emparejamiento o
     cuota desactualizada: se descarta.
  5. Stake 1-5 a partir de Kelly fraccional, limitado según la calidad de los datos.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from tipster.analysis.probability import ScoreMatrix, devig_power
from tipster.core.models import Event, Evidence, Market, MarketType, Pick, Selection, Sport
from tipster.stats.football import FootballMatchStats

log = logging.getLogger(__name__)

MAX_EDGE = 0.25
UNITS = {Sport.FOOTBALL: "goles", Sport.BASKETBALL: "puntos", Sport.TENNIS: "juegos"}


@dataclass
class EngineConfig:
    min_odds: float = 1.50
    max_odds: float = 2.30
    min_edge: float = 0.03
    min_probability: float = 0.45
    max_overround_reference: float = 1.08
    kelly_fraction: float = 0.25
    consensus_weight: float = 0.65


class ValueEngine:
    def __init__(self, cfg: EngineConfig) -> None:
        self.cfg = cfg

    # ------------------------------------------------------------------ API
    def evaluate(self, event: Event, references: list[Event],
                 stats: FootballMatchStats | None = None) -> list[Pick]:
        score = self._score_matrix(stats)
        picks: list[Pick] = []
        for market in event.markets:
            if market.type in (MarketType.TOTALS, MarketType.HANDICAP) and not _is_half_line(market.line):
                continue  # evitamos líneas con push (enteras/cuartos) para mantener EV exacto
            for sel in market.selections:
                if not self.cfg.min_odds <= sel.odds <= self.cfg.max_odds:
                    continue
                pick = self._evaluate_selection(event, market, sel, references, score, stats)
                if pick:
                    picks.append(pick)
        picks.sort(key=lambda p: p.edge * p.model_probability, reverse=True)
        return picks

    # ------------------------------------------------------------ internals
    def _evaluate_selection(self, event: Event, market: Market, sel: Selection, references: list[Event],
                            score: ScoreMatrix | None, stats: FootballMatchStats | None) -> Pick | None:
        consensus, books = self._consensus_probability(market, sel, references)
        model_p = self._model_probability(market, sel, score) if event.sport == Sport.FOOTBALL else None

        if consensus is not None and model_p is not None:
            p = self.cfg.consensus_weight * consensus + (1 - self.cfg.consensus_weight) * model_p
            quality, min_edge, max_stake = "consenso+modelo", self.cfg.min_edge, 5
        elif consensus is not None:
            p = consensus
            quality, min_edge, max_stake = "consenso", self.cfg.min_edge, 4
        elif model_p is not None:
            p = model_p
            quality, min_edge, max_stake = "modelo", self.cfg.min_edge * 2, 2
        else:
            return None

        # Si ambas fuentes discrepan fuertemente, el pick no es fiable.
        if consensus is not None and model_p is not None and abs(consensus - model_p) > 0.12:
            log.debug("Descartado %s %s: consenso %.3f vs modelo %.3f", event.name, sel.label, consensus, model_p)
            return None

        edge = p * sel.odds - 1
        if edge < min_edge or p < self.cfg.min_probability:
            return None
        if edge > MAX_EDGE:
            log.warning("EV anómalo %.1f%% en %s / %s @ %.2f: se descarta (posible error de datos)",
                        edge * 100, event.name, sel.label, sel.odds)
            return None

        stake = self._stake(p, sel.odds, max_stake)
        evidence = Evidence(metrics={
            "prob_estimada": round(p, 4),
            "prob_implicita_betano": round(1 / sel.odds, 4),
            "valor_esperado": round(edge, 4),
            "margen_betano": round(market.overround() - 1, 4),
            "calidad_datos": quality,
        })
        if consensus is not None:
            evidence.metrics["prob_mercado_sharp"] = round(consensus, 4)
            evidence.facts.append(
                f"El mercado de referencia ({', '.join(books)}) sin margen asigna {consensus:.1%} a esta selección, "
                f"frente al {1 / sel.odds:.1%} implícito en la cuota de Betano"
            )
        if model_p is not None:
            evidence.metrics["prob_modelo_poisson"] = round(model_p, 4)
            lam = stats.expected_goals() if stats else None
            if lam:
                evidence.facts.append(
                    f"Modelo Poisson: goles esperados {event.home} {lam[0]:.2f} - {lam[1]:.2f} {event.away}; "
                    f"probabilidad estimada {model_p:.1%}"
                )
        if stats:
            evidence.facts.extend(stats.facts())

        return Pick(
            event=event, market=market, selection=sel,
            model_probability=p, implied_probability=1 / sel.odds, edge=edge, stake=stake,
            pick_label=pick_label(event, market, sel), evidence=evidence,
        )

    def _consensus_probability(self, market: Market, sel: Selection,
                               references: list[Event]) -> tuple[float | None, list[str]]:
        probs, books = [], []
        for ref in references:
            p = _reference_probability(ref, market, sel, self.cfg.max_overround_reference)
            if p is not None:
                probs.append(p)
                books.append(ref.source.split(":")[-1])
        if not probs:
            return None, []
        return sum(probs) / len(probs), books

    def _model_probability(self, market: Market, sel: Selection, score: ScoreMatrix | None) -> float | None:
        if score is None:
            return None
        t, k = market.type, sel.key
        if t == MarketType.MATCH_WINNER:
            return {"home": score.p_home, "draw": score.p_draw, "away": score.p_away}[k]()
        if t == MarketType.TOTALS and market.line is not None:
            over = score.p_over(market.line)
            return over if k == "over" else 1 - over
        if t == MarketType.BTTS:
            yes = score.p_btts()
            return yes if k == "yes" else 1 - yes
        if t == MarketType.DOUBLE_CHANCE:
            ph, pd, pa = score.p_home(), score.p_draw(), score.p_away()
            return {"1X": ph + pd, "X2": pd + pa, "12": ph + pa}.get(k)
        if t == MarketType.HANDICAP and market.line is not None:
            home_cover = score.p_home_handicap(market.line)
            return home_cover if k == "home" else 1 - home_cover
        return None

    @staticmethod
    def _score_matrix(stats: FootballMatchStats | None) -> ScoreMatrix | None:
        lam = stats.expected_goals() if stats else None
        return ScoreMatrix(*lam) if lam else None

    def _stake(self, p: float, odds: float, max_stake: int) -> int:
        """Kelly fraccional (% del bank) -> escala 1-5."""
        kelly = max((p * odds - 1) / (odds - 1), 0.0) * self.cfg.kelly_fraction
        thresholds = [0.005, 0.01, 0.015, 0.025]  # 0.5%, 1%, 1.5%, 2.5% del bank
        stake = 1 + sum(kelly >= t for t in thresholds)
        return max(1, min(stake, max_stake))


# ---------------------------------------------------------------------- helpers
def _is_half_line(line: float | None) -> bool:
    return line is not None and abs(abs(line) % 1 - 0.5) < 1e-9


def _reference_probability(ref: Event, market: Market, sel: Selection, max_overround: float) -> float | None:
    if market.type == MarketType.DOUBLE_CHANCE:
        mw = ref.market(MarketType.MATCH_WINNER)
        probs = _devig_market(mw, max_overround) if mw else None
        if not probs or "draw" not in probs:
            return None
        return {"1X": probs["home"] + probs["draw"], "X2": probs["draw"] + probs["away"],
                "12": probs["home"] + probs["away"]}.get(sel.key)
    line = market.line if market.type in (MarketType.TOTALS, MarketType.HANDICAP) else None
    ref_market = ref.market(market.type, line)
    if ref_market is None:
        return None
    probs = _devig_market(ref_market, max_overround)
    return probs.get(sel.key) if probs else None


def _devig_market(market: Market, max_overround: float) -> dict[str, float] | None:
    if market.overround() > max_overround:
        return None  # casa no "sharp": su precio no sirve de referencia
    fair = devig_power([s.odds for s in market.selections])
    return {s.key: p for s, p in zip(market.selections, fair)}


def pick_label(event: Event, market: Market, sel: Selection) -> str:
    unit = UNITS.get(event.sport, "")
    t, k = market.type, sel.key
    if t == MarketType.MATCH_WINNER:
        return {"home": f"Gana {event.home}", "away": f"Gana {event.away}", "draw": "Empate"}[k]
    if t == MarketType.TOTALS:
        return f"{'Más' if k == 'over' else 'Menos'} de {market.line:g} {unit}".strip()
    if t == MarketType.HANDICAP:
        team = event.home if k == "home" else event.away
        line = market.line if k == "home" else -(market.line or 0)
        return f"Hándicap {team} {line:+g}"
    if t == MarketType.BTTS:
        return f"Ambos equipos marcan: {'Sí' if k == 'yes' else 'No'}"
    if t == MarketType.DOUBLE_CHANCE:
        return {"1X": f"Doble oportunidad: {event.home} o empate",
                "X2": f"Doble oportunidad: empate o {event.away}",
                "12": f"Doble oportunidad: {event.home} o {event.away}"}[k]
    return sel.label
