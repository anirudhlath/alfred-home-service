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
Without Redis/Alfred reachable, the service still boots and serves `/health` — tool
registration with Alfred's registry just logs a warning and retries on its next refresh.

## Test / lint / type

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy app/ alfred_ext/
uv run pytest -q
```

## Gotchas

- `alfred-sdk` is NOT on PyPI — CI and fresh installs resolve it via
  `tool.uv.sources`-less direct git reference in the `alfred` extra
  (`alfred-sdk @ git+https://github.com/anirudhlath/alfred@v0.1.0#subdirectory=sdk` in
  `pyproject.toml`); container builds instead copy the source in directly (see
  Containerfile).
- To develop against unreleased local SDK changes, run
  `uv pip install -e ../alfred/sdk` after `uv sync` — this overlays the editable local
  checkout into the venv (verified: `alfred_sdk.__file__` then resolves under
  `alfred/sdk/`, not the pinned git tag). Any subsequent `uv sync` reinstalls the pinned
  `v0.1.0` git ref and silently drops the overlay, so re-run the `uv pip install -e` line
  after every sync while iterating.
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
