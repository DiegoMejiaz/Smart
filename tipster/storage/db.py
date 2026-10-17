"""Persistencia SQLite: evita señales duplicadas y guarda histórico para auditoría/ROI."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from tipster.core.models import Pick

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals (
    dedupe_key   TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    sport        TEXT NOT NULL,
    competition  TEXT NOT NULL,
    event        TEXT NOT NULL,
    start_time   TEXT NOT NULL,
    pick         TEXT NOT NULL,
    odds         REAL NOT NULL,
    probability  REAL NOT NULL,
    edge         REAL NOT NULL,
    stake        INTEGER NOT NULL,
    justification TEXT,
    result       TEXT            -- WIN / LOSS / PUSH (rellenar para medir ROI)
);
CREATE INDEX IF NOT EXISTS idx_signals_created ON signals(created_at);
"""


class SignalStore:
    def __init__(self, path: str) -> None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.executescript(SCHEMA)

    def already_sent(self, pick: Pick) -> bool:
        cur = self.conn.execute("SELECT 1 FROM signals WHERE dedupe_key = ?", (pick.dedupe_key,))
        return cur.fetchone() is not None

    def event_already_signaled(self, event_name: str, start_iso: str) -> bool:
        cur = self.conn.execute("SELECT 1 FROM signals WHERE event = ? AND start_time = ?", (event_name, start_iso))
        return cur.fetchone() is not None

    def count_today(self) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cur = self.conn.execute("SELECT COUNT(*) FROM signals WHERE substr(created_at, 1, 10) = ?", (today,))
        return int(cur.fetchone()[0])

    def save(self, pick: Pick) -> None:
        try:
            with self.conn:
                self.conn.execute(
                    "INSERT OR IGNORE INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                    (
                        pick.dedupe_key, datetime.now(timezone.utc).isoformat(), pick.event.sport.value,
                        pick.event.competition, pick.event.name, pick.event.start_time.isoformat(),
                        pick.pick_label, pick.selection.odds, pick.model_probability, pick.edge,
                        pick.stake, pick.justification,
                    ),
                )
        except sqlite3.Error as exc:
            log.error("No se pudo guardar la señal %s: %s", pick.dedupe_key, exc)

    def close(self) -> None:
        self.conn.close()
