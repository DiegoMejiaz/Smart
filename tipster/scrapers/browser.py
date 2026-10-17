"""Fallback opcional con navegador headless (Playwright).

Solo se usa si BETANO_USE_BROWSER_FALLBACK=true y la API/HTML están bloqueadas.
Requiere: pip install playwright && playwright install chromium
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


async def fetch_initial_state_with_browser(url: str, timeout_ms: int = 45_000) -> Any | None:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        log.error("Playwright no instalado; desactiva BETANO_USE_BROWSER_FALLBACK o instálalo")
        return None

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context(locale="es-ES", viewport={"width": 1366, "height": 900})
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                await page.wait_for_timeout(3_000)  # dejar que el SPA hidrate
                state = await page.evaluate("() => window['initial_state'] || null")
                if state is None:
                    log.warning("Navegador: initial_state vacío en %s", url)
                return state
            finally:
                await browser.close()
    except Exception as exc:
        log.error("Navegador headless falló en %s: %s", url, exc)
        return None
