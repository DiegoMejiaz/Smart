"""Formato estricto del mensaje de señal."""
from __future__ import annotations

from zoneinfo import ZoneInfo

from tipster.core.models import Pick, Sport

SPORT_NAMES = {Sport.FOOTBALL: "Fútbol", Sport.BASKETBALL: "Baloncesto", Sport.TENNIS: "Tenis", Sport.OTHER: "Deporte"}
# El icono de la línea "Evento" se adapta al deporte; la estructura no cambia.
EVENT_ICONS = {Sport.FOOTBALL: "⚽️", Sport.BASKETBALL: "🏀", Sport.TENNIS: "🎾", Sport.OTHER: "⚽️"}

TEMPLATE = """🏆 {sport} / {competition}
{icon} Evento: {home} vs {away}
⏰ Hora: {time}

🎯 Pronóstico: {pick}
📈 Cuota: {odds:.2f}
🏠 Casa de Apuestas: Betano
💼 Stake recomendado: {stake}

📊 Justificación:
{justification}"""


def format_signal(pick: Pick, tz: str = "America/Lima") -> str:
    ev = pick.event
    local = ev.start_time.astimezone(ZoneInfo(tz))
    return TEMPLATE.format(
        sport=SPORT_NAMES.get(ev.sport, "Deporte"),
        competition=ev.competition,
        icon=EVENT_ICONS.get(ev.sport, "⚽️"),
        home=ev.home,
        away=ev.away,
        time=f"{local:%d/%m/%Y %H:%M} ({local.tzname()})",
        pick=pick.pick_label,
        odds=pick.selection.odds,
        stake=max(1, min(5, pick.stake)),
        justification=pick.justification.strip(),
    )
