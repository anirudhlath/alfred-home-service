# alfred-home-service

An async microservice that owns all Home Assistant communication for
[Alfred](https://github.com/anirudhlath/alfred). It connects to HA over the
**WebSocket API**, discovers every entity/device/area, **generates** its tool
surface from HA's own service catalog, forwards every state change onto
Alfred's event bus, and accepts credentials pushed at runtime from Alfred's UI.

Built with FastAPI + websockets + aiomqtt, packaged with
[uv](https://docs.astral.sh/uv/), Python 3.13+.

## How it fits into Alfred

```
┌──────────────┐  tool manifest + context    ┌──────────────┐                    ┌────────────────┐
│    Alfred    │ ◄────── via Redis ───────── │ home-service │  WebSocket (auth,  │ Home Assistant │
│  Home Agent  │                             │   (FastAPI)  │  events, registry, │                │
│              │ ── POST /mcp (JSON-RPC) ──► │              │  call_service) ──► │                │
│  Alfred core │ ── POST /credentials ─────► │              │                    │                │
└──────────────┘                             └──────┬───────┘                    └────────────────┘
                                                    │ every state_changed
                                                    ▼
                                       MQTT home/state_changed
                                       (bridge → alfred:home:state_changed)
```

1. **Credentials** — Alfred's settings UI shows a Home Assistant card (this
   service registers a `credentials_schema` with fields `url` + `token`).
   Saving pushes `POST /credentials {url, token}`; the service connects live
   and returns its resulting health. `.env` `HA_HOST`/`HA_TOKEN` remain a dev
   fallback. Credentials are held in memory only — on restart, Alfred's core
   re-pushes them (ServiceRegistered event).
2. **Discovery** — on connect, the service subscribes to `state_changed` and
   registry-updated events and fetches the entity/device/area registries plus
   the service catalog. The `EntityIndex` resolves areas and friendly names to
   real entity IDs (no name-guessing). Renames/additions in HA are picked up
   live; a NEW integration domain requires a service restart.
3. **Generated capabilities** — `CapabilityGenerator` crosses the service
   catalog with discovered entities. Compact `audience: reflex` tools (lights,
   switches, media players, scenes) carry live area/entity values in their
   parameter descriptions; every other domain with entities gets
   `audience: conscious` tools plus a generic `home.call_service` escape
   hatch. Risk tiers come from `config/risk_map.yaml` (data, not code).
4. **State ingest** — every `state_changed` becomes a bus-schema
   `StateChangedEvent` published to MQTT `home/state_changed`. No HA-side
   automation is required anymore.

## API

| Endpoint | Method | Description |
|---|---|---|
| `/mcp` | POST | JSON-RPC-style tool call: `{"method": "home.light_turn_on", "params": {"target": "Living Room", "brightness_pct": 40}, "id": "req-001"}` → `{"id": "req-001", "result": {...}, "error": null}`. Errors in-band (HTTP 200). |
| `/credentials` | POST | `{"url": "...", "token": "..."}` → applies live, returns `{"status": "ok", "health": ...}`. 422 on unknown/missing fields. Trusted network only. |
| `/health` | GET | `{"status": "ok", "service": "home-service", "ha": {"state": "connected"\|"auth_failed"\|"unreachable"\|"disconnected", "entities": N, "areas": N, "last_event_age_s": ...}}` |

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `HA_HOST` | *(unset)* | Dev fallback HA base URL (UI-pushed credentials are authoritative) |
| `HA_TOKEN` | *(unset)* | Dev fallback long-lived access token |
| `SERVICE_HOST` | `localhost` | Hostname Alfred uses to reach `/mcp` and `/credentials` |
| `REDIS_URL` | `redis://localhost:6379` | Alfred's tool-registry Redis (alfred-sdk) |
| `MQTT_HOST` | `localhost` | MQTT broker for state forwarding |
| `MQTT_PORT` | `1883` | MQTT broker port |

Risk/audience tuning lives in `config/risk_map.yaml` and
`config/reflex_tools.yaml` — edit YAML, restart, no code changes.

## Run locally

```bash
uv venv --python 3.13
uv pip install -e ".[dev]" ../alfred/sdk   # adjust path to your alfred checkout
uv run uvicorn app.server:app --host 0.0.0.0 --port 8000
```

alfred-sdk is required (installed from the alfred monorepo source — not on
PyPI). Redis/MQTT are best-effort: without them the service still boots and
serves `/health`, `/credentials`, `/mcp`.

## Tests & quality gates

```bash
uv run pytest
uv run ruff check . && uv run ruff format .
uv run mypy --strict app alfred_ext
```

Tests run a fake HA WebSocket server in-process — no live Home Assistant,
Redis, or MQTT needed.

## Security notes

- The HA token is held in memory only (pushed) or read from env (dev); never
  written to disk here. Alfred core keeps the durable copy in the OS keyring.
- `/mcp` and `/credentials` are unauthenticated by design — deploy on a
  trusted private network alongside Alfred. Do not expose to the internet.

## License

MIT — see [LICENSE](LICENSE).
