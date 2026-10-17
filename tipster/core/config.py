"""Configuración central del agente, cargada desde variables de entorno / .env."""
from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Listas que aceptan JSON (["a","b"]), separadas por comas (a,b) o vacías.
StrList = Annotated[list[str], NoDecode]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- Telegram ---
    telegram_bot_token: str = Field(..., description="Token del bot (BotFather)")
    telegram_channel_id: str = Field(..., description="@canal o id numérico (-100...)")
    telegram_dry_run: bool = False  # si True, imprime en log en vez de publicar
    send_test_signal_on_start: bool = False  # publica una señal de ejemplo al arrancar

    # --- LLM (Claude) ---
    anthropic_api_key: str | None = None  # el SDK también lo lee de ANTHROPIC_API_KEY
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "medium"
    llm_enabled: bool = True

    # --- Betano ---
    betano_base_url: str = "https://www.betano.pe"  # dominio del país (.pe, .mx, .com.co, .pt, .de...)
    betano_sports: StrList = ["futbol", "baloncesto", "tenis"]
    betano_league_paths: StrList = []  # rutas opcionales p.ej. "/sport/futbol/espana/laliga/5/"
    betano_use_browser_fallback: bool = False  # Playwright si la API JSON bloquea
    betano_max_leagues: int = 5  # ligas destacadas a revisar por deporte (cada una = 1 petición)

    # --- Validación de mercado / estadísticas ---
    odds_api_key: str | None = None  # https://the-odds-api.com (cuotas de referencia)
    odds_api_regions: str = "eu"
    odds_api_sharp_books: StrList = ["pinnacle", "betfair_ex_eu", "matchbook"]
    odds_api_sport_keys: StrList = [
        "soccer_epl", "soccer_spain_la_liga", "soccer_italy_serie_a",
        "soccer_germany_bundesliga", "soccer_france_ligue_one",
        "soccer_uefa_champs_league", "basketball_nba", "basketball_euroleague",
        "tennis_atp_aus_open_singles",
    ]
    api_football_key: str | None = None  # https://www.api-football.com (forma, H2H, lesiones, xG)

    # --- Red / anti-bloqueo ---
    scrape_do_token: str | None = None  # https://scrape.do: sustituye a los proxies para Betano
    scrape_do_geo: str = "pe"           # país de la IP (pe, mx, co, cl, br, pt, de...)
    scrape_do_super: bool = True        # IP residencial (necesaria contra el geo-bloqueo)
    proxies: StrList = []  # http://user:pass@host:port
    request_timeout: float = 20.0
    request_max_retries: int = 4
    min_request_delay: float = 1.5
    max_request_delay: float = 4.0
    impersonate_browsers: StrList = ["chrome", "chrome131", "edge101", "safari17_0"]

    # --- Motor de valor ---
    min_odds: float = 1.50
    max_odds: float = 2.30
    min_edge: float = 0.03          # valor esperado mínimo (3%)
    min_probability: float = 0.45   # prob. estimada mínima
    max_overround_reference: float = 1.08
    kelly_fraction: float = 0.25
    hours_ahead: int = 36
    min_minutes_to_start: int = 30
    max_picks_per_cycle: int = 3
    max_picks_per_day: int = 10

    # --- Ejecución ---
    poll_interval_seconds: int = 600
    timezone: str = "America/Lima"
    db_path: str = "data/tipster.db"
    log_level: str = "INFO"
    log_dir: str = "logs"

    @field_validator("betano_sports", "betano_league_paths", "odds_api_sharp_books",
                     "odds_api_sport_keys", "proxies", "impersonate_browsers", mode="before")
    @classmethod
    def _parse_list(cls, v: object) -> object:
        if not isinstance(v, str):
            return v
        v = v.strip()
        if not v:
            return []
        if v.startswith("["):
            try:
                return json.loads(v)
            except json.JSONDecodeError as exc:
                raise ValueError(f"lista JSON inválida: {exc}") from exc
        return [item.strip().strip('"\'') for item in v.split(",") if item.strip()]

    @field_validator("proxies")
    @classmethod
    def _drop_placeholder_proxies(cls, v: list[str]) -> list[str]:
        """Ignora los proxies de ejemplo de .env.example (no existen y bloquean todo)."""
        return [p for p in v if "example.com" not in p and "user:pass@" not in p]

    @field_validator("telegram_bot_token", "telegram_channel_id", "anthropic_api_key",
                     "odds_api_key", "api_football_key", "scrape_do_token", mode="before")
    @classmethod
    def _clean_secret(cls, v: object) -> object:
        """Quita espacios, saltos de línea y comillas que se cuelan al copiar/pegar."""
        if not isinstance(v, str):
            return v
        v = v.strip().strip('"\'').strip()
        return v or None

    @field_validator("telegram_bot_token")
    @classmethod
    def _check_bot_token(cls, v: str) -> str:
        if v.lower().startswith("bot") and ":" in v[3:]:
            v = v[3:]  # "bot123:ABC" -> "123:ABC"
        if v.startswith("https://") or "api.telegram.org" in v:
            raise ValueError("pega solo el token (123456789:AAH...), no la URL de la API")
        bot_id, _, secret = v.partition(":")
        if not bot_id.isdigit() or len(secret) < 30 or any(c.isspace() for c in v):
            raise ValueError(
                f"formato inválido (longitud {len(v)}). Debe ser 'números:letras', "
                "p.ej. 123456789:AAH... — cópialo de nuevo desde @BotFather (/mybots → API Token)"
            )
        return v

    @field_validator("betano_base_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @model_validator(mode="after")
    def _check_ranges(self) -> "Settings":
        if not 1.0 < self.min_odds < self.max_odds:
            raise ValueError("Se requiere 1.0 < MIN_ODDS < MAX_ODDS")
        if self.min_request_delay > self.max_request_delay:
            raise ValueError("MIN_REQUEST_DELAY no puede superar MAX_REQUEST_DELAY")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
