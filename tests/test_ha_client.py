"""Tests for Home Assistant client wrapper."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.mark.asyncio
async def test_get_states_returns_entities():
    from app.ha_client import HomeAssistantClient

    mock_response = AsyncMock()
    mock_response.json = MagicMock(
        return_value=[
            {
                "entity_id": "light.living_room",
                "state": "on",
                "attributes": {"brightness": 255},
            },
            {"entity_id": "media_player.tv", "state": "playing", "attributes": {}},
        ]
    )
    mock_response.raise_for_status = lambda: None

    with patch("httpx.AsyncClient.get", return_value=mock_response):
        client = HomeAssistantClient(host="http://fake:8123", token="fake-token")
        states = await client.get_states()
        assert len(states) == 2
        assert states[0]["entity_id"] == "light.living_room"


@pytest.mark.asyncio
async def test_call_service_sends_request():
    from app.ha_client import HomeAssistantClient

    mock_response = AsyncMock()
    mock_response.json = MagicMock(return_value=[])
    mock_response.raise_for_status = lambda: None

    with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
        client = HomeAssistantClient(host="http://fake:8123", token="fake-token")
        await client.call_service(
            "light", "turn_on", {"entity_id": "light.living_room", "brightness": 50}
        )

        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert "light/turn_on" in str(call_args)


@pytest.mark.asyncio
async def test_client_fails_fast_when_ha_unreachable():
    """HA being down must not stall Alfred's action pipeline.

    A dead HA (or Docker squatting the port and black-holing reads) must
    surface as an error within seconds, not httpx's 30s default — Alfred's
    HomeAgent times out at 30s, so home-service must always answer first.
    """
    from app.ha_client import HomeAssistantClient

    client = HomeAssistantClient(host="http://fake:8123", token="fake-token")
    timeout = client._get_client().timeout

    assert timeout.connect is not None and timeout.connect <= 3.0
    assert timeout.read is not None and timeout.read <= 10.0
