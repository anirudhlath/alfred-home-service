"""Tests for HAConnection against the fake HA WebSocket server."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.ha_connection import HACommandError, HAConnection
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
        state = await conn.apply_credentials(other.url, "other-token")
        assert state == "connected"
        assert "light.other" in conn.states
        assert "light.bedroom_lamp" not in conn.states
    finally:
        await other.stop()
