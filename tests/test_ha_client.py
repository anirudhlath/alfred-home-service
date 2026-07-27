"""Tests for the REST fallback client (states snapshot only)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch


async def test_get_states_returns_entities() -> None:
    from app.ha_client import HomeAssistantClient

    mock_response = AsyncMock()
    mock_response.json = MagicMock(
        return_value=[
            {"entity_id": "light.living_room", "state": "on", "attributes": {"brightness": 255}},
            {"entity_id": "media_player.tv", "state": "playing", "attributes": {}},
        ]
    )
    mock_response.raise_for_status = lambda: None

    with patch("httpx.AsyncClient.get", return_value=mock_response):
        client = HomeAssistantClient(host="http://fake:8123", token="fake-token")
        states = await client.get_states()
    assert len(states) == 2
    assert states[0]["entity_id"] == "light.living_room"
