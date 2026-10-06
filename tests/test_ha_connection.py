"""Tests for HAConnection against the fake HA WebSocket server."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from app.ha_connection import HACommandError, HAConnection, VoidListener
from tests.fake_ha import FakeHAServer, eventually


@pytest.fixture
async def conn() -> AsyncIterator[HAConnection]:
    connection = HAConnection(initial_backoff=0.05, max_backoff=0.2)
    yield connection
    await connection.stop()


async def test_apply_credentials_connects_and_fetches(
    fake_ha: FakeHAServer, conn: HAConnection
) -> None:
    state = await conn.apply_credentials(fake_ha.url, fake_ha.token)
    assert state == "connected"
    assert conn.conn_state == "connected"
    assert conn.states["light.bedroom_lamp"].state == "on"
    assert conn.states["light.bedroom_lamp"].attributes["brightness"] == 128
    assert "light" in conn.services_catalog
    assert len(conn.area_registry) == 3
    assert len(conn.entity_registry) == 11
    assert {
        "state_changed",
        "entity_registry_updated",
        "device_registry_updated",
        "area_registry_updated",
    } <= set(fake_ha.subscriptions)


async def test_starts_disconnected_and_no_event_age() -> None:
    connection = HAConnection()
    assert connection.conn_state == "disconnected"
    assert connection.last_event_age_s() is None


async def test_bad_token_sets_auth_failed_and_stops_retrying(
    fake_ha: FakeHAServer, conn: HAConnection
) -> None:
    state = await conn.apply_credentials(fake_ha.url, "wrong-token")
    assert state == "auth_failed"
    await asyncio.sleep(0.3)  # several backoff periods — must NOT retry a bad token
    assert fake_ha.auth_attempts == 1


async def test_unreachable_host_sets_unreachable(conn: HAConnection) -> None:
    state = await conn.apply_credentials("http://127.0.0.1:1", "token")
    assert state == "unreachable"


async def test_call_service_round_trip(fake_ha: FakeHAServer, conn: HAConnection) -> None:
    await conn.apply_credentials(fake_ha.url, fake_ha.token)
    result = await conn.call_service(
        "light", "turn_on", {"brightness_pct": 40}, ["light.bedroom_lamp"]
    )
    assert "context" in result
    assert fake_ha.service_calls == [
        {
            "domain": "light",
            "service": "turn_on",
            "service_data": {"brightness_pct": 40},
            "target": {"entity_id": ["light.bedroom_lamp"]},
        }
    ]


async def test_call_service_error_raises(fake_ha: FakeHAServer, conn: HAConnection) -> None:
    await conn.apply_credentials(fake_ha.url, fake_ha.token)
    fake_ha.fail_service_calls = True
    with pytest.raises(HACommandError) as exc_info:
        await conn.call_service("light", "turn_on", None, ["light.bedroom_lamp"])
    assert exc_info.value.code == "service_validation_error"


async def test_call_service_while_disconnected_raises() -> None:
    connection = HAConnection()
    with pytest.raises(HACommandError) as exc_info:
        await connection.call_service("light", "turn_on")
    assert exc_info.value.code == "not_connected"


async def test_state_changed_updates_states_and_notifies(
    fake_ha: FakeHAServer, conn: HAConnection
) -> None:
    received: list[tuple[str, str | None, str | None, dict[str, Any]]] = []

    async def listener(
        entity_id: str, old: str | None, new: str | None, attrs: dict[str, Any]
    ) -> None:
        received.append((entity_id, old, new, attrs))

    conn.add_state_listener(listener)
    await conn.apply_credentials(fake_ha.url, fake_ha.token)
    await fake_ha.push_state_changed(
        "light.bedroom_lamp", "on", "off", {"friendly_name": "Bedroom Lamp"}
    )
    await eventually(lambda: conn.states["light.bedroom_lamp"].state == "off")
    assert received == [("light.bedroom_lamp", "on", "off", {"friendly_name": "Bedroom Lamp"})]
    assert conn.last_event_age_s() is not None


async def test_entity_removal_drops_state(fake_ha: FakeHAServer, conn: HAConnection) -> None:
    await conn.apply_credentials(fake_ha.url, fake_ha.token)
    await fake_ha.push_state_changed("light.bedroom_lamp", "on", None)
    await eventually(lambda: "light.bedroom_lamp" not in conn.states)


async def test_registry_update_refetches_and_notifies(
    fake_ha: FakeHAServer, conn: HAConnection
) -> None:
    notified = asyncio.Event()

    async def registry_listener() -> None:
        notified.set()

    conn.add_registry_listener(registry_listener)
    await conn.apply_credentials(fake_ha.url, fake_ha.token)
    fake_ha.area_registry = fake_ha.area_registry + [
        {
            "area_id": "office",
            "name": "Office",
            "aliases": [],
            "floor_id": None,
            "icon": None,
            "labels": [],
            "picture": None,
        }
    ]
    await fake_ha.push_registry_updated("area", {"action": "create", "area_id": "office"})
    await eventually(lambda: len(conn.area_registry) == 4)
    await eventually(notified.is_set)


async def test_reconnect_after_drop_resubscribes(fake_ha: FakeHAServer, conn: HAConnection) -> None:
    connects = 0

    async def on_connect() -> None:
        nonlocal connects
        connects += 1

    conn.add_connect_listener(on_connect)
    await conn.apply_credentials(fake_ha.url, fake_ha.token)
    assert connects == 1
    await fake_ha.drop_connections()  # clears fake_ha.subscriptions
    await eventually(lambda: connects == 2, timeout=3.0)
    assert conn.conn_state == "connected"
    assert "state_changed" in fake_ha.subscriptions  # re-subscribed after reconnect


async def test_apply_credentials_idempotent_no_reconnect(
    fake_ha: FakeHAServer, conn: HAConnection
) -> None:
    """Re-applying the SAME credentials while already connected must NOT reconnect.

    Regression test for the credential re-push reconnect loop: core's
    credential_push_worker re-POSTs the stored HA creds to /credentials on every
    ServiceRegistered event (every re-register, on each HA connect and on each HA
    registry change). If apply_credentials unconditionally tore down and reconnected,
    that re-push would trigger on_connect again, re-register again, get re-pushed
    again — forever. A steady auth_attempts count proves the loop is broken.
    """
    state = await conn.apply_credentials(fake_ha.url, fake_ha.token)
    assert state == "connected"
    attempts_before = fake_ha.auth_attempts
    assert attempts_before == 1

    state = await conn.apply_credentials(fake_ha.url, fake_ha.token)

    assert state == "connected"
    assert conn.conn_state == "connected"
    assert fake_ha.auth_attempts == attempts_before  # no new handshake — no reconnect
    # entity state fetched at the original connect is still intact (proves we
    # didn't tear down and refetch)
    assert conn.states["light.bedroom_lamp"].state == "on"


async def test_apply_credentials_switches_servers(
    fake_ha: FakeHAServer, conn: HAConnection
) -> None:
    other = FakeHAServer(
        token="other-token",
        states=[
            {"entity_id": "light.other", "state": "on", "attributes": {"friendly_name": "Other"}}
        ],
    )
    await other.start()
    try:
        await conn.apply_credentials(fake_ha.url, fake_ha.token)
        assert "light.bedroom_lamp" in conn.states
        assert fake_ha.auth_attempts == 1
        state = await conn.apply_credentials(other.url, "other-token")
        assert state == "connected"
        assert "light.other" in conn.states
        assert "light.bedroom_lamp" not in conn.states
        # a genuinely different url DOES reconnect — the idempotency guard must
        # not suppress real credential changes
        assert other.auth_attempts == 1
    finally:
        await other.stop()


def _counting(conn: HAConnection) -> list[int]:
    drops = [0]

    async def on_disconnect() -> None:
        # Suspend before counting, as a real listener (a Redis write) does. A listener
        # that never yields would pass even if apply_credentials returned before it ran.
        await asyncio.sleep(0)
        drops[0] += 1

    conn.add_disconnect_listener(on_disconnect)
    return drops


async def test_disconnect_listener_quiet_while_connected(
    fake_ha: FakeHAServer, conn: HAConnection
) -> None:
    drops = _counting(conn)
    await conn.apply_credentials(fake_ha.url, fake_ha.token)
    assert drops[0] == 0


async def test_disconnect_listener_fires_when_the_connection_drops(
    fake_ha: FakeHAServer, conn: HAConnection
) -> None:
    drops = _counting(conn)
    await conn.apply_credentials(fake_ha.url, fake_ha.token)
    await fake_ha.drop_connections()
    await eventually(lambda: drops[0] == 1)


async def test_disconnect_listener_fires_when_the_token_is_rejected(
    fake_ha: FakeHAServer, conn: HAConnection
) -> None:
    drops = _counting(conn)
    assert await conn.apply_credentials(fake_ha.url, "wrong-token") == "auth_failed"
    assert drops[0] == 1


async def test_disconnect_listener_fires_when_ha_is_unreachable(conn: HAConnection) -> None:
    drops = _counting(conn)
    assert await conn.apply_credentials("http://127.0.0.1:1", "token") == "unreachable"
    assert drops[0] >= 1


async def test_a_failing_disconnect_listener_does_not_stop_reconnecting(
    fake_ha: FakeHAServer, conn: HAConnection
) -> None:
    async def broken() -> None:
        raise RuntimeError("boom")

    connects = [0]

    async def on_connect() -> None:
        connects[0] += 1

    conn.add_disconnect_listener(broken)
    conn.add_connect_listener(on_connect)
    await conn.apply_credentials(fake_ha.url, fake_ha.token)
    await fake_ha.drop_connections()
    await eventually(lambda: connects[0] == 2, timeout=3.0)


async def test_a_failing_disconnect_listener_does_not_skip_the_next(
    fake_ha: FakeHAServer, conn: HAConnection
) -> None:
    async def broken() -> None:
        raise RuntimeError("boom")

    conn.add_disconnect_listener(broken)
    drops = _counting(conn)
    assert await conn.apply_credentials(fake_ha.url, "wrong-token") == "auth_failed"
    assert drops[0] == 1


async def test_disconnect_listener_quiet_when_stopped(
    fake_ha: FakeHAServer, conn: HAConnection
) -> None:
    drops = _counting(conn)
    await conn.apply_credentials(fake_ha.url, fake_ha.token)
    await conn.stop()
    assert drops[0] == 0


async def test_disconnect_listener_quiet_when_switching_servers(
    fake_ha: FakeHAServer, conn: HAConnection
) -> None:
    other = FakeHAServer(token="other-token")
    await other.start()
    try:
        drops = _counting(conn)
        await conn.apply_credentials(fake_ha.url, fake_ha.token)
        assert await conn.apply_credentials(other.url, "other-token") == "connected"
        assert drops[0] == 0
    finally:
        await other.stop()


# Bound on every wait in the overlapping-attempt tests, so a regression fails, not hangs.
_BOUND = 3.0


def _stalling(add: Callable[[VoidListener], None]) -> tuple[asyncio.Event, asyncio.Event]:
    """Register, through `add`, a listener that holds an attempt open until `release` is set.

    Tests set `release` in a finally: if a regression re-enters the listener when the
    fixture's stop() cancels the connection, it then returns at once instead of
    blocking that stop() forever.
    """
    entered, release = asyncio.Event(), asyncio.Event()

    async def stall() -> None:
        entered.set()
        await release.wait()

    add(stall)
    return entered, release


def _live_connection_tasks() -> list[asyncio.Task[Any]]:
    return [t for t in asyncio.all_tasks() if t.get_name() == "ha-connection" and not t.done()]


async def test_a_superseded_apply_credentials_still_returns(
    fake_ha: FakeHAServer, conn: HAConnection
) -> None:
    """A second apply_credentials cancels the first attempt; the first caller must not hang."""
    entered, release = _stalling(conn.add_disconnect_listener)
    try:
        first = asyncio.create_task(conn.apply_credentials(fake_ha.url, "wrong-token"))
        await asyncio.wait_for(entered.wait(), _BOUND)

        second = await asyncio.wait_for(conn.apply_credentials(fake_ha.url, fake_ha.token), _BOUND)

        assert second == "connected"
        # The superseded caller's attempt never finished, so it must not report success.
        assert await asyncio.wait_for(first, _BOUND) == "disconnected"
    finally:
        release.set()


async def test_stop_releases_a_pending_apply_credentials(
    fake_ha: FakeHAServer, conn: HAConnection
) -> None:
    """A bare stop() (shutdown) cancels the attempt; the waiting caller must not hang."""
    entered, release = _stalling(conn.add_disconnect_listener)
    try:
        first = asyncio.create_task(conn.apply_credentials(fake_ha.url, "wrong-token"))
        await asyncio.wait_for(entered.wait(), _BOUND)

        await asyncio.wait_for(conn.stop(), _BOUND)

        assert await asyncio.wait_for(first, _BOUND) == "disconnected"
    finally:
        release.set()


async def test_stop_while_connect_listeners_run_reports_disconnected(
    fake_ha: FakeHAServer, conn: HAConnection
) -> None:
    """conn_state is already "connected" while connect listeners run; stop() there resets it."""
    entered, release = _stalling(conn.add_connect_listener)
    try:
        first = asyncio.create_task(conn.apply_credentials(fake_ha.url, fake_ha.token))
        await asyncio.wait_for(entered.wait(), _BOUND)
        assert conn.conn_state == "connected"

        await asyncio.wait_for(conn.stop(), _BOUND)

        assert conn.conn_state == "disconnected"
        assert await asyncio.wait_for(first, _BOUND) == "disconnected"
    finally:
        release.set()


async def test_reapplying_the_same_credentials_after_stop_reconnects(
    fake_ha: FakeHAServer, conn: HAConnection
) -> None:
    await asyncio.wait_for(conn.apply_credentials(fake_ha.url, fake_ha.token), _BOUND)
    await asyncio.wait_for(conn.stop(), _BOUND)

    state = await asyncio.wait_for(conn.apply_credentials(fake_ha.url, fake_ha.token), _BOUND)

    assert state == "connected"
    assert fake_ha.auth_attempts == 2  # a real reconnect, not the idempotency no-op
    assert len(_live_connection_tasks()) == 1


async def test_overlapping_apply_credentials_leave_one_connection(
    fake_ha: FakeHAServer, conn: HAConnection
) -> None:
    """B and C both supersede A while A's task is still being cancelled.

    Unserialised, both stop A, B starts its task, and C then overwrites B's task without
    stopping it: two connection loops run, and the fixture's stop() reaches only one.
    """
    entered, release = _stalling(conn.add_disconnect_listener)
    try:
        a = asyncio.create_task(conn.apply_credentials(fake_ha.url, "wrong-token"))
        await asyncio.wait_for(entered.wait(), _BOUND)

        b = asyncio.create_task(conn.apply_credentials(fake_ha.url, fake_ha.token))
        c = asyncio.create_task(conn.apply_credentials(fake_ha.url, fake_ha.token))
        state_a, _, state_c = await asyncio.wait_for(asyncio.gather(a, b, c), _BOUND)

        assert state_a != "connected"
        assert state_c == "connected"
        assert len(_live_connection_tasks()) == 1
    finally:
        release.set()


async def test_cancelling_a_stop_caller_propagates(
    fake_ha: FakeHAServer, conn: HAConnection
) -> None:
    """stop() swallows its connection task's cancellation, never its own caller's."""
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def slow_to_cancel() -> None:
        entered.set()
        try:
            await release.wait()
        finally:
            cleaning.set()
            await release.wait()  # cleanup that outlasts the cancel: stop() keeps awaiting

    conn.add_disconnect_listener(slow_to_cancel)
    try:
        first = asyncio.create_task(conn.apply_credentials(fake_ha.url, "wrong-token"))
        await asyncio.wait_for(entered.wait(), _BOUND)
        (inner,) = _live_connection_tasks()

        stopper = asyncio.create_task(conn.stop())
        await asyncio.wait_for(cleaning.wait(), _BOUND)
        stopper.cancel()
        await asyncio.wait([stopper, inner], timeout=_BOUND)

        assert stopper.cancelled()
        assert inner.cancelled()
        assert await asyncio.wait_for(first, _BOUND) == "disconnected"
    finally:
        release.set()
