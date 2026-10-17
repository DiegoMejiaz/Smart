"""Redacción de la justificación analítica con Claude (Anthropic API)."""
from __future__ import annotations

import json
import logging

import anthropic

from tipster.core.models import Pick

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """Eres el analista jefe de un servicio profesional de pronósticos deportivos.
Redactas la justificación de una selección ya decidida por un modelo cuantitativo.

Reglas:
- Español neutro, tono profesional, sobrio y analítico.
- Un único párrafo de 3 a 5 líneas (entre 55 y 95 palabras). Sin viñetas, sin emojis, sin títulos.
- Usa EXCLUSIVAMENTE los datos del bloque DATOS. No inventes lesiones, cifras, rachas ni contexto
  que no aparezca ahí; si un dato no está, no lo menciones.
- Cita al menos dos cifras concretas de los datos y explica por qué la cuota ofrece valor
  (probabilidad estimada frente a la implícita en la cuota).
- No prometas resultados ni uses expresiones como "seguro", "fijo" o "no puede fallar".
- Devuelve solo el párrafo."""


class Justifier:
    def __init__(self, model: str = "claude-opus-5-5", effort: str = "medium", enabled: bool = True,
                 api_key: str | None = None) -> None:
        self.model = model
        self.effort = effort
        self.enabled = enabled
        self.client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=3, timeout=120.0) if enabled else None

    async def justify(self, pick: Pick) -> str:
        if not self.enabled or self.client is None:
            return fallback_justification(pick)
        try:
            text = await self._call_llm(pick)
            if text:
                return text
            log.warning("LLM devolvió texto vacío para %s; usando plantilla", pick.event.name)
        except anthropic.RateLimitError as exc:
            log.error("LLM rate limit (request_id=%s); usando plantilla", getattr(exc, "request_id", "?"))
        except anthropic.APIStatusError as exc:
            log.error("LLM error HTTP %s: %s; usando plantilla", exc.status_code, exc.message)
        except anthropic.APIConnectionError as exc:
            log.error("LLM sin conexión: %s; usando plantilla", exc)
        return fallback_justification(pick)

    async def _call_llm(self, pick: Pick) -> str:
        payload = {
            "deporte": pick.event.sport.value,
            "competicion": pick.event.competition,
            "evento": pick.event.name,
            "pronostico": pick.pick_label,
            "cuota_betano": pick.selection.odds,
            "metricas": pick.evidence.metrics,
            "hechos": pick.evidence.facts,
        }
        # Fallback servidor: si el modelo principal rechaza la petición,
        # la API la reintenta automáticamente con otro modelo en la misma llamada.
        response = await self.client.beta.messages.create(  # type: ignore[union-attr]
            model=self.model,
            max_tokens=4000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": self.effort},
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": "DATOS:\n" + json.dumps(payload, ensure_ascii=False, indent=2)
                           + "\n\nRedacta la justificación.",
            }],
        )
        if response.stop_reason == "refusal":
            log.warning("LLM rechazó la petición (%s)", response.stop_details)
            return ""
        text = "".join(b.text for b in response.content if b.type == "text").strip()
        log.debug("LLM ok (request_id=%s, in=%s, out=%s)", response._request_id,
                  response.usage.input_tokens, response.usage.output_tokens)
        return " ".join(text.split())


def fallback_justification(pick: Pick) -> str:
    """Plantilla determinista si el LLM no está disponible: solo datos verificables."""
    m = pick.evidence.metrics
    parts = [
        f"Nuestro modelo estima una probabilidad del {pick.model_probability:.1%} para «{pick.pick_label}», "
        f"por encima del {pick.implied_probability:.1%} implícito en la cuota {pick.selection.odds:.2f} de Betano, "
        f"lo que supone un valor esperado del {pick.edge:+.1%}."
    ]
    if "prob_mercado_sharp" in m:
        parts.append(f"El mercado de referencia sin margen respalda la lectura con un {float(m['prob_mercado_sharp']):.1%}.")
    for fact in pick.evidence.facts[1:3]:
        parts.append(fact.rstrip(".") + ".")
    return " ".join(parts)
