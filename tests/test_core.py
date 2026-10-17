import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from tipster.analysis.matcher import find_matches, team_similarity
from tipster.analysis.probability import ScoreMatrix, devig_power
from tipster.analysis.value_engine import EngineConfig, ValueEngine
from tipster.core.models import Event, Market, MarketType, Selection, Sport
from tipster.llm.justifier import fallback_justification
from tipster.notifier.formatter import format_signal
from tipster.scrapers.betano import BetanoScraper, extract_initial_state
from tipster.stats.football import H2H, FootballMatchStats, TeamProfile

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "betano_sample.json").read_text())


def _scraper() -> BetanoScraper:
    return BetanoScraper(http=None, base_url="https://www.betano.pe", sports=["futbol"])  # type: ignore[arg-type]


# --------------------------------------------------------------------- betano
def test_betano_parser_extracts_prematch_events_and_markets():
    events = _scraper().parse_payload(FIXTURE)
    assert {e.name for e in events} == {"Real Madrid vs Getafe", "Boston Celtics vs Miami Heat"}

    rm = next(e for e in events if e.home == "Real Madrid")
    assert rm.sport == Sport.FOOTBALL
    assert rm.competition == "España - LaLiga"
    assert rm.url.startswith("https://www.betano.pe/")
    types = sorted((m.type.value, m.line) for m in rm.markets)
    # El mercado de 1ª parte se excluye; los totales se separan por línea.
    assert types == [("btts", None), ("match_winner", None), ("totals", 2.5), ("totals", 3.5)]
    over = rm.market(MarketType.TOTALS, 2.5)
    assert {s.key: s.odds for s in over.selections} == {"over": 1.80, "under": 2.02}

    nba = next(e for e in events if e.sport == Sport.BASKETBALL)
    assert {s.key for s in nba.market(MarketType.MATCH_WINNER).selections} == {"home", "away"}


def test_initial_state_extraction_from_html():
    html = '<html><script>window["initial_state"]=' + json.dumps(FIXTURE) + ';</script></html>'
    state = extract_initial_state(html)
    assert len(_scraper().parse_payload(state)) == 2


# ---------------------------------------------------------------- probability
def test_devig_power_sums_to_one_and_removes_margin():
    probs = devig_power([1.90, 1.90])
    assert sum(probs) == pytest.approx(1.0)
    assert probs[0] == pytest.approx(0.5)
    fav, dog = devig_power([1.30, 3.80])
    assert sum([fav, dog]) == pytest.approx(1.0)
    assert fav > 1 / 1.30 / (1 / 1.30 + 1 / 3.80)  # el método potencia favorece al favorito


def test_score_matrix_consistency():
    sm = ScoreMatrix(1.6, 1.1)
    assert sm.p_home() + sm.p_draw() + sm.p_away() == pytest.approx(1.0)
    assert sm.p_home() > sm.p_away()
    assert 0.4 < sm.p_over(2.5) < 0.6
    assert sm.p_home_handicap(-0.5) == pytest.approx(sm.p_home())


# ---------------------------------------------------------------------- match
def test_team_matching_handles_aliases():
    assert team_similarity("Man Utd", "Manchester United") >= 90
    assert team_similarity("Atl. Madrid", "Atletico Madrid") >= 90
    assert team_similarity("Real Madrid", "Getafe") < 60


def _event(source, home, away, markets, sport=Sport.FOOTBALL, hours=24):
    return Event(event_id=f"{source}-{home}", source=source, sport=sport, competition="LaLiga",
                 home=home, away=away, start_time=datetime.now(timezone.utc) + timedelta(hours=hours),
                 markets=markets)


def _totals(over, under, line=2.5):
    return Market(MarketType.TOTALS, "totals", [Selection("over", "Over", over, line), Selection("under", "Under", under, line)], line)


def test_find_matches_respects_time_window():
    target = _event("betano", "Real Madrid", "Getafe", [])
    same = _event("odds_api:pinnacle", "Real Madrid CF", "Getafe CF", [])
    later = _event("odds_api:pinnacle", "Real Madrid", "Getafe", [], hours=72)
    assert find_matches(target, [same, later]) == [same]


