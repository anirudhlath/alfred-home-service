"""home-service FastAPI server — MCP dispatch, credentials, health.

Composition root: wires HAConnection → EntityIndex / CapabilityGenerator /
StateForwarder / ContextRefresher, and registers the generated tool surface
with Alfred via the SDK.

The /mcp JSON-RPC contract ({method, params, id} → {id, result, error}) is
unchanged from Alfred HomeAgent's perspective.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from alfred_sdk import AlfredClient
from dotenv import load_dotenv
from fastapi import FastAPI
from loguru import logger
from pydantic import BaseModel, ConfigDict

from alfred_ext.register import build_client
from app.capability_generator import CapabilityGenerator
from app.entity_index import EntityIndex
from app.ha_connection import HAConnection
from app.home_feature import HomeCapabilitiesContext, HomeCapabilitiesFeature
from app.risk_map import RiskMap, load_reflex_config
from app.state_forwarder import StateForwarder

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# Keep the periodic re-registration: refreshes the tool-registry entry and the
# 10-min-TTL context key. LIVE context freshness comes from ContextRefresher.
ENTITY_REFRESH_INTERVAL = 300.0
CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


class McpRequest(BaseModel):
    """JSON-RPC style MCP tool call request."""

    method: str
    params: dict[str, Any] = {}
    id: str


class McpResponse(BaseModel):
    """JSON-RPC style MCP tool call response."""

    id: str
    result: dict[str, Any] | None = None
    error: str | None = None


class CredentialsBody(BaseModel):
    """POST /credentials body — field names match the CredentialSchema (contract C4)."""

    model_config = ConfigDict(extra="forbid")

    url: str = "http://homeassistant.local:8123"
    token: str


class ContextRefresher:
    """Debounced live context refresh — re-registers with Alfred after state events.

    Event-driven with coalescing (not polling): the first event schedules one
    refresh min_interval later; events during the window ride along.
    """

    def __init__(self, client: AlfredClient, min_interval: float = 2.0) -> None:
        self._client = client
        self._min_interval = min_interval
        self._pending: asyncio.Task[None] | None = None

    async def on_state_changed(
        self,
        entity_id: str,
        old_state: str | None,
        new_state: str | None,
        attributes: dict[str, Any],
    ) -> None:
        if self._pending is None or self._pending.done():
            self._pending = asyncio.create_task(self._refresh_soon(), name="context-refresh")

    async def _refresh_soon(self) -> None:
        await asyncio.sleep(self._min_interval)
        try:
            await self._client.register()
        except Exception as exc:
            logger.warning("Context refresh failed: {}", exc)

    async def stop(self) -> None:
        if self._pending is not None and not self._pending.done():
            self._pending.cancel()
            with contextlib.suppress(BaseException):
                await self._pending


def health_payload(conn: HAConnection, index: EntityIndex) -> dict[str, Any]:
    """Contract C6 health payload."""
    connected = conn.conn_state == "connected"
    return {
        "status": "ok",
        "service": "home-service",
        "ha": {
            "state": conn.conn_state,
            "entities": index.entity_count() if connected else 0,
            "areas": index.area_count() if connected else 0,
            "last_event_age_s": conn.last_event_age_s(),
        },
    }


async def apply_env_credentials(conn: HAConnection) -> None:
    """Dev fallback: HA_HOST/HA_TOKEN from .env when nothing has been pushed."""
    url = os.getenv("HA_HOST", "")
    token = os.getenv("HA_TOKEN", "")
    if not url or not token:
        logger.info("No HA credentials in environment — waiting for POST /credentials")
        return
    state = await conn.apply_credentials(url, token)
    logger.info("Applied HA credentials from environment — state: {}", state)


def create_app() -> FastAPI:
    conn = HAConnection()
    index = EntityIndex()
    forwarder = StateForwarder()
    client = build_client()
    generator = CapabilityGenerator(
        RiskMap.load(CONFIG_DIR / "risk_map.yaml"),
        load_reflex_config(CONFIG_DIR / "reflex_tools.yaml"),
    )
    refresher = ContextRefresher(client)

    conn.add_state_listener(forwarder.on_state_changed)
    conn.add_state_listener(refresher.on_state_changed)

    async def try_register() -> None:
        try:
            await client.register()
        except Exception as exc:
            logger.warning("Could not register with Alfred (best-effort): {}", exc)

    async def rebuild_index() -> None:
        index.rebuild(
            entity_registry=conn.entity_registry,
            device_registry=conn.device_registry,
            area_registry=conn.area_registry,
            states=conn.states,
        )

    async def refresh_loop() -> None:
        while True:
            await asyncio.sleep(ENTITY_REFRESH_INTERVAL)
            await try_register()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # Register even with zero features so the credentials card appears in the UI.
        await try_register()
        await forwarder.start()
        env_task = asyncio.create_task(apply_env_credentials(conn), name="env-credentials")
        refresh_task = asyncio.create_task(refresh_loop(), name="register-refresh")
        yield
        for task in (env_task, refresh_task):
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
        await refresher.stop()
        await forwarder.stop()
        await conn.stop()
        try:
            await client.unregister()
        except Exception as exc:
            logger.warning("Could not unregister from Alfred: {}", exc)

    app = FastAPI(title="home-service", lifespan=lifespan)
    app.state.ha = conn
    app.state.index = index
    app.state.client = client
    app.state.forwarder = forwarder
    app.state.refresher = refresher
    app.state.capabilities_ready = False

    async def on_connect() -> None:
        await rebuild_index()
        if not app.state.capabilities_ready:
            specs = generator.generate(conn.services_catalog, index)
            ctx = HomeCapabilitiesContext(conn=conn, index=index, generator=generator, specs=specs)
            client.discover_features_from_classes([HomeCapabilitiesFeature], ctx=ctx)
            app.state.capabilities_ready = True
            logger.info(
                "Generated {} tools across {} domains from the HA service catalog",
                len(specs),
                len({s.domain for s in specs if s.domain}),
            )
        else:
            logger.info(
                "Reconnected to HA — capability set is frozen for this process; "
                "restart if the HA instance or its service catalog changed"
            )
        await try_register()

    async def on_registries_updated() -> None:
        await rebuild_index()
        await try_register()

    conn.add_connect_listener(on_connect)
    conn.add_registry_listener(on_registries_updated)

    @app.post("/mcp")
    async def mcp_endpoint(request: McpRequest) -> McpResponse:
        """Handle an MCP tool call — contract unchanged for Alfred's HomeAgent."""
        try:
            result = await client.dispatch(request.method, request.params)
            return McpResponse(
                id=request.id,
                result=result if isinstance(result, dict) else {"data": result},
            )
        except KeyError as exc:
            return McpResponse(id=request.id, error=str(exc))
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
            logger.error("Tool execution failed: {}", message)
            return McpResponse(id=request.id, error=message)

    @app.post("/credentials")
    async def credentials_endpoint(body: CredentialsBody) -> dict[str, Any]:
        """Apply pushed credentials live; return resulting health (contract C4)."""
        state = await conn.apply_credentials(body.url, body.token)
        logger.info("Credentials applied — HA state: {}", state)
        return {"status": "ok", "health": health_payload(conn, index)}

    @app.get("/health")
    async def health() -> dict[str, Any]:
        """Contract C6 health endpoint."""
        return health_payload(conn, index)

    return app


app = create_app()
