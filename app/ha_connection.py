"""Persistent WebSocket connection to Home Assistant (contract C12).

Message shapes verified against https://developers.home-assistant.io/docs/api/websocket
and HA core source — see the implementation plan's "Verified HA WebSocket API
message shapes" section for the exact envelopes.

Design notes:
- One client-chosen increasing integer id per command; results are correlated
  back to awaiting futures (`_pending`).
- Listeners are awaited inside the reader loop, so they MUST NOT issue
  WebSocket commands (would deadlock the correlation loop). Registry refresh
  therefore runs as a separate coalesced task.
- `auth_invalid` is terminal: no retry until `apply_credentials` is called
  again with new credentials.
- The `websockets` library handles ping/pong keepalive automatically.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from loguru import logger
from pydantic import BaseModel, Field
from websockets.asyncio.client import ClientConnection, connect

ConnState = Literal["connected", "auth_failed", "unreachable", "disconnected"]

StateListener = Callable[[str, str | None, str | None, dict[str, Any]], Awaitable[None]]
VoidListener = Callable[[], Awaitable[None]]

COMMAND_TIMEOUT = 30.0

SUBSCRIBED_EVENTS = (
    "state_changed",
    "entity_registry_updated",
    "device_registry_updated",
    "area_registry_updated",
)


class HAAuthError(Exception):
    """Home Assistant rejected the access token (auth_invalid)."""


class HACommandError(Exception):
    """A WebSocket command failed or could not be sent."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class HAEntityState(BaseModel):
    """One entity's live state (mirror of an /api/states item)."""

    entity_id: str
    state: str
    attributes: dict[str, Any] = Field(default_factory=dict)


