# home-service — HA wrapper microservice
# Build context must be the workspace root (parent of alfred/ and home-service/)
# so we can access alfred/sdk/ for the unpublished alfred-sdk package.

FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install alfred-sdk from monorepo source (not on PyPI). Installing it first
# means the alfred-sdk requirement in pyproject is already satisfied below.
COPY alfred/sdk/ /tmp/alfred-sdk/
RUN uv pip install --system --no-cache /tmp/alfred-sdk/ && rm -rf /tmp/alfred-sdk/

# Install home-service. --no-sources makes uv skip pyproject's [tool.uv.sources]
# git pin and use the alfred-sdk already installed from the copied source above
# (the git pin is for dev/CI 'uv sync' only). Otherwise uv would git-clone the
# pinned rev and discard the local SDK copied in the step above.
COPY home-service/pyproject.toml /app/
COPY home-service/app/ /app/app/
COPY home-service/alfred_ext/ /app/alfred_ext/
COPY home-service/config/ /app/config/
RUN uv pip install --system --no-cache --no-sources .

EXPOSE 8000

CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8000"]
