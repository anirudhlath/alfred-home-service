# alfred-home-service

A small async microservice that wraps the [Home Assistant](https://www.home-assistant.io/) REST API and exposes smart-home tools to [Alfred](https://github.com/anirudhlath/alfred) — a multi-agent home assistant — over an MCP-style JSON-RPC endpoint.

Built with FastAPI + httpx, packaged with [uv](https://docs.astral.sh/uv/), Python 3.13+.

## How it fits into Alfred

Alfred never talks to Home Assistant directly. This service is the sovereign owner of all Home Assistant communication; Alfred reaches smart-home devices only through it, via the `alfred-sdk` bridge:

```
┌──────────────┐  tool manifest + context   ┌──────────────┐                 ┌────────────────┐
│    Alfred    │ ◄────── via Redis ──────── │ home-service │  REST (httpx)   │ Home Assistant │
│  Home Agent  │                            │   (FastAPI)  │ ──────────────► │                │
│              │ ── POST /mcp (JSON-RPC) ─► │              │                 │                │
└──────────────┘                            └──────────────┘                 └────────────────┘
```

1. **Registration** — on startup, the service uses `alfred_sdk.AlfredClient` to publish its tool manifest into Alfred's Redis tool registry, along with a context snapshot of live entity states (lights, scenes). A background loop re-registers every 5 minutes so the context stays fresh.
2. **Dispatch** — Alfred's Home Agent selects a tool (e.g. `lighting.dim_lights`) and POSTs a JSON-RPC-style request to this service's `/mcp` endpoint. The service dispatches to the matching feature method and returns the result.
3. **Execution** — feature methods call Home Assistant's REST API (`/api/states`, `/api/services/<domain>/<service>`) through a shared async `httpx` client.

The core app (`app/`) has **zero Alfred dependencies** — `HomeAssistantClient` and the FastAPI server run standalone. The Alfred integration lives entirely in `alfred_ext/` and activates only when `alfred-sdk` is installed (without it, the server still boots and serves `/health`, but tool registration and `/mcp` dispatch are unavailable).

## Plugin architecture: `BaseFeature`

Capabilities are plugins in `alfred_ext/features/`. Each feature subclasses `alfred_sdk.BaseFeature`, declares a `feature_name`, exposes tools with the `@tool` decorator, and reports live entity state via `get_context()`:

```python
class LightingFeature(BaseFeature):
    feature_name = "lighting"

    def __init__(self, ctx):          # ctx.ha is the shared HomeAssistantClient
        super().__init__()
        self.ha = ctx.ha

    async def get_context(self) -> ContextSnapshot:
        return await context_for_domain(self.ha, "light")

    @tool
    async def dim_lights(self, room: str, level: int) -> dict:
        """Dim the lights in a room. ..."""
```

Features are auto-discovered: `alfred_ext/register.py` scans the `alfred_ext.features` package with `client.discover_features()`, so adding a capability means dropping a new module into `alfred_ext/features/` — no registration boilerplate. Tool names are namespaced as `<feature_name>.<method>` and tool schemas are derived from the method signatures and docstrings.

Built-in features:

| Feature | Tools | What it does |
|---|---|---|
| `lighting` | `lighting.dim_lights`, `lighting.turn_off_lights` | Brightness control and on/off per room |
| `scenes` | `scenes.set_scene` | Activate Home Assistant scenes |

## API

| Endpoint | Method | Description |
|---|---|---|
| `/mcp` | POST | JSON-RPC-style tool call: `{"method": "lighting.dim_lights", "params": {"room": "living room", "level": 40}, "id": "req-001"}` → `{"id": "req-001", "result": {...}, "error": null}` |
| `/health` | GET | `{"status": "ok", "service": "home-service"}` |

Errors are returned in-band (HTTP 200 with an `error` field), matching JSON-RPC conventions.

## Quickstart

### Prerequisites

- Python 3.13+ and [uv](https://docs.astral.sh/uv/)
- A Home Assistant instance and a [long-lived access token](https://www.home-assistant.io/docs/authentication/#your-account-profile)
- For full Alfred integration: a Redis instance and a checkout of the [alfred](https://github.com/anirudhlath/alfred) repo (`alfred-sdk` lives at `alfred/sdk/` and is not on PyPI)

### Configuration

All configuration is via environment variables (or a git-ignored `.env` file in the repo root, loaded automatically). The Home Assistant token is never stored in the repo.

| Variable | Default | Purpose |
|---|---|---|
| `HA_HOST` | `http://homeassistant.local:8123` | Home Assistant base URL |
| `HA_TOKEN` | *(required)* | Home Assistant long-lived access token |
| `SERVICE_HOST` | `localhost` | Hostname Alfred uses to reach this service's `/mcp` endpoint |
| `REDIS_URL` | `redis://localhost:6379` | Alfred's tool-registry Redis (used by `alfred-sdk`) |

### Run locally

```bash
uv venv --python 3.13
uv pip install -e ".[dev]" ../alfred/sdk   # adjust path to your alfred checkout
HA_TOKEN=<your-token> .venv/bin/uvicorn app.server:app --host 0.0.0.0 --port 8000
```

If Alfred's Redis isn't reachable, the service logs a warning and keeps serving — registration is best-effort by design.

### Run in a container

The `Containerfile` builds the service together with `alfred-sdk` from monorepo source. The build context must be the workspace root that contains both `alfred/` and `home-service/`:

```bash
podman build -f home-service/Containerfile -t home-service .
podman run -e HA_HOST=http://homeassistant.local:8123 -e HA_TOKEN=<your-token> \
  -e SERVICE_HOST=<host-alfred-can-reach> -e REDIS_URL=redis://<alfred-redis>:6379 \
  -p 8000:8000 home-service
```

## Tests

```bash
uv venv --python 3.13
uv pip install -e ".[dev]" ../alfred/sdk
.venv/bin/pytest
```

Tests mock the Home Assistant API with `unittest.mock` and drive the FastAPI app in-process via httpx's `ASGITransport` — no live Home Assistant or Redis needed.

## Security notes

- The HA token is read from the environment only; `.env` is git-ignored.
- `/mcp` is unauthenticated by design — deploy on a trusted private network alongside Alfred. Do not expose it to the public internet.

## License

MIT — see [LICENSE](LICENSE).
