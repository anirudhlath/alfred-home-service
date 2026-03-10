"""Tests for the MCP JSON-RPC server."""

from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient


@pytest.mark.asyncio
async def test_mcp_endpoint_dispatches_tool_call() -> None:
    # Mock HA client so tools don't make real HTTP calls
    with patch("alfred_ext.register.ha") as mock_ha:
        mock_ha.call_service = AsyncMock(return_value=[])

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
