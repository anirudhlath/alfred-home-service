"""Tests for the MCP JSON-RPC server."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

HA_STATES = [
    {"entity_id": "light.living_room", "state": "on", "attributes": {}},
    {"entity_id": "light.bedroom", "state": "off", "attributes": {}},
    {"entity_id": "scene.movie_night", "state": "scening", "attributes": {}},
]


@pytest.mark.asyncio
async def test_mcp_endpoint_dispatches_tool_call() -> None:
    with patch(
        "app.ha_client.HomeAssistantClient.call_service",
        new_callable=AsyncMock,
        return_value=[],
    ):
        from app.server import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/mcp",
                json={
                    "method": "lighting.dim_lights",
                    "params": {"room": "living_room", "level": 20},
                    "id": "req-001",
                },
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "req-001"
    assert "result" in data
    assert data["result"]["entity_id"] == "light.living_room"


@pytest.mark.asyncio
async def test_mcp_endpoint_normalizes_room_name_with_spaces() -> None:
    """Room names with spaces are converted to valid HA entity_ids."""
    with patch(
        "app.ha_client.HomeAssistantClient.call_service",
        new_callable=AsyncMock,
        return_value=[],
    ):
        from app.server import app

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/mcp",
                json={
                    "method": "lighting.dim_lights",
                    "params": {"room": "living room", "level": 40},
                    "id": "req-003",
                },
            )

    assert resp.status_code == 200
    data = resp.json()
    assert data["result"]["entity_id"] == "light.living_room"


@pytest.mark.asyncio
async def test_lighting_get_context_returns_light_entities() -> None:
    """LightingFeature.get_context() returns structured light entity data."""
    mock_ha = AsyncMock()
    mock_ha.get_states = AsyncMock(return_value=HA_STATES)

    class Ctx:
        ha = mock_ha

    from alfred_ext.features.lighting import LightingFeature

    feature = LightingFeature(Ctx())
    context = await feature.get_context()

    assert "light" in context.controllable
    entity_ids = [e.entity_id for e in context.controllable["light"]]
    assert "light.living_room" in entity_ids
    assert "light.bedroom" in entity_ids


@pytest.mark.asyncio
async def test_scenes_get_context_returns_scene_entities() -> None:
    """SceneFeature.get_context() returns structured scene entity data."""
    mock_ha = AsyncMock()
    mock_ha.get_states = AsyncMock(return_value=HA_STATES)

    class Ctx:
        ha = mock_ha

    from alfred_ext.features.scenes import SceneFeature

    feature = SceneFeature(Ctx())
    context = await feature.get_context()

    assert "scene" in context.controllable
    entity_ids = [e.entity_id for e in context.controllable["scene"]]
    assert "scene.movie_night" in entity_ids


@pytest.mark.asyncio
async def test_mcp_endpoint_unknown_method_returns_error() -> None:
    from app.server import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/mcp",
            json={
                "method": "nonexistent.tool",
                "params": {},
                "id": "req-002",
            },
        )

    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "req-002"
    assert "error" in data


@pytest.mark.asyncio
async def test_health_endpoint() -> None:
    from app.server import app

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")

    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
