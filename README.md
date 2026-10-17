# Tipster AI — Agente autónomo de Value Betting (Betano → Telegram)

Agente en Python que monitorea las cuotas pre-partido de **Betano**, las contrasta con el mercado
de referencia (casas *sharp*) y con estadísticas deportivas, selecciona apuestas de valor dentro de
un rango de cuotas factible (por defecto **1.50 – 2.30**), redacta una justificación con un LLM
(Claude) y publica la señal en un canal de **Telegram**.

> ⚠️ **Aviso importante.** Ningún modelo garantiza aciertos: el *value betting* busca una ventaja
> esperada positiva a largo plazo, con rachas negativas inevitables. Los endpoints internos de
> Betano no son una API pública y su uso automatizado puede estar restringido por sus Términos de
> Servicio; revisa la normativa de tu país y de la casa antes de operar. Incluye mensajes de juego
> responsable (+18) en tu canal.

---

## 1. Arquitectura

```
                ┌───────────────────────────────────────────────────────────┐
                │                    TipsterAgent (agent.py)                │
                │   bucle cada POLL_INTERVAL_SECONDS  ·  o --once vía cron   │
                └───────────────┬───────────────────────────────────────────┘
                                │
   ┌────────────────────────────┼──────────────────────────────┬─────────────────────────┐
   ▼                            ▼                              ▼                         ▼
BetanoScraper             OddsApiProvider             FootballStatsProvider        SignalStore
(scrapers/betano.py)      (scrapers/odds_api.py)      (stats/football.py)          (storage/db.py)
 API JSON → HTML →         Pinnacle, Betfair Ex.,      API-Football: goles,          SQLite: dedupe,
 navegador headless        Matchbook (cuotas sharp)    forma, H2H, lesiones, xG      límite diario, ROI
   │                            │                              │
   └──────────── HttpClient (scrapers/http_client.py) ─────────┘
                 curl_cffi · impersonación TLS de navegador · rotación de
                 proxies con cuarentena · rotación de headers · backoff
                                │
                                ▼
                    matcher.py (empareja equipos entre fuentes)
                                │
                                ▼
                ValueEngine (analysis/value_engine.py)
                 filtro de cuotas 1.50–2.30 · de-vig del mercado sharp ·
                 Poisson + Dixon-Coles (goles + xG + bajas) · EV · Kelly → stake 1-5
                                │
                                ▼
                Justifier (llm/justifier.py) — Claude, solo con datos aportados
                                │
                                ▼
                formatter.py → TelegramPublisher (notifier/telegram_bot.py)
```

### Estructura del proyecto

```
Smart/
├── tipster/
│   ├── __main__.py            # CLI: python -m tipster [--once] [--dry-run]
│   ├── agent.py               # orquestador del ciclo
│   ├── core/
│   │   ├── config.py          # Settings (pydantic-settings, .env)
│   │   ├── logging_setup.py   # consola + archivo rotativo logs/tipster.log
│   │   └── models.py          # Event, Market, Selection, Pick, Evidence
│   ├── scrapers/
│   │   ├── http_client.py     # proxies, headers, impersonación TLS, reintentos
│   │   ├── betano.py          # extractor de Betano
│   │   ├── browser.py         # fallback Playwright (opcional)
│   │   └── odds_api.py        # cuotas de referencia (The Odds API)
│   ├── stats/football.py      # estadísticas (API-Football)
│   ├── analysis/
│   │   ├── probability.py     # de-vig (método potencia), Poisson/Dixon-Coles
│   │   ├── matcher.py         # emparejamiento difuso de equipos
│   │   └── value_engine.py    # algoritmo de selección
│   ├── llm/justifier.py       # justificación con Claude + plantilla de respaldo
│   ├── notifier/
│   │   ├── formatter.py       # formato estricto del mensaje
│   │   └── telegram_bot.py    # publicación con reintentos / flood control
│   └── storage/db.py          # SQLite
├── tests/                     # pytest (parser, motor, formato...)
├── deploy/                    # systemd + cron
├── Dockerfile · docker-compose.yml
├── requirements.txt · requirements-dev.txt
└── .env.example
```

---

## 2. Cómo decide el agente (motor de valor)

1. **Filtro de cuotas factibles:** solo se evalúan selecciones con cuota entre `MIN_ODDS` y `MAX_ODDS`.
   En totales y hándicaps solo se usan líneas `.5` (sin *push*) para que el EV sea exacto.