class HAConnection:
    """Owns the WebSocket to HA: auth, subscriptions, fetches, service calls."""

    def __init__(self, *, initial_backoff: float = 1.0, max_backoff: float = 60.0) -> None:
        self._initial_backoff = initial_backoff
        self._max_backoff = max_backoff
        self._url = ""
        self._token = ""
        self._ws: ClientConnection | None = None
        self._task: asyncio.Task[None] | None = None
        self._registry_refresh_task: asyncio.Task[None] | None = None
        self._registry_dirty = False
        self._next_msg_id = 1
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._attempt_done = asyncio.Event()

        self.conn_state: ConnState = "disconnected"
        self.states: dict[str, HAEntityState] = {}
        self.services_catalog: dict[str, Any] = {}
        self.entity_registry: list[dict[str, Any]] = []
        self.device_registry: list[dict[str, Any]] = []
        self.area_registry: list[dict[str, Any]] = []

        self._last_event_monotonic: float | None = None
        self._state_listeners: list[StateListener] = []
        self._registry_listeners: list[VoidListener] = []
        self._connect_listeners: list[VoidListener] = []

    # ── listeners ──

    def add_state_listener(self, cb: StateListener) -> None:
        self._state_listeners.append(cb)

    def add_registry_listener(self, cb: VoidListener) -> None:
        self._registry_listeners.append(cb)

    def add_connect_listener(self, cb: VoidListener) -> None:
        self._connect_listeners.append(cb)

    def last_event_age_s(self) -> float | None:
        if self._last_event_monotonic is None:
            return None
        return round(time.monotonic() - self._last_event_monotonic, 1)

    # ── lifecycle ──

    async def apply_credentials(self, url: str, token: str) -> ConnState:
        """Apply (or replace) credentials and (re)connect.

        Returns the connection state after the first attempt completes, so the
        caller (POST /credentials) can immediately report connected-or-failed.
        """
        await self.stop()
        self._url = url.rstrip("/")
        self._token = token
        self.conn_state = "disconnected"
        self._attempt_done = asyncio.Event()
        self._task = asyncio.create_task(self._run(), name="ha-connection")
        await self._attempt_done.wait()
        return self.conn_state

    async def stop(self) -> None:
        for task in (self._task, self._registry_refresh_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(BaseException):
                    await task
        self._task = None
        self._registry_refresh_task = None
        self._ws = None

    def _ws_url(self) -> str:
        if self._url.startswith("https://"):
            return "wss://" + self._url.removeprefix("https://") + "/api/websocket"
        return "ws://" + self._url.removeprefix("http://") + "/api/websocket"

    # ── connection loop ──

    async def _run(self) -> None:
        backoff = self._initial_backoff
        while True:
            try:
                async with connect(self._ws_url(), max_size=None) as ws:
                    self._ws = ws
                    await self._handshake(ws)
                    backoff = self._initial_backoff
                    setup = asyncio.create_task(self._on_connected(ws))
                    try:
                        async for raw in ws:
                            await self._handle_message(json.loads(raw))
                    finally:
                        setup.cancel()
                        self._ws = None
                        self._fail_pending()
                self.conn_state = "unreachable"
                logger.warning("HA WebSocket closed — reconnecting in {:.1f}s", backoff)
            except HAAuthError as exc:
                self.conn_state = "auth_failed"
                logger.error("HA rejected token ({}) — waiting for new credentials", exc)
                self._attempt_done.set()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.conn_state = "unreachable"
                logger.warning(
                    "HA connection failed ({}: {}) — retrying in {:.1f}s",
                    type(exc).__name__,
                    exc,
                    backoff,
                )
            self._attempt_done.set()
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, self._max_backoff)

    async def _handshake(self, ws: ClientConnection) -> None:
        first = json.loads(await ws.recv())
        if first.get("type") != "auth_required":
            raise HACommandError("protocol", f"expected auth_required, got {first.get('type')}")
        await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
        resp = json.loads(await ws.recv())
        if resp.get("type") == "auth_invalid":
            raise HAAuthError(str(resp.get("message", "invalid token")))
        if resp.get("type") != "auth_ok":
            raise HACommandError("protocol", f"expected auth_ok, got {resp.get('type')}")

    async def _on_connected(self, ws: ClientConnection) -> None:
        """Post-auth setup: subscribe, refresh registries/states/catalog, notify."""
        try:
            for event_type in SUBSCRIBED_EVENTS:
                await self._cmd(ws, {"type": "subscribe_events", "event_type": event_type})
            await self._refresh_registries(ws)
            raw_states: list[dict[str, Any]] = await self._cmd(ws, {"type": "get_states"}) or []
            self.states = {
                s["entity_id"]: HAEntityState(
                    entity_id=s["entity_id"],
                    state=s.get("state", "unknown"),
                    attributes=s.get("attributes", {}),
                )
                for s in raw_states
            }
            self.services_catalog = await self._cmd(ws, {"type": "get_services"}) or {}
            self.conn_state = "connected"
            logger.info(
                "Connected to HA: {} entities, {} areas, {} service domains",
                len(self.states),
                len(self.area_registry),
                len(self.services_catalog),
            )
            for cb in self._connect_listeners:
                try:
                    await cb()
                except Exception:
                    logger.exception("connect listener failed")
            self._attempt_done.set()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.error("HA post-connect setup failed: {}", exc)
            await ws.close()  # reader loop exits → _run marks unreachable + retries

    async def _refresh_registries(self, ws: ClientConnection) -> None:
        self.entity_registry = await self._cmd(ws, {"type": "config/entity_registry/list"}) or []
        self.device_registry = await self._cmd(ws, {"type": "config/device_registry/list"}) or []
        self.area_registry = await self._cmd(ws, {"type": "config/area_registry/list"}) or []

    # ── command correlation ──

    async def _cmd(self, ws: ClientConnection, payload: dict[str, Any]) -> Any:
        msg_id = self._next_msg_id
        self._next_msg_id += 1
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[msg_id] = fut
        try:
            await ws.send(json.dumps({"id": msg_id, **payload}))
            return await asyncio.wait_for(fut, timeout=COMMAND_TIMEOUT)
        finally:
            self._pending.pop(msg_id, None)

    def _fail_pending(self) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(HACommandError("connection_lost", "WebSocket closed"))
        self._pending.clear()

    # ── inbound messages ──

    async def _handle_message(self, msg: dict[str, Any]) -> None:
        match msg.get("type"):
            case "result":
                fut = self._pending.get(int(msg["id"]))
                if fut is not None and not fut.done():
                    if msg.get("success"):
                        fut.set_result(msg.get("result"))
                    else:
                        err = msg.get("error") or {}
                        fut.set_exception(
                            HACommandError(
                                str(err.get("code", "unknown")), str(err.get("message", ""))
                            )
                        )
            case "event":
                event = msg.get("event") or {}
                event_type = str(event.get("event_type", ""))
                if event_type == "state_changed":
                    await self._handle_state_changed(event.get("data") or {})
                elif event_type.endswith("_registry_updated"):
                    self._schedule_registry_refresh()
            case _:
                pass

    async def _handle_state_changed(self, data: dict[str, Any]) -> None:
        entity_id = str(data.get("entity_id", ""))
        if not entity_id:
            return
        new = data.get("new_state")
        old = data.get("old_state")
        new_state = str(new["state"]) if new else None
        old_state = str(old["state"]) if old else None
        attributes: dict[str, Any] = dict(new.get("attributes") or {}) if new else {}
        if new is None:
            self.states.pop(entity_id, None)
        else:
            self.states[entity_id] = HAEntityState(
                entity_id=entity_id, state=new_state or "unknown", attributes=attributes
            )
        self._last_event_monotonic = time.monotonic()
        for cb in self._state_listeners:
            try:
                await cb(entity_id, old_state, new_state, attributes)
            except Exception:
                logger.exception("state listener failed for {}", entity_id)

    # ── registry refresh (coalesced task — never run inside the reader loop) ──

    def _schedule_registry_refresh(self) -> None:
        self._registry_dirty = True
        if self._registry_refresh_task is not None and not self._registry_refresh_task.done():
            return
        self._registry_refresh_task = asyncio.create_task(
            self._registry_refresh_loop(), name="ha-registry-refresh"
        )

    async def _registry_refresh_loop(self) -> None:
        while self._registry_dirty:
            self._registry_dirty = False
            ws = self._ws
            if ws is None:
                return  # reconnect path refreshes registries anyway
            try:
                await self._refresh_registries(ws)
            except Exception as exc:
                logger.warning("Registry refresh failed: {}", exc)
                return
            for cb in self._registry_listeners:
                try:
                    await cb()
                except Exception:
                    logger.exception("registry listener failed")

    # ── service calls ──

    async def call_service(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any] | None = None,
        entity_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        ws = self._ws
        if self.conn_state != "connected" or ws is None:
            raise HACommandError(
                "not_connected", f"cannot call {domain}.{service}: HA state is {self.conn_state}"
            )
        payload: dict[str, Any] = {"type": "call_service", "domain": domain, "service": service}
        if service_data:
            payload["service_data"] = service_data
        if entity_ids:
            payload["target"] = {"entity_id": entity_ids}
        result = await self._cmd(ws, payload)
        return result if isinstance(result, dict) else {}
