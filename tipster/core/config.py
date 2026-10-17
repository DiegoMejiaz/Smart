"""Configuración central del agente, cargada desde variables de entorno / .env."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    betano_sports: list[str] = ["futbol", "baloncesto", "tenis"]
    betano_league_paths: list[str] = []  # rutas opcionales p.ej. "/sport/futbol/espana/laliga/5/"
    betano_use_browser_fallback: bool = False  # Playwright si la API JSON bloquea

    # --- Validación de mercado / estadísticas ---
    odds_api_key: str | None = None  # https://the-odds-api.com (cuotas de referencia)
    odds_api_regions: str = "eu"
    odds_api_sharp_books: list[str] = ["pinnacle", "betfair_ex_eu", "matchbook"]
    odds_api_sport_keys: list[str] = [
        "soccer_epl", "soccer_spain_la_liga", "soccer_italy_serie_a",
        "soccer_germany_bundesliga", "soccer_france_ligue_one",
        "soccer_uefa_champs_league", "basketball_nba", "basketball_euroleague",
        "tennis_atp_aus_open_singles",
    ]
    api_football_key: str | None = None  # https://www.api-football.com (forma, H2H, lesiones, xG)

    # --- Red / anti-bloqueo ---
    proxies: list[str] = []  # http://user:pass@host:port
    request_timeout: float = 20.0
    request_max_retries: int = 4
    min_request_delay: float = 1.5
    max_request_delay: float = 4.0
    impersonate_browsers: list[str] = ["chrome", "chrome131", "edge101", "safari17_0"]

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