2. **Probabilidad real estimada** a partir de dos fuentes independientes:
   - **Consenso sharp:** se toman las cuotas de Pinnacle/exchanges (margen ≤ `MAX_OVERROUND_REFERENCE`),
     se elimina su margen con el *método potencia* y se promedian.
   - **Modelo estadístico (fútbol):** goles esperados de cada equipo combinando promedios de goles
     local/visitante con **xG** de los últimos partidos, penalizados por **bajas** (lesiones/sanciones);
     distribución de marcadores **Poisson con corrección Dixon-Coles** → 1X2, más/menos, BTTS,
     doble oportunidad y hándicap.
   - Ambas: mezcla 65 % mercado / 35 % modelo. Si discrepan > 12 pp, el pick se descarta.
   - **Sin ninguna de las dos fuentes no se apuesta.**
3. **Valor esperado:** `EV = p × cuota − 1`. Se exige `EV ≥ MIN_EDGE` y `p ≥ MIN_PROBABILITY`.
   Con solo el modelo estadístico se exige el doble de EV. EV > 25 % se considera error de datos y se descarta.
4. **Stake 1-5:** Kelly fraccional (`KELLY_FRACTION`, 25 % por defecto) mapeado a 1–5; tope 4 sin
   modelo y 2 sin mercado de referencia.
5. **Selección:** mejor pick por partido, sin repetir partido, máximo `MAX_PICKS_PER_CYCLE` por
   ciclo y `MAX_PICKS_PER_DAY` por día.

Toda la evidencia usada (métricas + hechos) se pasa al LLM, que tiene prohibido inventar datos.
Si la API del LLM falla, se publica una justificación de plantilla construida solo con cifras reales.

---

## 3. Formato del mensaje

```
🏆 Fútbol / España - LaLiga
⚽️ Evento: Real Madrid vs Getafe
⏰ Hora: 18/10/2026 14:00 (-05)

🎯 Pronóstico: Más de 2.5 goles
📈 Cuota: 1.95
🏠 Casa de Apuestas: Betano
💼 Stake recomendado: 3

📊 Justificación:
<párrafo de 3-5 líneas generado por Claude>
```

El icono de la línea *Evento* se adapta al deporte (⚽️ / 🏀 / 🎾); la estructura es idéntica.

---

## 4. Instalación y configuración

