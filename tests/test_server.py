"""Integration tests for the rewritten server: /credentials, /health, /mcp, wiring."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock, Mock, create_autospec

import pytest
from alfred_sdk.context import ContextEntry, ContextSnapshot
from alfred_sdk.live_state import LiveStateWriter
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from loguru import logger

from app.live_state import build_snapshot
from app.server import CredentialsBody, Registrar, apply_env_credentials, create_app
from tests.fake_ha import FakeHAServer, eventually

_BOUND = 3.0  # seconds; bounds every wait, so a regression fails instead of hanging
_WARNING = 30  # loguru's WARNING level number


@pytest.fixture
def logs() -> Iterator[list[Any]]:
    """The loguru records emitted while the test runs."""
    records: list[Any] = []
    handler = logger.add(lambda message: records.append(message.record), level="DEBUG")
    yield records
    logger.remove(handler)


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    application = create_app()
    # keep Redis out of tests — registration and live state are best-effort by design
    application.state.client.register = AsyncMock()
    application.state.client.unregister = AsyncMock()
    live = application.state.live_state
    # Autospecced from the real writer: mypy skips alfred_sdk, so this is what fails a
    # call that no longer matches the SDK's signature after a pin moves.
    writer = create_autospec(LiveStateWriter, instance=True)
    for method in ("replace", "update", "remove", "clear", "aclose"):
        setattr(live, method, getattr(writer, method))
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


async def test_a_state_change_right_behind_the_get_states_reply_survives_the_connect(
    app: FastAPI, fake_ha: FakeHAServer
) -> None:
    """HA can send an event straight behind its get_states reply, so both frames arrive
    together and the reader handles the event before the connect setup resumes. Applied to
    the previous states, it would then be overwritten by the older reply, and the connect
    replace would publish the lamp as it was until it next changed."""
    fake_ha.state_changes_after_get_states = [("light.bedroom_lamp", "on", "off")]

    assert await app.state.ha.apply_credentials(fake_ha.url, fake_ha.token) == "connected"

    live = app.state.live_state
    live.update.assert_awaited_once()  # the event was handled, so the checks below bite
    assert app.state.ha.states["light.bedroom_lamp"].state == "off"
    assert _states(live.replace.await_args.args[0])["light.bedroom_lamp"] == "off"


@pytest.mark.parametrize("failing", ["index", "capabilities"])
async def test_connect_publishes_even_when_the_index_or_tool_surface_fails(
    app: FastAPI, fake_ha: FakeHAServer, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    """A credential swap stops the old connection without a disconnect clear, so the new
    connection's replace is all that retires the old instance's entries. It reads only
    HA's states and service catalog, so a failure building the index or the tool surface
    must not cost it."""
    owner, name = {
        "index": (app.state.index, "rebuild"),
        "capabilities": (app.state.client, "discover_features_from_classes"),
    }[failing]
    monkeypatch.setattr(owner, name, Mock(side_effect=RuntimeError("boom")))

    assert await app.state.ha.apply_credentials(fake_ha.url, fake_ha.token) == "connected"

    live = app.state.live_state
    live.replace.assert_awaited_once()
    assert _states(live.replace.await_args.args[0])["light.bedroom_lamp"] == "on"


async def test_the_index_reflects_a_reconnect_while_its_replace_waits_on_redis(
    connected_app: FastAPI, fake_ha: FakeHAServer
) -> None:
    """/health and /mcp read the index, and a hung Redis can hold the connect replace for
    the writer's whole 5 s timeout. Meanwhile they must see the new connection's entities,
    not the previous connection's."""
    index, live = connected_app.state.index, connected_app.state.live_state
    before = index.entity_count()
    fake_ha.states.append({"entity_id": "light.new_lamp", "state": "off", "attributes": {}})
    assert index.get("light.new_lamp") is None
    sent, reply = asyncio.Event(), asyncio.Event()

    async def hung_replace(snapshot: ContextSnapshot) -> None:
        sent.set()
        await reply.wait()

    live.replace.side_effect = hung_replace
    try:
        await fake_ha.drop_connections()
        await asyncio.wait_for(sent.wait(), _BOUND)  # the reconnect's replace waits on Redis
        assert _states(live.replace.await_args.args[0])["light.new_lamp"] == "off"

        assert index.get("light.new_lamp") is not None
        async with _client(connected_app) as http:
            health = (await http.get("/health")).json()["ha"]
        assert (health["state"], health["entities"]) == ("connected", before + 1)
    finally:
        reply.set()


