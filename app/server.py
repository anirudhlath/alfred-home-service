"""home-service FastAPI server — MCP dispatch, credentials, health.

Composition root: wires HAConnection → EntityIndex / CapabilityGenerator /
StateForwarder / LiveStateWriter, and registers the generated tool surface
with Alfred via the SDK.

Alfred hears from this service on events only. It registers at startup, on each
HA connect and when HA's registries change, and it writes live state as HA
reports changes (Alfred issue #281). Nothing here runs on a schedule; only a
failed registration is retried, with backoff.

The /mcp JSON-RPC contract ({method, params, id} → {id, result, error}) is
unchanged from Alfred HomeAgent's perspective.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from alfred_sdk import AlfredClient
from alfred_sdk.live_state import LiveStateWriter
from dotenv import load_dotenv
from fastapi import FastAPI
from loguru import logger
from pydantic import BaseModel, ConfigDict

from alfred_ext.register import build_client
from app.capability_generator import CapabilityGenerator
from app.entity_index import EntityIndex
from app.ha_connection import HAConnection
from app.home_feature import HomeCapabilitiesContext, HomeCapabilitiesFeature
from app.live_state import LiveStatePublisher
from app.risk_map import RiskMap, load_reflex_config
from app.state_forwarder import StateForwarder
from app.tasks import cancel_and_wait

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

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


async def _run_all(*steps: Callable[[], Awaitable[None]]) -> None:
    """Await each step in turn; a cancellation arriving part-way is raised after the last."""
    cancelled: asyncio.CancelledError | None = None
    for step in steps:
        try:
            await step()
        except asyncio.CancelledError as exc:
            cancelled = cancelled or exc
    if cancelled is not None:
        raise cancelled


class Registrar:
    """Registers with Alfred on demand; a failed attempt retries until one lands.

    Not a refresh loop: once a registration succeeds, nothing stays scheduled. A
    registration requested while a retry is pending runs as soon as any attempt in
    flight ends, and its success cancels the retry. This keeps a service started while
    Redis is unreachable from staying unregistered — and Alfred pushes credentials only
    in answer to ServiceRegistered.
    """

    def __init__(
        self,
        client: AlfredClient,
        *,
        initial_backoff: float = 1.0,
        max_backoff: float = 60.0,
    ) -> None:
        self._client = client
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._retry: asyncio.Task[None] | None = None
        self._attempt_lock = asyncio.Lock()

    async def register(self) -> None:
        if await self._attempt():
            await self.stop()
        elif self._retry is None or self._retry.done():
            self._retry = asyncio.create_task(self._retry_until_registered(), name="register-retry")

    async def _attempt(self) -> bool:
        # One attempt at a time, in the order asked for (on_connect and a registry change
        # can overlap). A success therefore lands after every attempt that started before
        # it, so the stop() that follows cancels only a retry those left, never one a
        # later failure scheduled for a newer manifest.
        async with self._attempt_lock:
            try:
                await self._client.register()
            except Exception as exc:
                logger.warning("Could not register with Alfred: {}", exc)
                return False
            return True

    async def _retry_until_registered(self) -> None:
        delay = self._initial_backoff
        while True:
            await asyncio.sleep(delay)
            if await self._attempt():
                return
            delay = min(delay * 2, self._max_backoff)

    async def stop(self) -> None:
        """Cancel a pending retry and wait for it to end."""
        # Let go of it before waiting: a registration that fails meanwhile then schedules
        # a retry of its own, which stays tracked instead of being dropped on our return.
        retry, self._retry = self._retry, None
        await cancel_and_wait(retry)


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
    live_state = LiveStateWriter(client.redis_url, client.service_name)
    registrar = Registrar(client)
    generator = CapabilityGenerator(
        RiskMap.load(CONFIG_DIR / "risk_map.yaml"),
        load_reflex_config(CONFIG_DIR / "reflex_tools.yaml"),
    )

    publisher = LiveStatePublisher(live_state, conn)

    conn.add_state_listener(forwarder.on_state_changed)
    conn.add_state_listener(publisher.on_state_changed)
    conn.add_disconnect_listener(publisher.clear)

    async def rebuild_index() -> None:
        index.rebuild(
            entity_registry=conn.entity_registry,
            device_registry=conn.device_registry,
            area_registry=conn.area_registry,
            states=conn.states,
        )

    async def unregister() -> None:
        try:
            await client.unregister()
        except Exception as exc:
            logger.warning("Could not unregister from Alfred: {}", exc)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        # A process killed before it could clear on disconnect left its hash behind.
        await publisher.clear()
        # Register even with zero features so the credentials card appears in the UI.
        await registrar.register()
        await forwarder.start()
        env_task = asyncio.create_task(apply_env_credentials(conn), name="env-credentials")
        yield
        # Every step runs even if shutdown is cancelled part-way; the cancellation is
        # raised after the last. HA stops before the registrar and the final clear, so no
        # HA listener can schedule a registration retry or write live state after them.
        await _run_all(
            lambda: cancel_and_wait(env_task),
            forwarder.stop,
            conn.stop,
            registrar.stop,
            publisher.clear,
            unregister,
            live_state.aclose,
        )

    app = FastAPI(title="home-service", lifespan=lifespan)
    app.state.ha = conn
    app.state.index = index
    app.state.client = client
    app.state.forwarder = forwarder
    app.state.live_state = live_state
    app.state.live_state_publisher = publisher
    app.state.registrar = registrar
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
        await publisher.publish()
        await registrar.register()

    async def on_registries_updated() -> None:
        # Never writes live state: this runs in HAConnection's registry-refresh task, which
        # a disconnect does not cancel, so a write here could land after the disconnect's
        # clear and bring back state for a connection that is gone.
        await rebuild_index()
        await registrar.register()

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
