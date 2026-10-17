"""Cliente HTTP resiliente: rotación de proxies, headers e impersonación TLS.

Usa curl_cffi para reproducir la huella TLS/HTTP2 de navegadores reales, lo que
reduce los bloqueos por WAF frente a clientes Python "puros".
"""
from __future__ import annotations

import asyncio
import itertools
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

log = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.6 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
]
ACCEPT_LANGUAGES = ["es-PE,es;q=0.9,en;q=0.8", "es-ES,es;q=0.9", "es-MX,es;q=0.9,en-US;q=0.7", "es-419,es;q=0.9"]

RETRYABLE_STATUS = {403, 408, 425, 429, 500, 502, 503, 504}


class FetchError(Exception):
    """Fallo definitivo tras agotar reintentos."""


@dataclass
class ProxyState:
    url: str | None
    failures: int = 0
    banned_until: float = 0.0

    @property
    def available(self) -> bool:
        return time.monotonic() >= self.banned_until


@dataclass
class ProxyPool:
    """Rotación round-robin con "cuarentena" exponencial para proxies que fallan."""
    proxies: list[str]
    _states: list[ProxyState] = field(init=False)
    _cycle: Any = field(init=False)

    def __post_init__(self) -> None:
        # Sin proxies configurados se usa conexión directa (None).
        self._states = [ProxyState(p) for p in self.proxies] or [ProxyState(None)]
        self._cycle = itertools.cycle(self._states)

    def next(self) -> ProxyState:
        for _ in range(len(self._states)):
            st = next(self._cycle)
            if st.available:
                return st
        # Todos en cuarentena: usar el que antes se libere.
        return min(self._states, key=lambda s: s.banned_until)

    def report_failure(self, st: ProxyState) -> None:
        st.failures += 1
        cooldown = min(60 * 2 ** (st.failures - 1), 1800)
        st.banned_until = time.monotonic() + cooldown
        log.warning("Proxy %s en cuarentena %ss (fallos=%d)", _mask(st.url), cooldown, st.failures)

    def report_success(self, st: ProxyState) -> None:
        st.failures = 0
        st.banned_until = 0.0


def _mask(proxy: str | None) -> str:
    if not proxy:
        return "DIRECT"
    return proxy.split("@")[-1]  # no loguear credenciales


class HttpClient:
    def __init__(
        self,
        proxies: list[str],
        impersonate: list[str],
        timeout: float = 20.0,
        max_retries: int = 4,
        min_delay: float = 1.5,
        max_delay: float = 4.0,
    ) -> None:
        self.pool = ProxyPool(proxies)
        self.impersonate = impersonate or ["chrome"]
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_delay = min_delay
        self.max_delay = max_delay
        self._session: AsyncSession | None = None
        self._last_request = 0.0
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "HttpClient":
        self._session = AsyncSession()
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._session:
            await self._session.close()

    def _headers(self, referer: str | None, extra: dict[str, str] | None) -> dict[str, str]:
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": random.choice(ACCEPT_LANGUAGES),
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }
        if referer:
            headers["Referer"] = referer
        if extra:
            headers.update(extra)
        return headers

    async def _throttle(self) -> None:
        """Espaciado aleatorio entre peticiones (cortesía + menor huella)."""
        async with self._lock:
            wait = random.uniform(self.min_delay, self.max_delay) - (time.monotonic() - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        referer: str | None = None,
        expect_json: bool = True,
        throttle: bool = True,
    ) -> Any:
        if self._session is None:
            raise RuntimeError("HttpClient debe usarse con 'async with'")

        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            proxy = self.pool.next()
            browser = random.choice(self.impersonate)
            if throttle:
                await self._throttle()
            started = time.monotonic()
            try:
                resp = await self._session.get(
                    url,
                    params=params,
                    headers=self._headers(referer, headers),
                    proxy=proxy.url,
                    impersonate=browser,  # type: ignore[arg-type]
                    timeout=self.timeout,
                )
                elapsed = (time.monotonic() - started) * 1000
                log.debug("GET %s -> %s (%.0f ms, proxy=%s, ua=%s)", url, resp.status_code, elapsed, _mask(proxy.url), browser)

                if resp.status_code in RETRYABLE_STATUS:
                    last_error = FetchError(f"HTTP {resp.status_code}")
                    if resp.status_code in (403, 429):
                        self.pool.report_failure(proxy)
                    if resp.status_code == 403 and "splash screen" in resp.text[:2000].lower():
                        log.error("Geo-bloqueo de Betano desde proxy=%s: usa un proxy residencial del país "
                                  "del dominio configurado (BETANO_BASE_URL)", _mask(proxy.url))
                    log.warning("Intento %d/%d %s -> HTTP %s", attempt, self.max_retries, url, resp.status_code)
                elif resp.status_code >= 400:
                    raise FetchError(f"HTTP {resp.status_code} no recuperable para {url}")
                else:
                    self.pool.report_success(proxy)
                    if not expect_json:
                        return resp.text
                    try:
                        return resp.json()
                    except ValueError as exc:
                        # Páginas de challenge (HTML) en lugar de JSON = bloqueo encubierto
                        last_error = FetchError(f"Respuesta no JSON ({exc}); posible challenge anti-bot")
                        self.pool.report_failure(proxy)
                        log.warning("Intento %d/%d %s -> respuesta no JSON", attempt, self.max_retries, url)
            except RequestException as exc:
                last_error = exc
                self.pool.report_failure(proxy)
                log.warning("Intento %d/%d %s -> error de red: %s", attempt, self.max_retries, url, exc)

            backoff = min(2 ** attempt + random.uniform(0, 1.5), 30)
            await asyncio.sleep(backoff)

        raise FetchError(f"Agotados {self.max_retries} intentos para {url}: {last_error}")
