"""Integration tests for the rewritten server: /credentials, /health, /mcp, wiring."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from unittest.mock import AsyncMock

import pytest
from alfred_sdk.context import ContextEntry, ContextSnapshot
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.server import CredentialsBody, Registrar, apply_env_credentials, create_app
from tests.fake_ha import FakeHAServer, eventually

_BOUND = 3.0  # seconds; bounds every wait, so a regression fails instead of hanging


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    application = create_app()
    # keep Redis out of tests — registration and live state are best-effort by design
    application.state.client.register = AsyncMock()
    application.state.client.unregister = AsyncMock()
    live = application.state.live_state
    for method in ("replace", "update", "remove", "clear", "aclose"):
        setattr(live, method, AsyncMock())
    yield application
    await application.state.registrar.stop()
    await application.state.ha.stop()


@pytest.fixture
async def connected_app(app: FastAPI, fake_ha: FakeHAServer) -> FastAPI:
    state = await app.state.ha.apply_credentials(fake_ha.url, fake_ha.token)
    assert state == "connected"
    return app


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_health_disconnected_before_credentials(app: FastAPI) -> None:
    async with _client(app) as http:
        resp = await http.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "service": "home-service",
        "ha": {"state": "disconnected", "entities": 0, "areas": 0, "last_event_age_s": None},
    }


async def test_credentials_endpoint_connects_and_returns_health(
    app: FastAPI, fake_ha: FakeHAServer
) -> None:
    async with _client(app) as http:
        resp = await http.post("/credentials", json={"url": fake_ha.url, "token": "test-token"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["health"]["ha"]["state"] == "connected"
    assert body["health"]["ha"]["entities"] == 11
    assert body["health"]["ha"]["areas"] == 3


async def test_credentials_bad_token_reports_auth_failed(
    app: FastAPI, fake_ha: FakeHAServer
) -> None:
    async with _client(app) as http:
        resp = await http.post("/credentials", json={"url": fake_ha.url, "token": "wrong"})
    assert resp.status_code == 200
    assert resp.json()["health"]["ha"]["state"] == "auth_failed"


async def test_credentials_unknown_field_422(app: FastAPI) -> None:
    async with _client(app) as http:
        resp = await http.post("/credentials", json={"url": "http://x", "token": "t", "bogus": 1})
    assert resp.status_code == 422


async def test_credentials_missing_token_422(app: FastAPI) -> None:
    async with _client(app) as http:
        resp = await http.post("/credentials", json={"url": "http://x"})
    assert resp.status_code == 422


def test_credentials_body_url_default() -> None:
    body = CredentialsBody(token="t")
    assert body.url == "http://homeassistant.local:8123"


async def test_capabilities_registered_after_connect(connected_app: FastAPI) -> None:
    assert connected_app.state.capabilities_ready is True
    connected_app.state.client.register.assert_awaited()


async def test_mcp_dispatches_generated_tool_end_to_end(
    connected_app: FastAPI, fake_ha: FakeHAServer
) -> None:
    async with _client(connected_app) as http:
        resp = await http.post(
            "/mcp",
            json={
                "method": "home.light_turn_on",
                "params": {"target": "Living Room", "brightness_pct": 50},
                "id": "req-001",
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "req-001"
    assert data["error"] is None
    assert data["result"]["entity_ids"] == ["light.living_room_lamp"]
    assert fake_ha.service_calls == [
        {
            "domain": "light",
            "service": "turn_on",
            "service_data": {"brightness_pct": 50},
            "target": {"entity_id": ["light.living_room_lamp"]},
        }
    ]


async def test_mcp_unknown_method_returns_error_in_band(connected_app: FastAPI) -> None:
    async with _client(connected_app) as http:
        resp = await http.post(
            "/mcp", json={"method": "nonexistent.tool", "params": {}, "id": "req-002"}
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "req-002"
    assert data["error"] is not None


async def test_mcp_unresolvable_target_returns_error_in_band(
    connected_app: FastAPI,
) -> None:
    async with _client(connected_app) as http:
        resp = await http.post(
            "/mcp",
            json={
                "method": "home.light_turn_on",
                "params": {"target": "attic"},
                "id": "req-003",
            },
        )
    data = resp.json()
    assert data["error"] is not None
    assert "attic" in data["error"]
    assert "Areas:" in data["error"]  # LLM can self-correct from the options list


async def test_state_event_feeds_forwarder_and_health(
    connected_app: FastAPI, fake_ha: FakeHAServer
) -> None:
    # forwarder not started (no lifespan in ASGITransport) → events accumulate
    await fake_ha.push_state_changed(
        "light.bedroom_lamp", "on", "off", {"friendly_name": "Bedroom Lamp"}
    )
    await eventually(lambda: connected_app.state.forwarder.pending_count() == 1)
    await eventually(lambda: connected_app.state.ha.states["light.bedroom_lamp"].state == "off")
    async with _client(connected_app) as http:
        resp = await http.get("/health")
    assert resp.json()["ha"]["last_event_age_s"] is not None


async def test_env_fallback_applies_credentials(
    app: FastAPI, fake_ha: FakeHAServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HA_HOST", fake_ha.url)
    monkeypatch.setenv("HA_TOKEN", "test-token")
    await apply_env_credentials(app.state.ha)
    assert app.state.ha.conn_state == "connected"


async def test_env_fallback_absent_stays_disconnected(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HA_HOST", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    await apply_env_credentials(app.state.ha)
    assert app.state.ha.conn_state == "disconnected"


def test_registration_manifest_carries_credentials_schema() -> None:
    from alfred_ext.register import build_client

    manifest = build_client().get_registration_manifest()
    assert manifest["service_name"] == "home-service"
    assert manifest["credentials_endpoint"].endswith(":8000/credentials")
    fields = manifest["credentials_schema"]["fields"]
    assert fields["url"]["field_type"] == "url"
    assert fields["url"]["default"] == "http://homeassistant.local:8123"
    assert fields["token"]["field_type"] == "password"
    assert fields["token"]["required"] is True


async def test_connect_publishes_live_state_then_registers(
    app: FastAPI, fake_ha: FakeHAServer
) -> None:
    order: list[str] = []
    app.state.live_state.replace.side_effect = lambda snapshot: order.append("replace")
    app.state.client.register.side_effect = lambda: order.append("register")

    assert await app.state.ha.apply_credentials(fake_ha.url, fake_ha.token) == "connected"

    assert order == ["replace", "register"]
    snapshot = app.state.live_state.replace.await_args.args[0]
    lamps = {e.entity_id: e for e in snapshot.controllable["light"]}
    assert lamps["light.bedroom_lamp"].state == "on"


async def test_a_state_change_updates_one_entity(
    connected_app: FastAPI, fake_ha: FakeHAServer
) -> None:
    await fake_ha.push_state_changed(
        "light.bedroom_lamp", "on", "off", {"friendly_name": "Bedroom Lamp", "junk": 1}
    )
    live = connected_app.state.live_state
    await eventually(lambda: live.update.await_count == 1)
    assert live.update.await_args.args == (
        "light",
        "controllable",
        ContextEntry(
            entity_id="light.bedroom_lamp",
            state="off",
            attributes={"friendly_name": "Bedroom Lamp"},
        ),
    )


async def test_an_entity_ha_deleted_is_removed(
    connected_app: FastAPI, fake_ha: FakeHAServer
) -> None:
    await fake_ha.push_state_changed("light.bedroom_lamp", "on", None)
    live = connected_app.state.live_state
    await eventually(lambda: live.remove.await_count == 1)
    live.remove.assert_awaited_once_with("light.bedroom_lamp")
    live.update.assert_not_awaited()


async def test_a_hundred_state_changes_register_nothing(
    connected_app: FastAPI, fake_ha: FakeHAServer
) -> None:
    """Regression (#281): state churn re-registered every ~5 s in production."""
    registered = connected_app.state.client.register.await_count
    for i in range(100):
        await fake_ha.push_state_changed("sensor.outdoor_temp", str(20 + i % 2), str(21 - i % 2))
    live = connected_app.state.live_state
    await eventually(lambda: live.update.await_count == 100, timeout=5.0)
    await asyncio.sleep(2.5)  # longer than the old 2 s debounce window
    assert connected_app.state.client.register.await_count == registered


async def test_a_failed_live_state_write_does_not_stop_forwarding(
    connected_app: FastAPI, fake_ha: FakeHAServer
) -> None:
    live = connected_app.state.live_state
    live.update.side_effect = ConnectionError("redis down")
    await fake_ha.push_state_changed("light.bedroom_lamp", "on", "off")
    await fake_ha.push_state_changed("light.bedroom_lamp", "off", "on")
    # Both events still forwarded, both still offered to the writer: a failed write
    # costs that entity's freshness, never the listener chain.
    await eventually(lambda: connected_app.state.forwarder.pending_count() == 2)
    await eventually(lambda: live.update.await_count == 2)
    assert connected_app.state.ha.states["light.bedroom_lamp"].state == "on"


async def test_disconnect_clears_live_state_and_reconnect_republishes(
    connected_app: FastAPI, fake_ha: FakeHAServer
) -> None:
    live = connected_app.state.live_state
    await fake_ha.drop_connections()
    await eventually(lambda: live.clear.await_count >= 1)
    await eventually(lambda: live.replace.await_count == 2, timeout=3.0)


class _FifoWriter:
    """LiveStateWriter's Redis side: its FIFO lock, the hash, and the order writes land in.

    replace() holds the lock until its reply arrives. A cancel while it waits still lands
    it, the worst case for a MULTI/EXEC already sent.
    """

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.hash: dict[str, str] = {}
        self.landed: list[str] = []
        self.replace_requested = asyncio.Event()
        self.replace_sent = asyncio.Event()
        self.reply = asyncio.Event()
        self.clear_requested = asyncio.Event()

    async def replace(self, snapshot: ContextSnapshot) -> None:
        fields = {
            entry.entity_id: entry.state
            for groups in (snapshot.controllable, snapshot.sensors)
            for entries in groups.values()
            for entry in entries
        }
        self.replace_requested.set()
        async with self.lock:
            self.replace_sent.set()
            try:
                await self.reply.wait()
            finally:
                self.hash = fields
                self.landed.append("replace")

    async def clear(self) -> None:
        self.clear_requested.set()
        async with self.lock:
            self.hash = {}
            self.landed.append("clear")


@pytest.mark.parametrize("snapshot", ["in_flight", "queued"])
async def test_a_drop_while_the_connect_snapshot_is_pending_ends_cleared(
    app: FastAPI, fake_ha: FakeHAServer, snapshot: str
) -> None:
    """HA drops while on_connect's replace() is mid-write, or queued behind another write.

    HAConnection cancels the connect setup without awaiting it, then runs the disconnect
    listeners. The snapshot may land before the clear (the writer is FIFO), never after
    it: live state must end empty, not showing a connection that is gone.
    """
    writer = _FifoWriter()
    app.state.live_state.replace = writer.replace
    app.state.live_state.clear = writer.clear
    held = snapshot == "queued"
    if held:
        await writer.lock.acquire()  # another write holds the writer
    try:
        connecting = asyncio.create_task(app.state.ha.apply_credentials(fake_ha.url, fake_ha.token))
        pending = writer.replace_sent if snapshot == "in_flight" else writer.replace_requested
        await asyncio.wait_for(pending.wait(), _BOUND)

        await fake_ha.stop()  # HA goes away and stays away, so nothing reconnects
        await asyncio.wait_for(writer.clear_requested.wait(), _BOUND)
        writer.reply.set()  # the snapshot's reply arrives...
        if held:
            held = False
            writer.lock.release()  # ...or the write ahead of it finishes

        assert await asyncio.wait_for(connecting, _BOUND) == "unreachable"
        first_clear = writer.landed.index("clear")
        assert "replace" not in writer.landed[first_clear:]
        assert writer.hash == {}
    finally:
        writer.reply.set()
        if held:
            writer.lock.release()


async def test_a_registry_change_registers_without_republishing(
    connected_app: FastAPI, fake_ha: FakeHAServer
) -> None:
    registered = connected_app.state.client.register.await_count
    await fake_ha.push_registry_updated("area", {"action": "create", "area_id": "office"})
    await eventually(lambda: connected_app.state.client.register.await_count == registered + 1)
    assert connected_app.state.live_state.replace.await_count == 1


async def test_a_registry_change_never_writes_live_state(
    connected_app: FastAPI, fake_ha: FakeHAServer
) -> None:
    """The registry refresh is not cancelled on disconnect, so a live-state write from it
    could land after the disconnect's clear and bring back state for a connection that
    is gone."""
    live = connected_app.state.live_state
    writes = ("replace", "update", "remove", "clear")
    before = {name: getattr(live, name).await_count for name in writes}
    registered = connected_app.state.client.register.await_count

    await fake_ha.push_registry_updated("entity", {"action": "update", "entity_id": "x.y"})
    await eventually(lambda: connected_app.state.client.register.await_count == registered + 1)

    assert {name: getattr(live, name).await_count for name in writes} == before


def _record(calls: list[str], *targets: tuple[Any, str]) -> None:
    """Make each (owner, AsyncMock attribute) append its name to calls when awaited."""
    for owner, name in targets:
        getattr(owner, name).side_effect = lambda name=name: calls.append(name)


def _record_then_run(calls: list[str], label: str, method: Callable[[], Awaitable[None]]) -> Any:
    async def recorded() -> None:
        calls.append(label)
        await method()

    return recorded


async def test_lifespan_clears_around_registration(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HA_HOST", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    app.state.forwarder.start = AsyncMock()
    app.state.forwarder.stop = AsyncMock()
    calls: list[str] = []
    _record(
        calls,
        (app.state.live_state, "clear"),
        (app.state.live_state, "aclose"),
        (app.state.client, "register"),
        (app.state.client, "unregister"),
    )
    async with app.router.lifespan_context(app):
        assert calls == ["clear", "register"]
        # Recorded from here on: a registration that lands also stops a pending retry.
        # HA stops before the final clear, so no state event can write after it, and
        # before the registrar, so no HA listener can schedule a retry after that.
        for owner, label in ((app.state.ha, "ha.stop"), (app.state.registrar, "registrar.stop")):
            monkeypatch.setattr(owner, "stop", _record_then_run(calls, label, owner.stop))

    assert calls == [
        "clear",
        "register",
        "ha.stop",
        "registrar.stop",
        "clear",
        "unregister",
        "aclose",
    ]


def _slow_to_cancel() -> tuple[
    Callable[[], Awaitable[None]], asyncio.Event, asyncio.Event, asyncio.Event
]:
    """An attempt whose cleanup outlasts a cancel; return it with its events.

    It sets `entered` and holds open. Once cancelled it sets `cleaning` and keeps awaiting
    `release`, so whoever cancelled it is left awaiting it, until a second cancel ends
    that cleanup. Tests set `release` in a finally.
    """
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def attempt() -> None:
        entered.set()
        try:
            await release.wait()
        finally:
            cleaning.set()
            await release.wait()

    return attempt, entered, cleaning, release


async def _forever() -> None:
    await asyncio.Event().wait()


def _registrations(
    *outcomes: BaseException | Callable[[], Awaitable[None]] | None,
) -> Callable[[], Awaitable[None]]:
    """A client.register side effect: each call raises, awaits or (None) returns the next
    outcome; once they run out, calls hang until cancelled."""
    queue = list(outcomes)

    async def register() -> None:
        outcome = queue.pop(0) if queue else _forever
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is not None:
            await outcome()

    return register


async def test_a_cancelled_shutdown_finishes_then_passes_the_cancellation_on(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Shutdown swallows the env-credentials task's own cancellation, never the lifespan's.

    A cancellation aimed at the lifespan while it waits for that task is held until the
    rest of shutdown has run, so live state is still cleared and the service unregistered.
    """
    app.state.forwarder.start = AsyncMock()
    app.state.forwarder.stop = AsyncMock()
    calls: list[str] = []
    _record(
        calls,
        (app.state.live_state, "clear"),
        (app.state.live_state, "aclose"),
        (app.state.client, "register"),
        (app.state.client, "unregister"),
    )
    attempt, entered, cleaning, release = _slow_to_cancel()
    monkeypatch.setattr("app.server.apply_env_credentials", lambda conn: attempt())
    leave = asyncio.Event()

    async def serve() -> None:
        async with app.router.lifespan_context(app):
            await leave.wait()

    try:
        lifespan = asyncio.create_task(serve())
        await asyncio.wait_for(entered.wait(), _BOUND)
        leave.set()
        await asyncio.wait_for(cleaning.wait(), _BOUND)  # shutdown cancelled it and waits
        lifespan.cancel()
        await asyncio.wait([lifespan], timeout=_BOUND)

        assert lifespan.cancelled()
        assert calls == ["clear", "register", "clear", "unregister", "aclose"]
    finally:
        release.set()


async def test_a_failed_registration_retries_until_it_lands() -> None:
    client: Any = AsyncMock()
    client.register.side_effect = [ConnectionError("down"), ConnectionError("down"), None]
    registrar = Registrar(client, initial_backoff=0.01, max_backoff=0.02)

    await registrar.register()
    await eventually(lambda: client.register.await_count == 3)
    await asyncio.sleep(0.1)

    assert client.register.await_count == 3  # nothing scheduled after a success
    await registrar.stop()


async def test_a_successful_registration_cancels_the_pending_retry() -> None:
    client: Any = AsyncMock()
    client.register.side_effect = [ConnectionError("down"), None]
    registrar = Registrar(client, initial_backoff=10.0)

    await registrar.register()  # fails, schedules a retry in 10 s
    await registrar.register()  # succeeds now
    await asyncio.sleep(0)

    assert client.register.await_count == 2
    assert registrar._retry is None


async def test_registrar_stop_passes_on_a_cancellation_aimed_at_its_caller() -> None:
    """stop() swallows its retry's cancellation, never its own caller's."""
    attempt, entered, cleaning, release = _slow_to_cancel()
    client: Any = AsyncMock()
    client.register.side_effect = _registrations(ConnectionError("down"), attempt)
    registrar = Registrar(client, initial_backoff=0.01)
    try:
        await registrar.register()  # fails, schedules a retry
        await asyncio.wait_for(entered.wait(), _BOUND)  # the retry's attempt is in flight
        retry = registrar._retry

        stopper = asyncio.create_task(registrar.stop())
        await asyncio.wait_for(cleaning.wait(), _BOUND)  # stop() cancelled it and waits
        stopper.cancel()
        await asyncio.wait([stopper], timeout=_BOUND)

        assert stopper.cancelled()
        assert retry is not None and retry.cancelled()
    finally:
        release.set()


async def test_registrar_stop_ignores_a_cancellation_its_caller_already_handled() -> None:
    """cancelling() counts every cancel ever requested; only a rise during stop() is live."""
    client: Any = AsyncMock()
    client.register.side_effect = ConnectionError("down")
    registrar = Registrar(client, initial_backoff=10.0)
    await registrar.register()  # fails, schedules a retry in 10 s
    retry = registrar._retry
    waiting = asyncio.Event()

    async def handle_a_cancel_then_stop() -> int:
        try:
            waiting.set()
            await asyncio.sleep(_BOUND)
        except asyncio.CancelledError:
            pass  # handled, without uncancel(): the count stays at 1
        await registrar.stop()
        task = asyncio.current_task()
        assert task is not None
        return task.cancelling()

    caller = asyncio.create_task(handle_a_cancel_then_stop())
    await asyncio.wait_for(waiting.wait(), _BOUND)
    caller.cancel()
    await asyncio.wait([caller], timeout=_BOUND)

    assert not caller.cancelled()
    assert caller.result() == 1
    assert retry is not None and retry.cancelled()


async def test_a_registration_failing_while_stop_waits_keeps_its_retry() -> None:
    """stop() lets go of the retry before waiting for it to end.

    A registration that fails meanwhile schedules a retry of its own, which must stay
    tracked, so the next stop() cancels it, rather than be dropped or left running.
    """
    attempt, entered, cleaning, release = _slow_to_cancel()
    client: Any = AsyncMock()
    client.register.side_effect = _registrations(
        ConnectionError("down"),  # a registration fails and schedules a retry...
        attempt,  # ...which is in flight when...
        None,  # ...another lands, so stop() cancels the retry and waits for it...
        ConnectionError("down"),  # ...while a third fails
    )
    registrar = Registrar(client, initial_backoff=0.01)
    try:
        await registrar.register()
        await asyncio.wait_for(entered.wait(), _BOUND)
        landing = asyncio.create_task(registrar.register())
        await asyncio.wait_for(cleaning.wait(), _BOUND)
        await registrar.register()
        release.set()
        await asyncio.wait_for(landing, _BOUND)

        retry = registrar._retry
        assert retry is not None and not retry.done()
        await registrar.stop()
        assert retry.cancelled()
    finally:
        release.set()
        await registrar.stop()