async def test_nothing_yields_from_connected_to_the_connect_snapshot_handover(
    app: FastAPI, fake_ha: FakeHAServer
) -> None:
    """on_connect rebuilds the index in a try whose finally publishes. A yield in there would
    let a drop's cancel land inside the try, and the finally would then publish while the
    setup unwinds. A callback queued as HA turns "connected" must not have run by the
    handover. HAConnection logs "Connected to HA" straight after setting the flag."""
    loop = asyncio.get_running_loop()
    yielded = asyncio.Event()
    handler = logger.add(
        lambda _: loop.call_soon(yielded.set),
        filter=lambda record: record["message"].startswith("Connected to HA"),
    )
    at_handover: list[bool] = []
    app.state.live_state.replace.side_effect = lambda _: at_handover.append(yielded.is_set())
    try:
        assert await app.state.ha.apply_credentials(fake_ha.url, fake_ha.token) == "connected"
    finally:
        logger.remove(handler)
    assert at_handover == [False]
    assert yielded.is_set()  # the hook fired, so the check above was not vacuous


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
            attributes={"friendly_name": "Bedroom Lamp", "area": "Bedroom"},
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
    connected_app: FastAPI, fake_ha: FakeHAServer, logs: list[Any]
) -> None:
    live = connected_app.state.live_state
    live.update.side_effect = ConnectionError("redis down")
    live.replace.side_effect = ConnectionError("redis down")
    await fake_ha.push_state_changed("light.bedroom_lamp", "on", "off")
    await fake_ha.push_state_changed("light.bedroom_lamp", "off", "on")
    # Both events still forwarded, both still offered to the writer: a failed write
    # costs that entity's freshness, never the listener chain.
    await eventually(lambda: connected_app.state.forwarder.pending_count() == 2)
    await eventually(lambda: live.update.await_count == 2)
    assert live.replace.await_count == 1  # only the connect one: no write has landed since
    assert connected_app.state.ha.states["light.bedroom_lamp"].state == "on"
    # The live-state listener handles its own failures. HAConnection's isolation would
    # also keep the chain going, but it logs a traceback per event: it must never see one.
    assert [r for r in logs if "state listener failed" in r["message"]] == []


async def test_disconnect_clears_live_state_and_reconnect_republishes(
    connected_app: FastAPI, fake_ha: FakeHAServer
) -> None:
    live = connected_app.state.live_state
    await fake_ha.drop_connections()
    await eventually(lambda: live.clear.await_count >= 1)
    await eventually(lambda: live.replace.await_count == 2, timeout=3.0)


def _states(snapshot: ContextSnapshot) -> dict[str, str]:
    return {
        entry.entity_id: entry.state
        for groups in (snapshot.controllable, snapshot.sensors)
        for entries in groups.values()
        for entry in entries
    }


def _live_state_logs(records: list[Any]) -> list[str]:
    return [r["level"].name for r in records if r["name"] == "app.live_state"]


def _record_writes(live: Any, *names: str) -> list[str]:
    """Record the order the named writer methods are awaited in; each one lands."""
    order: list[str] = []
    for name in names:
        getattr(live, name).side_effect = lambda *_, name=name: order.append(name)
    return order


