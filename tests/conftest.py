"""Shared fixtures for home-service tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from tests.fake_ha import FakeHAServer


@pytest.fixture
async def fake_ha() -> AsyncIterator[FakeHAServer]:
    server = FakeHAServer()
    await server.start()
    yield server
    await server.stop()
