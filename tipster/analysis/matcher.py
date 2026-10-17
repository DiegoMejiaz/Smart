"""Emparejamiento de eventos entre fuentes con nombres distintos.

"Man Utd" vs "Manchester United", "Atl. Madrid" vs "Atletico Madrid"...
Se normaliza el texto y se combina similitud difusa + ventana horaria.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import timedelta

from rapidfuzz import fuzz

from tipster.core.models import Event

STOPWORDS = {"fc", "cf", "sc", "ac", "cd", "ud", "sd", "afc", "club", "de", "the", "calcio"}
ALIASES = {
    "man utd": "manchester united", "man united": "manchester united", "man city": "manchester city",
    "atl madrid": "atletico madrid", "atl.": "atletico", "psg": "paris saint germain",
    "inter": "internazionale", "spurs": "tottenham", "wolves": "wolverhampton",
}


def normalize(name: str) -> str:
    txt = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    txt = re.sub(r"[^a-z0-9. ]", " ", txt)
    for alias, full in ALIASES.items():
        txt = re.sub(rf"\b{re.escape(alias)}(?=\s|$)", full, txt)
    tokens = [t for t in txt.replace(".", " ").split() if t not in STOPWORDS]
    return " ".join(tokens) or txt.strip()


def team_similarity(a: str, b: str) -> float:
    na, nb = normalize(a), normalize(b)
    return max(fuzz.token_set_ratio(na, nb), fuzz.partial_ratio(na, nb) if min(len(na), len(nb)) >= 4 else 0)


def find_matches(target: Event, candidates: list[Event], min_score: float = 80.0,
                 max_time_diff: timedelta = timedelta(hours=2)) -> list[Event]:
    """Devuelve todos los candidatos (p.ej. una entrada por casa) del mismo partido."""
    best_score, matches = 0.0, []
    for cand in candidates:
        if cand.sport != target.sport or abs(cand.start_time - target.start_time) > max_time_diff:
            continue
        score = min(team_similarity(target.home, cand.home), team_similarity(target.away, cand.away))
        if score < min_score:
            continue
        if score > best_score + 5:
            best_score, matches = score, [cand]
        elif score >= best_score - 5:
            matches.append(cand)
            best_score = max(best_score, score)
    return matches
