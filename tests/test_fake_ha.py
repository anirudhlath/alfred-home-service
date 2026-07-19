"""Self-tests for the fake HA WebSocket server."""

from __future__ import annotations

import json

from websockets.asyncio.client import connect

from tests.fake_ha import FakeHAServer


def _ws_url(server: FakeHAServer) -> str:
    return f"ws://127.0.0.1:{server.port}/api/websocket"


async def test_auth_ok_flow_and_get_states(fake_ha: FakeHAServer) -> None:
    async with connect(_ws_url(fake_ha)) as ws:
        assert json.loads(await ws.recv())["type"] == "auth_required"
        await ws.send(json.dumps({"type": "auth", "access_token": "test-token"}))
        assert json.loads(await ws.recv())["type"] == "auth_ok"
        await ws.send(json.dumps({"id": 1, "type": "get_states"}))
        msg = json.loads(await ws.recv())
        assert msg == {"id": 1, "type": "result", "success": True, "result": fake_ha.states}


async def test_auth_invalid_on_wrong_token(fake_ha: FakeHAServer) -> None:
    async with connect(_ws_url(fake_ha)) as ws:
        assert json.loads(await ws.recv())["type"] == "auth_required"
        await ws.send(json.dumps({"type": "auth", "access_token": "nope"}))
        assert json.loads(await ws.recv())["type"] == "auth_invalid"
    assert fake_ha.auth_attempts == 1


async def test_subscribe_and_event_push(fake_ha: FakeHAServer) -> None:
    async with connect(_ws_url(fake_ha)) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": "test-token"}))
        await ws.recv()
        await ws.send(
            json.dumps({"id": 7, "type": "subscribe_events", "event_type": "state_changed"})
        )
        assert json.loads(await ws.recv())["success"] is True
        await fake_ha.push_state_changed(
            "light.bedroom_lamp", "on", "off", {"friendly_name": "Bedroom Lamp"}
        )
        event = json.loads(await ws.recv())
        assert event["id"] == 7
        assert event["type"] == "event"
        assert event["event"]["event_type"] == "state_changed"
        assert event["event"]["data"]["entity_id"] == "light.bedroom_lamp"
        assert event["event"]["data"]["new_state"]["state"] == "off"


async def test_call_service_recorded_and_error_mode(fake_ha: FakeHAServer) -> None:
    async with connect(_ws_url(fake_ha)) as ws:
        await ws.recv()
        await ws.send(json.dumps({"type": "auth", "access_token": "test-token"}))
        await ws.recv()
        await ws.send(
            json.dumps(
                {
                    "id": 2,
                    "type": "call_service",
                    "domain": "light",
                    "service": "turn_on",
                    "service_data": {"brightness_pct": 40},
                    "target": {"entity_id": ["light.bedroom_lamp"]},
                }
            )
        )
        result = json.loads(await ws.recv())
        assert result["success"] is True and "context" in result["result"]
        assert fake_ha.service_calls == [
            {
                "domain": "light",
                "service": "turn_on",
                "service_data": {"brightness_pct": 40},
                "target": {"entity_id": ["light.bedroom_lamp"]},
            }
        ]
        fake_ha.fail_service_calls = True
        await ws.send(
            json.dumps({"id": 3, "type": "call_service", "domain": "light", "service": "turn_on"})
        )
        err = json.loads(await ws.recv())
        assert err["success"] is False
        assert err["error"]["code"] == "service_validation_error"
