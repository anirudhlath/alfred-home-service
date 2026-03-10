# home-service — HA wrapper microservice
# Build context must be the workspace root (parent of alfred/ and home-service/)
# so we can access alfred/sdk/ for the unpublished alfred-sdk package.

FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install alfred-sdk from monorepo source (not on PyPI)
COPY alfred/sdk/ /tmp/alfred-sdk/
RUN uv pip install --system --no-cache /tmp/alfred-sdk/ && rm -rf /tmp/alfred-sdk/

# Install home-service dependencies
COPY home-service/pyproject.toml /app/
COPY home-service/app/ /app/app/
COPY home-service/alfred_ext/ /app/alfred_ext/
RUN uv pip install --system --no-cache .

EXPOSE 8000

CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8000"]
