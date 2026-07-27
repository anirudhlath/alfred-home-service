"""Integration tests for the rewritten server: /credentials, /health, /mcp, wiring."""

from __future__ import annotations

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.server import CredentialsBody, apply_env_credentials, create_app
from tests.fake_ha import FakeHAServer, eventually


@pytest.fixture
async def app() -> AsyncIterator[FastAPI]:
    application = create_app()
    # keep Redis out of tests — registration is best-effort by design
    application.state.client.register = AsyncMock()
    application.state.client.unregister = AsyncMock()
    yield application
    await application.state.refresher.stop()
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