# --------------------------------------------------------------------- engine
def test_engine_finds_value_against_sharp_consensus():
    betano = _event("betano", "Real Madrid", "Getafe", [_totals(2.00, 1.80)])
    pinnacle = _event("odds_api:pinnacle", "Real Madrid", "Getafe", [_totals(1.80, 2.08)])
    picks = ValueEngine(EngineConfig()).evaluate(betano, [pinnacle])
    assert len(picks) == 1
    p = picks[0]
    assert p.selection.key == "over" and p.pick_label == "Más de 2.5 goles"
    assert p.edge > 0.03 and 1 <= p.stake <= 4


def test_engine_rejects_odds_outside_feasible_range_and_no_value():
    betano = _event("betano", "Real Madrid", "Getafe", [_totals(2.60, 1.45)])
    pinnacle = _event("odds_api:pinnacle", "Real Madrid", "Getafe", [_totals(2.40, 1.60)])
    assert ValueEngine(EngineConfig()).evaluate(betano, [pinnacle]) == []

    fair = _event("betano", "Real Madrid", "Getafe", [_totals(1.85, 1.95)])
    sharp = _event("odds_api:pinnacle", "Real Madrid", "Getafe", [_totals(1.80, 2.08)])
    assert ValueEngine(EngineConfig()).evaluate(fair, [sharp]) == []


def test_engine_never_bets_without_supporting_data():
    betano = _event("betano", "Real Madrid", "Getafe", [_totals(2.00, 1.80)])
    assert ValueEngine(EngineConfig()).evaluate(betano, [], None) == []


def test_engine_ignores_soft_reference_books():
    betano = _event("betano", "Real Madrid", "Getafe", [_totals(2.00, 1.80)])
    soft = _event("odds_api:softbook", "Real Madrid", "Getafe", [_totals(1.70, 1.90)])  # margen 11%
    assert ValueEngine(EngineConfig()).evaluate(betano, [soft]) == []


def test_engine_with_stats_only_requires_double_edge_and_caps_stake():
    stats = FootballMatchStats(
        home=TeamProfile("Real Madrid", 2.4, 0.7, "WWWDW", injuries=[]),
        away=TeamProfile("Getafe", 0.9, 1.6, "LDLLW", injuries=["X (Lesión)", "Y (Sanción)"]),
        h2h=H2H(6, 4, 1, 1, 2.8, 0.5),
    )
    betano = _event("betano", "Real Madrid", "Getafe", [_totals(2.20, 1.62)])
    picks = ValueEngine(EngineConfig()).evaluate(betano, [], stats)
    assert picks and all(p.stake <= 2 and p.edge >= 0.06 for p in picks)
    assert any("Bajas de Getafe" in f for f in picks[0].evidence.facts)


# ------------------------------------------------------------------ formatter
def test_signal_format_is_exact():
    betano = _event("betano", "Real Madrid", "Getafe", [_totals(2.00, 1.80)])
    pinnacle = _event("odds_api:pinnacle", "Real Madrid", "Getafe", [_totals(1.80, 2.08)])
    pick = ValueEngine(EngineConfig()).evaluate(betano, [pinnacle])[0]
    pick.justification = fallback_justification(pick)
    text = format_signal(pick, "America/Lima")
    lines = text.split("\n")
    assert lines[0] == "🏆 Fútbol / LaLiga"
    assert lines[1] == "⚽️ Evento: Real Madrid vs Getafe"
    assert lines[2].startswith("⏰ Hora: ")
    assert lines[3] == ""
    assert lines[4] == "🎯 Pronóstico: Más de 2.5 goles"
    assert lines[5] == "📈 Cuota: 2.00"
    assert lines[6] == "🏠 Casa de Apuestas: Betano"
    assert lines[7].startswith("💼 Stake recomendado: ")
    assert lines[8] == ""
    assert lines[9] == "📊 Justificación:"
    assert "Betano" in lines[10]


# --------------------------------------------------------------------- config
@pytest.mark.parametrize("raw,expected", [
    ("", []),
    ("http://u:p@h:1", ["http://u:p@h:1"]),
    ("http://a:1, http://b:2", ["http://a:1", "http://b:2"]),
    ('["http://u:p@h:1"]', ["http://u:p@h:1"]),
])
def test_list_settings_accept_json_csv_or_empty(monkeypatch, raw, expected):
    from tipster.core.config import Settings

    monkeypatch.setenv("PROXIES", raw)
    assert Settings(_env_file=None).proxies == expected
