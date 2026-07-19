"""Shared fixtures for home-service tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from app.ha_connection import HAEntityState
from tests.fake_ha import DEFAULT_STATES, FakeHAServer


@pytest.fixture
async def fake_ha() -> AsyncIterator[FakeHAServer]:
    server = FakeHAServer()
    await server.start()
    yield server
    await server.stop()


@pytest.fixture
def default_states_map() -> dict[str, HAEntityState]:
    return {
        s["entity_id"]: HAEntityState(
            entity_id=s["entity_id"], state=s["state"], attributes=s.get("attributes", {})
        )
        for s in DEFAULT_STATES
    }