@pytest.mark.parametrize("failed", ["update", "remove"])
async def test_a_failed_write_heals_with_a_full_replace_then_updates_resume(
    connected_app: FastAPI, fake_ha: FakeHAServer, logs: list[Any], failed: str
) -> None:
    """A write lost to a Redis outage (every alfred deploy restarts Redis) leaves that
    entity wrong until it next changes. So the next event, whatever entity it is for,
    makes its own write and, once that lands (Redis is back), republishes everything;
    after that, events go back to one entity each."""
    live = connected_app.state.live_state
    publisher = connected_app.state.live_state_publisher
    getattr(live, failed).side_effect = ConnectionError("redis down")
    lamp = "off" if failed == "update" else None  # None: HA deleted the entity
    await fake_ha.push_state_changed("light.bedroom_lamp", "on", lamp)
    await eventually(lambda: getattr(live, failed).await_count == 1)
    assert publisher.dirty
    order = _record_writes(live, "update", "remove", "replace")  # Redis is back

    await fake_ha.push_state_changed("sensor.outdoor_temp", "20", "21")
    await eventually(lambda: live.replace.await_count == 2)  # the connect one, then the heal
    assert order == ["update", "replace"]  # the event's own write landed first
    assert live.update.await_args.args[2].state == "21"
    healed = _states(live.replace.await_args.args[0])
    assert healed.get("light.bedroom_lamp") == lamp
    assert healed["sensor.outdoor_temp"] == "21"
    assert not publisher.dirty

    updates = live.update.await_count
    await fake_ha.push_state_changed("sensor.outdoor_temp", "21", "22")
    await eventually(lambda: live.update.await_count == updates + 1)
    assert live.replace.await_count == 2
    assert _live_state_logs(logs) == ["WARNING", "INFO"]  # once going dirty, once healed


