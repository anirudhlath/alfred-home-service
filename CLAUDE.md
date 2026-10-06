# Alfred Home Service

FastAPI microservice wrapping Home Assistant for the Alfred multi-agent system. A
**sovereign app**: it must run and be useful without Alfred; its ONLY coupling to Alfred
is the `alfred-sdk` package (never import from the alfred monorepo directly).

## Run

```bash
uv venv --python 3.13 && uv sync --all-extras
uv run uvicorn app.server:app --port 8000
```

Requires a `.env` (python-dotenv loads it — `os.getenv` alone does NOT): `HA_HOST`,
`HA_TOKEN` for the Home Assistant instance. Never commit `.env` (already gitignored).
Without Redis/Alfred reachable, the service still boots and serves `/health` —
registration with Alfred logs a warning and retries with backoff (1 s doubling to 60 s)
until it lands.

## Test / lint / type

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy app/ alfred_ext/
uv run pytest -q
```

## Alfred lifecycle (alfred#281)

Nothing here runs on a schedule; only a failed registration is retried, with backoff.
Alfred hears from this service on events:

| Moment | Live state (`LiveStatePublisher`) | Registration |
|---|---|---|
| Startup | `clear()` (a killed run may have left its hash) | `register()` |
| HA connected | rebuild the entity index, then a full `replace()` from `conn.states` — published even if the rebuild fails | then generate capabilities (first connect only), then `register()` |
| HA `state_changed` | `update()`, or `remove()` if HA deleted the entity; a full `replace()` instead while the hash is dirty and HA is connected | — |
| HA registry change | — (the index is rebuilt; live state is not written) | `register()` |
| HA closed / unreachable / token rejected | `clear()` (the disconnect listener; `conn.stop()` does not fire it) | — |
| A registration lands (any of the above) | heals a dirty hash: `replace()` if HA is connected, `clear()` if not | — |
| Shutdown | `clear()`, then the writer's `aclose()` last | `unregister()`, between the two |

Any failed live-state write marks the hash dirty (one WARNING); any `replace()` or
`clear()` that lands marks it clean again (one INFO). One double fault never heals on its
own: HA away *and* the disconnect's `clear()` failed — the hash keeps the last state until
HA reconnects or the service restarts (or a registration retry already pending when Redis
returns).

Registrations are serialised (one attempt at a time, in the order asked for). A failed
one retries with backoff (1 s doubling to 60 s); once one lands, nothing stays scheduled.

Shutdown order (`lifespan` in `app/server.py`): cancel the env-credentials task, stop the
state forwarder, stop the HA connection (so no listener can write live state or schedule a
registration after the clear), cancel a pending registration retry, `clear()`,
`unregister()`, then `aclose()`. Every step runs even if shutdown is cancelled part-way;
the cancellation is raised after the last.

The key and the entry format belong to Alfred (`alfred_sdk.live_state`). This service
only builds entries, in `app/live_state.py`.

## Gotchas

- `alfred-sdk` is NOT on PyPI. It is a base dependency (`alfred-sdk>=0.1.0` in
  `[project.dependencies]`; the only extra is `dev`), and `[tool.uv.sources]` in
  `pyproject.toml` pins it to an alfred commit (`git` + `rev` + `subdirectory = "sdk"`),
  which `uv.lock` records — CI and fresh `uv sync` installs resolve that commit. Container
  builds instead install the SDK from the copied alfred source and then install this
  package with `--no-sources`, skipping the pin (see Containerfile).
- To develop against unreleased local SDK changes, run
  `uv pip install -e ../alfred/sdk` after `uv sync` — this overlays the editable local
  checkout into the venv (verified: `alfred_sdk.__file__` then resolves under
  `alfred/sdk/`, not `.venv`). Any subsequent `uv sync` reinstalls the pinned git commit
  and silently drops the overlay, so re-run the `uv pip install -e` line after every sync
  while iterating.
- `alfred-sdk` ships no `py.typed` marker yet (upstream gap in `alfred/sdk`), so only
  `alfred_sdk.*` (not `alfred_ext/`) is exempted from mypy via a `follow_imports = "skip"`
  override in `pyproject.toml`. `alfred_ext/` (the optional Alfred-integration layer) is
  first-party and IS checked by CI (`mypy-targets: "app/ alfred_ext/"`) — where
  `BaseFeature`'s untyped `Any` leaks into subclasses/decorators, narrow
  `# type: ignore[misc]` / `# type: ignore[untyped-decorator]` comments suppress just
  those lines. Remove the override and the targeted ignores once alfred-sdk ships types.
- `httpx.AsyncClient` must be long-lived — never create per-request.
- The Plan 2 rewrite (`alfred/docs/superpowers/plans/2026-07-15-ha-plan2-home-service-rewrite.md`)
  will reshape this service around SDK `credentials_schema`/`credentials_endpoint`.
- PRs: `<type>/<slug>` branch, conventional PR title, squash-only, `ci-ok` required.