### Requisitos
- Python 3.11+ (probado con 3.12/3.13)
- Un bot de Telegram y un canal
- Clave de API de Anthropic (Claude)
- **Muy recomendado:** clave de [The Odds API](https://the-odds-api.com) y de [API-Football](https://www.api-football.com)
- **Proxies residenciales del país** del dominio de Betano que uses (ver §6)

### Pasos

```bash
git clone <repo> && cd Smart
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env          # y rellena las variables
pytest                        # tests
python -m tipster --once --dry-run   # un ciclo sin publicar (ver logs)
python -m tipster                    # modo continuo
```

### Telegram
1. Habla con **@BotFather** → `/newbot` → copia el token en `TELEGRAM_BOT_TOKEN`.
2. Crea el canal y añade el bot como **administrador** con permiso para publicar.
3. `TELEGRAM_CHANNEL_ID`: `@nombre_canal` (público) o el id `-100…` (privado; puedes obtenerlo
   reenviando un mensaje del canal a @userinfobot).

### Variables de entorno principales

| Variable | Descripción | Defecto |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHANNEL_ID` | Credenciales y destino | — (obligatorias) |
| `ANTHROPIC_API_KEY` | Clave de Claude | — |
| `LLM_MODEL` / `LLM_EFFORT` | Modelo y esfuerzo de razonamiento | `claude-opus-5-5` / `medium` |
| `BETANO_BASE_URL` | Dominio Betano del país | `https://www.betano.pe` |
| `BETANO_SPORTS` | Deportes (slug de la URL) en JSON | `["futbol","baloncesto","tenis"]` |
| `BETANO_LEAGUE_PATHS` | Rutas de ligas concretas | `[]` |
| `ODDS_API_KEY` | Validación con mercado sharp | vacío |
| `API_FOOTBALL_KEY` | Forma, H2H, lesiones, xG | vacío |
| `PROXIES` | Lista JSON de proxies `http://user:pass@host:port` | `[]` (directo) |
| `MIN_ODDS` / `MAX_ODDS` | Rango de cuotas factibles | `1.50` / `2.30` |
| `MIN_EDGE` / `MIN_PROBABILITY` | Umbrales de valor | `0.03` / `0.45` |
| `KELLY_FRACTION` | Fracción de Kelly para el stake | `0.25` |
| `POLL_INTERVAL_SECONDS` | Intervalo del bucle | `600` |
| `TIMEZONE` | Zona horaria de la hora mostrada | `America/Lima` |

La lista completa está en `.env.example`. El LLM recibe además el parámetro de *fallback* del lado
servidor (`fallbacks: "default"`), de modo que si el modelo principal rechaza una petición la API
la reintenta con otro modelo en la misma llamada. Si falla todo, se usa la plantilla determinista.

---

## 5. Despliegue

### Docker (recomendado, VPS)
```bash
cp .env.example .env   # rellenar
docker compose up -d --build
docker compose logs -f tipster
```
`restart: unless-stopped` reinicia el contenedor ante caídas; `data/` (SQLite) y `logs/` quedan en
volúmenes del host.

### systemd (sin Docker)
```bash
sudo useradd -r -m tipster && sudo mkdir -p /opt/tipster && sudo cp -r . /opt/tipster
cd /opt/tipster && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
sudo cp deploy/tipster.service /etc/systemd/system/ && sudo chown -R tipster /opt/tipster
sudo systemctl daemon-reload && sudo systemctl enable --now tipster
journalctl -u tipster -f
```

### Cron (ejecuciones puntuales)
Ver `deploy/crontab.example`: ejecuta `python -m tipster --once` cada 10 min con `flock` para evitar
solapamientos.

### Monitorización
- Logs en consola y `logs/tipster.log` (rotación 5×5 MB). Cada ciclo registra eventos extraídos,
  candidatos (con p, EV y stake) y señales publicadas.
- Mensajes a vigilar: `Geo-bloqueo de Betano`, `Proxy … en cuarentena`, `Betano no devolvió eventos`,
  `EV anómalo`, `LLM error HTTP`.
- Histórico en SQLite (`signals`): rellena la columna `result` para calcular ROI/yield.

---

## 6. Notas sobre la extracción de Betano

- Betano **geo-bloquea** por país e IPs de datacenter (devuelve una *splash screen* con HTTP 403).
  Necesitas **proxies residenciales del país** del dominio configurado (`.pe` → Perú, `.mx` → México…).
  El agente lo detecta y lo indica en el log.
- Estrategia en cascada: API JSON interna (`/api/<ruta>?req=la,s,stnf,c,mb`) → HTML con
  `window["initial_state"]` → navegador headless (`BETANO_USE_BROWSER_FALLBACK=true`, requiere
  `pip install playwright && playwright install chromium`).
- El parser no depende de rutas fijas del JSON: detecta cualquier objeto con `markets` y
  `startTime`, normaliza mercados por código (`MRES`, `HCTG`, `BTSC`…) o por nombre en
  español/portugués/inglés, y descarta mercados de periodos parciales, córners y tarjetas.
- **Validación pendiente en producción:** el código se probó con un JSON de ejemplo que reproduce la
  estructura conocida del sitio, pero desde el entorno de desarrollo Betano respondió con geo-bloqueo,
  por lo que no se pudo verificar contra una respuesta real. Al desplegar, ejecuta
  `LOG_LEVEL=DEBUG python -m tipster --once --dry-run` y revisa que los eventos y mercados se
  parseen; si Betano cambió nombres de campos o códigos de mercado, ajusta `MARKET_CODES` /
  `MARKET_NAME_PATTERNS` en `tipster/scrapers/betano.py` y añade el JSON real a `tests/fixtures/`.

## 7. Extender

- **Nueva casa de referencia:** implementar `OddsProvider.fetch_events()` y añadirla en
  `agent._reference_events`.
- **Modelos para baloncesto/tenis:** hoy se apoyan en el consenso sharp; puedes añadir un
  proveedor de estadísticas (ratings Elo, ritmo/eficiencia ofensiva) y conectarlo en
  `ValueEngine._model_probability`.
- **Seguimiento de resultados:** un job que liquide `signals.result` y publique el balance semanal.