async def test_a_failed_startup_clear_and_connect_replace_heal_on_the_next_event(
    app: FastAPI, fake_ha: FakeHAServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Redis down at startup: the previous run's hash survives the failed clear and the
    failed connect replace, underneath any update, until a replace lands."""
    monkeypatch.delenv("HA_HOST", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    app.state.forwarder.start = AsyncMock()
    app.state.forwarder.stop = AsyncMock()
    live = app.state.live_state
    publisher = app.state.live_state_publisher
    live.clear.side_effect = ConnectionError("redis down")
    live.replace.side_effect = [ConnectionError("redis down"), None]
    # Registration needs Redis too; one that landed would heal before any event could.
    app.state.client.register.side_effect = ConnectionError("redis down")

    async with app.router.lifespan_context(app):
        assert publisher.dirty  # the failed startup clear alone marks it
        assert await app.state.ha.apply_credentials(fake_ha.url, fake_ha.token) == "connected"
        assert (live.clear.await_count, live.replace.await_count) == (1, 1)
        assert publisher.dirty

        order = _record_writes(live, "update", "replace")
        await fake_ha.push_state_changed("light.bedroom_lamp", "on", "off")
        await eventually(lambda: live.replace.await_count == 2)
        assert order == ["update", "replace"]  # the lamp's own write proved Redis back
        assert live.update.await_args.args[2].state == "off"
        assert _states(live.replace.await_args.args[0])["light.bedroom_lamp"] == "off"
        assert not publisher.dirty


async def test_a_failed_connect_replace_heals_when_the_connect_registration_lands(
    app: FastAPI, fake_ha: FakeHAServer, logs: list[Any]
) -> None:
    """A connect replace lost to Redis leaves whatever the hash held before. The
    registration the connect makes next proves Redis back, so it republishes."""
    live = app.state.live_state
    live.replace.side_effect = [ConnectionError("redis down"), None]

    assert await app.state.ha.apply_credentials(fake_ha.url, fake_ha.token) == "connected"

    assert live.replace.await_count == 2  # the failed connect one, then the heal
    assert _states(live.replace.await_args.args[0])["light.bedroom_lamp"] == "on"
    assert not app.state.live_state_publisher.dirty
    assert _live_state_logs(logs) == ["WARNING", "INFO"]


async def test_a_failed_disconnect_clear_stays_dirty_until_the_reconnect_replace(
    connected_app: FastAPI, fake_ha: FakeHAServer, logs: list[Any]
) -> None:
    """A clear that lands would heal it too; here every clear fails, so only the
    reconnect's replace can."""
    live = connected_app.state.live_state
    publisher = connected_app.state.live_state_publisher
    live.clear.side_effect = ConnectionError("redis down")

    await fake_ha.drop_connections()
    await eventually(lambda: live.clear.await_count >= 1)
    assert publisher.dirty

    await eventually(lambda: live.replace.await_count == 2, timeout=_BOUND)  # reconnected
    assert not publisher.dirty
    assert _live_state_logs(logs) == ["WARNING", "INFO"]


async def test_while_redis_stays_down_each_event_tries_only_its_own_write_quietly(
    connected_app: FastAPI,
    fake_ha: FakeHAServer,
    logs: list[Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """While dirty, an event's own write is the probe: one that fails builds no snapshot
    of the whole house, so an outage costs one small write per event."""
    snapshots = 0

    def counting_build_snapshot(*args: Any) -> ContextSnapshot:
        nonlocal snapshots
        snapshots += 1
        return build_snapshot(*args)

    monkeypatch.setattr("app.live_state.build_snapshot", counting_build_snapshot)
    live = connected_app.state.live_state
    live.update.side_effect = ConnectionError("redis down")
    live.replace.side_effect = ConnectionError("redis down")
    for i in range(3):
        await fake_ha.push_state_changed("sensor.outdoor_temp", str(20 + i), str(21 + i))
    await eventually(lambda: live.update.await_count == 3)

    assert live.replace.await_count == 1  # only the connect one
    assert snapshots == 0
    assert connected_app.state.live_state_publisher.dirty
    # One warning for the outage, from the publisher; nothing per event, from anyone.
    assert len([r for r in logs if r["level"].no >= _WARNING]) == 1
    assert _live_state_logs(logs) == ["WARNING"]


def _fail_once(write: AsyncMock) -> None:
    """The next call fails (Redis drops it), then Redis is back for everything after."""

    def fail_once(*_: object) -> None:
        write.side_effect = None
        raise ConnectionError("redis down")

    write.side_effect = fail_once


def _make_dirty(live: Any) -> None:
    _fail_once(live.update)


async def test_a_heal_waits_for_the_fresh_states_after_a_reconnect(
    connected_app: FastAPI, fake_ha: FakeHAServer
) -> None:
    """HAConnection keeps the previous connection's states until get_states answers, and
    handles state events from the moment it has subscribed. A heal in that window would
    republish the old connection's states and call the hash whole."""
    live = connected_app.state.live_state
    publisher = connected_app.state.live_state_publisher
    replaces = live.replace.await_count

    # HA restarts with the lamp now off, and holds its get_states answer on the reconnect.
    fake_ha.states = [
        {**s, "state": "off"} if s["entity_id"] == "light.bedroom_lamp" else s
        for s in fake_ha.states
    ]
    fake_ha.get_states_requested.clear()
    gate = fake_ha.get_states_gate = asyncio.Event()
    _fail_once(live.clear)  # the disconnect's clear is lost, so the window opens dirty
    try:
        await fake_ha.drop_connections()
        await asyncio.wait_for(fake_ha.get_states_requested.wait(), _BOUND)
        assert publisher.dirty
        writes = live.update.await_count + live.replace.await_count
        await fake_ha.push_state_changed("sensor.outdoor_temp", "20", "21")
        await eventually(lambda: live.update.await_count + live.replace.await_count > writes)
        assert publisher.dirty  # one entity's update makes nothing whole
        gate.set()
        await eventually(lambda: not publisher.dirty)
    finally:
        gate.set()

    lamps = [_states(c.args[0])["light.bedroom_lamp"] for c in live.replace.await_args_list]
    assert lamps[replaces:] == ["off"]  # only the reconnect's own, from the fresh states


async def test_a_registration_landing_while_dirty_republishes_from_a_live_connection(
    connected_app: FastAPI, fake_ha: FakeHAServer
) -> None:
    """A landed registration proves Redis is back, so it heals without waiting for HA."""
    live = connected_app.state.live_state
    publisher = connected_app.state.live_state_publisher
    _make_dirty(live)
    await fake_ha.push_state_changed("light.bedroom_lamp", "on", "off")
    await eventually(lambda: publisher.dirty)

    await connected_app.state.registrar.register()

    assert live.replace.await_count == 2  # the connect one, then the heal
    assert _states(live.replace.await_args.args[0])["light.bedroom_lamp"] == "off"
    assert not publisher.dirty


async def test_a_registration_landing_while_clean_writes_nothing(
    connected_app: FastAPI,
) -> None:
    live = connected_app.state.live_state
    writes = ("replace", "update", "remove", "clear")
    before = {name: getattr(live, name).await_count for name in writes}

    await connected_app.state.registrar.register()

    assert {name: getattr(live, name).await_count for name in writes} == before


async def _connect_with_no_reconnect_attempt_in_the_test(
    app: FastAPI, fake_ha: FakeHAServer
) -> None:
    """Connect with a backoff longer than any test, so once HA goes away no reconnect
    attempt's disconnect clear can land while the test counts writes. HAConnection reads
    the backoff as it connects, so it is set first."""
    app.state.ha._initial_backoff = 3600.0
    assert await app.state.ha.apply_credentials(fake_ha.url, fake_ha.token) == "connected"


async def test_a_registration_landing_after_a_drop_clears_rather_than_republishes(
    app: FastAPI, fake_ha: FakeHAServer
) -> None:
    """The registration path is not cancelled on disconnect: a heal from it after the
    drop must not bring back the gone connection's states."""
    await _connect_with_no_reconnect_attempt_in_the_test(app, fake_ha)
    live = app.state.live_state
    publisher = app.state.live_state_publisher
    _fail_once(live.clear)  # the disconnect's clear is lost
    await fake_ha.stop()  # HA goes away and stays away
    await eventually(lambda: live.clear.await_count == 1)
    assert publisher.dirty

    await app.state.registrar.register()

    assert live.replace.await_count == 1  # only the connect one
    assert live.clear.await_count == 2  # the disconnect's, then the heal's
    assert not publisher.dirty


async def test_a_disconnect_clear_that_lands_leaves_the_hash_clean(
    app: FastAPI, fake_ha: FakeHAServer, logs: list[Any]
) -> None:
    """An empty hash is right while HA is away, so a clear that lands heals as a replace
    does, and leaves a registration nothing to do."""
    await _connect_with_no_reconnect_attempt_in_the_test(app, fake_ha)
    live = app.state.live_state
    publisher = app.state.live_state_publisher
    _make_dirty(live)
    await fake_ha.push_state_changed("light.bedroom_lamp", "on", "off")
    await eventually(lambda: publisher.dirty)
    await fake_ha.stop()  # HA goes away and stays away
    await eventually(lambda: live.clear.await_count == 1)
    assert not publisher.dirty

    writes = ("replace", "update", "remove", "clear")
    before = {name: getattr(live, name).await_count for name in writes}
    await app.state.registrar.register()

    assert {name: getattr(live, name).await_count for name in writes} == before
    assert _live_state_logs(logs) == ["WARNING", "INFO"]


async def test_a_failed_heal_keeps_the_hash_dirty_without_another_warning(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch, logs: list[Any]
) -> None:
    monkeypatch.delenv("HA_HOST", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    app.state.forwarder.start = AsyncMock()
    app.state.forwarder.stop = AsyncMock()
    live = app.state.live_state
    publisher = app.state.live_state_publisher
    # The startup clear fails, and so does the heal after the startup registration lands.
    live.clear.side_effect = [ConnectionError("redis down"), ConnectionError("still"), None, None]

    async with app.router.lifespan_context(app):
        assert live.clear.await_count == 2
        assert publisher.dirty
        await app.state.registrar.register()
        assert live.clear.await_count == 3
        assert not publisher.dirty

    assert _live_state_logs(logs) == ["WARNING", "INFO"]


async def test_redis_down_at_startup_without_ha_heals_when_registration_lands(
    app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No HA, so no state event will ever come: the previous run's hash, which the failed
    startup clear left behind, is cleared once a registration retry lands."""
    monkeypatch.delenv("HA_HOST", raising=False)
    monkeypatch.delenv("HA_TOKEN", raising=False)
    app.state.forwarder.start = AsyncMock()
    app.state.forwarder.stop = AsyncMock()
    live = app.state.live_state
    publisher = app.state.live_state_publisher
    live.clear.side_effect = [ConnectionError("redis down"), None, None]
    app.state.client.register.side_effect = [ConnectionError("redis down"), None]

    async with app.router.lifespan_context(app):
        assert publisher.dirty
        await eventually(lambda: live.clear.await_count == 2, timeout=_BOUND)  # ~1 s backoff
        assert app.state.client.register.await_count == 2
        assert not publisher.dirty
        live.replace.assert_not_awaited()


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

    HAConnection cancels the connect setup and waits for it to end, then runs the
    disconnect listeners. The snapshot may land before the clear (the writer is FIFO),
    never after it: live state must end empty, not showing a connection that is gone.
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


def _areas(snapshot: ContextSnapshot) -> dict[str, Any]:
    return {
        entry.entity_id: entry.attributes.get("area")
        for groups in (snapshot.controllable, snapshot.sensors)
        for entries in groups.values()
        for entry in entries
    }


async def test_connect_publishes_each_entity_with_its_room(connected_app: FastAPI) -> None:
    areas = _areas(connected_app.state.live_state.replace.await_args.args[0])
    assert areas["light.bedroom_lamp"] == "Bedroom"
    assert areas["media_player.tv"] == "Living Room"  # from its device
    assert areas["scene.movie_night"] is None


def _move(fake_ha: FakeHAServer, registry: str, key: str, value: str, area_id: str) -> None:
    rows = fake_ha.entity_registry if registry == "entity" else fake_ha.device_registry
    row = next(r for r in rows if r[key] == value)
    row["area_id"] = area_id


@pytest.mark.parametrize(
    ("registry", "key", "value", "entity_id"),
    [
        ("entity", "entity_id", "light.bedroom_lamp", "light.bedroom_lamp"),
        ("device", "id", "dev-tv", "media_player.tv"),
    ],
)
async def test_a_room_move_republishes_on_the_next_state_event(
    connected_app: FastAPI,
    fake_ha: FakeHAServer,
    registry: str,
    key: str,
    value: str,
    entity_id: str,
) -> None:
    """The registry refresh never writes live state (see the test below), so a move only
    marks the hash stale; the next state event's write lands and the full replace follows,
    carrying the new room. No restart is needed."""
    live = connected_app.state.live_state
    registered = connected_app.state.client.register.await_count
    _move(fake_ha, registry, key, value, "garage")

    await fake_ha.push_registry_updated(registry, {"action": "update", key: value})
    await eventually(lambda: connected_app.state.client.register.await_count == registered + 1)
    assert live.replace.await_count == 1  # only the connect's: the refresh wrote nothing

    await fake_ha.push_state_changed("switch.coffee_maker", "off", "on")
    await eventually(lambda: live.replace.await_count == 2)
    assert _areas(live.replace.await_args.args[0])[entity_id] == "Garage"

    await fake_ha.push_state_changed("switch.coffee_maker", "on", "off")
    await eventually(lambda: live.update.await_count == 2)
    assert live.replace.await_count == 2  # healed once; updates resume


async def test_a_registry_change_that_moves_no_room_marks_nothing_stale(
    connected_app: FastAPI, fake_ha: FakeHAServer
) -> None:
    live = connected_app.state.live_state
    registered = connected_app.state.client.register.await_count
    await fake_ha.push_registry_updated("entity", {"action": "update", "entity_id": "x.y"})
    await eventually(lambda: connected_app.state.client.register.await_count == registered + 1)

    await fake_ha.push_state_changed("switch.coffee_maker", "off", "on")
    await eventually(lambda: live.update.await_count == 1)
    assert live.replace.await_count == 1


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
        attempt,  # ...whose attempt is in flight when stop() cancels it and waits...
        ConnectionError("down"),  # ...while another registration fails
    )
    registrar = Registrar(client, initial_backoff=0.01)
    try:
        await registrar.register()
        await asyncio.wait_for(entered.wait(), _BOUND)
        stopping = asyncio.create_task(registrar.stop())
        await asyncio.wait_for(cleaning.wait(), _BOUND)
        failing = asyncio.create_task(registrar.register())
        await asyncio.sleep(0.05)  # it gets as far as it can while the retry unwinds
        release.set()
        await asyncio.wait_for(asyncio.gather(stopping, failing), _BOUND)

        retry = registrar._retry
        assert retry is not None and not retry.done()
        await registrar.stop()
        assert retry.cancelled()
    finally:
        release.set()
        await registrar.stop()


async def _slow_hook() -> None:
    await asyncio.sleep(0.02)  # a heal writing to Redis


@pytest.mark.parametrize("on_registered", [None, _slow_hook], ids=["no-hook", "slow-hook"])
async def test_an_earlier_success_landing_last_keeps_a_later_failures_retry(
    on_registered: Callable[[], Awaitable[None]] | None,
) -> None:
    """Registrations run one at a time, in the order they were asked for.

    on_connect and a registry change can register at once. Unserialised, the later one,
    carrying the newer manifest, can fail first and schedule a retry, which the earlier
    one's success then cancels when it lands: the older manifest would stay registered.
    The same goes if the success's hook ran before it let go of the retry.
    """
    sent, reply = asyncio.Event(), asyncio.Event()

    async def slow_success() -> None:
        sent.set()
        await reply.wait()

    client: Any = AsyncMock()
    client.register.side_effect = _registrations(slow_success, ConnectionError("down"))
    registrar = Registrar(client, initial_backoff=10.0, on_registered=on_registered)
    try:
        earlier = asyncio.create_task(registrar.register())
        await asyncio.wait_for(sent.wait(), _BOUND)
        later = asyncio.create_task(registrar.register())
        await asyncio.sleep(0.05)  # the later one gets as far as it can
        reply.set()
        await asyncio.wait_for(asyncio.gather(earlier, later), _BOUND)

        assert client.register.await_count == 2
        retry = registrar._retry
        assert retry is not None and not retry.done()
    finally:
        reply.set()
        await registrar.stop()


async def test_the_registered_hook_runs_once_per_registration_that_lands() -> None:
    landed = 0

    async def on_registered() -> None:
        nonlocal landed
        landed += 1

    client: Any = AsyncMock()
    client.register.side_effect = [ConnectionError("down"), None, None]
    registrar = Registrar(client, initial_backoff=0.01, on_registered=on_registered)

    await registrar.register()  # fails: no hook
    assert landed == 0
    await eventually(lambda: landed == 1)  # the retry lands
    await registrar.register()  # lands directly
    assert landed == 2
    assert client.register.await_count == 3
    await registrar.stop()


async def test_a_registration_failing_during_the_retrys_hook_gets_a_retry_of_its_own() -> None:
    """A retry that lands lets go of itself before its hook runs. A registration failing
    while that heal writes would otherwise find the retry still pending and schedule
    nothing, and the retry would then end: its newer manifest never registered."""
    in_hook, release = asyncio.Event(), asyncio.Event()
    hooks = 0

    async def on_registered() -> None:
        nonlocal hooks
        hooks += 1
        if hooks == 1:
            in_hook.set()
            await release.wait()  # a heal writing to Redis

    client: Any = AsyncMock()
    client.register.side_effect = [ConnectionError("down"), None, ConnectionError("down"), None]
    registrar = Registrar(client, initial_backoff=0.01, on_registered=on_registered)
    try:
        await registrar.register()  # fails, schedules a retry
        await asyncio.wait_for(in_hook.wait(), _BOUND)  # the retry landed; its heal runs
        await registrar.register()  # fails meanwhile
        release.set()
        await eventually(lambda: hooks == 2)  # its own retry lands, and heals

        assert client.register.await_count == 4
        assert registrar._retry is None
    finally:
        release.set()
        await registrar.stop()


async def test_a_failing_registered_hook_does_not_fail_the_registration(
    logs: list[Any],
) -> None:
    """register() runs inside on_connect and the lifespan's startup: a hook must not
    break either."""

    async def on_registered() -> None:
        raise RuntimeError("boom")

    client: Any = AsyncMock()
    registrar = Registrar(client, on_registered=on_registered)

    await registrar.register()

    client.register.assert_awaited_once()
    assert registrar._retry is None
    assert [r["level"].name for r in logs if r["name"] == "app.server"] == ["ERROR"]
