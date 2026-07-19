"""Fake Home Assistant WebSocket server for tests.

Speaks the exact message shapes of the HA WebSocket API (verified against
https://developers.home-assistant.io/docs/api/websocket and HA core source):
auth_required/auth/auth_ok/auth_invalid, subscribe_events, get_states,
get_services, config/{entity,device,area}_registry/list, call_service, and
event push for state_changed / *_registry_updated.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from typing import Any

from websockets.asyncio.server import Server, ServerConnection, serve

DEFAULT_AREA_REGISTRY: list[dict[str, Any]] = [
    {
        "area_id": "living_room",
        "name": "Living Room",
        "aliases": [],
        "floor_id": None,
        "icon": None,
        "labels": [],
        "picture": None,
    },
    {
        "area_id": "bedroom",
        "name": "Bedroom",
        "aliases": [],
        "floor_id": None,
        "icon": None,
        "labels": [],
        "picture": None,
    },
    {
        "area_id": "garage",
        "name": "Garage",
        "aliases": [],
        "floor_id": None,
        "icon": None,
        "labels": [],
        "picture": None,
    },
]

DEFAULT_DEVICE_REGISTRY: list[dict[str, Any]] = [
    {
        "id": "dev-tv",
        "area_id": "living_room",
        "name": "Living Room TV",
        "name_by_user": None,
        "manufacturer": "LG",
        "model": "C3",
    },
    {
        "id": "dev-garage-opener",
        "area_id": "garage",
        "name": "Garage Door Opener",
        "name_by_user": None,
        "manufacturer": "Chamberlain",
        "model": "MyQ",
    },
]

DEFAULT_ENTITY_REGISTRY: list[dict[str, Any]] = [
    {
        "entity_id": "light.living_room_lamp",
        "area_id": "living_room",
        "device_id": None,
        "name": None,
        "original_name": "Living Room Lamp",
        "disabled_by": None,
        "platform": "hue",
    },
    {
        "entity_id": "light.bedroom_lamp",
        "area_id": "bedroom",
        "device_id": None,
        "name": "Bedroom Lamp",
        "original_name": None,
        "disabled_by": None,
        "platform": "hue",
    },
    {
        "entity_id": "light.closet",
        "area_id": None,
        "device_id": None,
        "name": None,
        "original_name": "Closet Light",
        "disabled_by": None,
        "platform": "hue",
    },
    {
        "entity_id": "light.disabled_lamp",
        "area_id": None,
        "device_id": None,
        "name": None,
        "original_name": "Disabled Lamp",
        "disabled_by": "user",
        "platform": "hue",
    },
    {
        "entity_id": "switch.coffee_maker",
        "area_id": "living_room",
        "device_id": None,
        "name": None,
        "original_name": "Coffee Maker",
        "disabled_by": None,
        "platform": "tplink",
    },
    {
        "entity_id": "media_player.tv",
        "area_id": None,
        "device_id": "dev-tv",
        "name": None,
        "original_name": "TV",
        "disabled_by": None,
        "platform": "webostv",
    },
    {
        "entity_id": "scene.movie_night",
        "area_id": None,
        "device_id": None,
        "name": None,
        "original_name": "Movie Night",
        "disabled_by": None,
        "platform": "homeassistant",
    },
    {
        "entity_id": "climate.thermostat",
        "area_id": "living_room",
        "device_id": None,
        "name": None,
        "original_name": "Thermostat",
        "disabled_by": None,
        "platform": "nest",
    },
    {
        "entity_id": "lock.front_door",
        "area_id": None,
        "device_id": None,
        "name": None,
        "original_name": "Front Door",
        "disabled_by": None,
        "platform": "august",
    },
    {
        "entity_id": "cover.garage_door",
        "area_id": None,
        "device_id": "dev-garage-opener",
        "name": None,
        "original_name": "Garage Door",
        "disabled_by": None,
        "platform": "myq",
    },
    {
        "entity_id": "binary_sensor.hallway_motion",
        "area_id": None,
        "device_id": None,
        "name": None,
        "original_name": "Hallway Motion",
        "disabled_by": None,
        "platform": "zha",
    },
]

DEFAULT_STATES: list[dict[str, Any]] = [
    {
        "entity_id": "light.living_room_lamp",
        "state": "off",
        "attributes": {"friendly_name": "Living Room Lamp"},
    },
    {
        "entity_id": "light.bedroom_lamp",
        "state": "on",
        "attributes": {"friendly_name": "Bedroom Lamp", "brightness": 128},
    },
    {
        "entity_id": "switch.coffee_maker",
        "state": "off",
        "attributes": {"friendly_name": "Coffee Maker"},
    },
    {"entity_id": "media_player.tv", "state": "idle", "attributes": {"friendly_name": "TV"}},
    {
        "entity_id": "scene.movie_night",
        "state": "scening",
        "attributes": {"friendly_name": "Movie Night"},
    },
    {
        "entity_id": "climate.thermostat",
        "state": "heat",
        "attributes": {"friendly_name": "Thermostat", "current_temperature": 21.5},
    },
    {
        "entity_id": "lock.front_door",
        "state": "locked",
        "attributes": {"friendly_name": "Front Door"},
    },
    {
        "entity_id": "cover.garage_door",
        "state": "closed",
        "attributes": {"friendly_name": "Garage Door", "device_class": "garage"},
    },
    {
        "entity_id": "binary_sensor.hallway_motion",
        "state": "off",
        "attributes": {"friendly_name": "Hallway Motion", "device_class": "motion"},
    },
    {
        "entity_id": "sensor.outdoor_temp",
        "state": "18.4",
        "attributes": {"friendly_name": "Outdoor Temp", "unit_of_measurement": "°C"},
    },
]

_TARGET = {"entity": [{}]}

DEFAULT_SERVICES: dict[str, Any] = {
    "light": {
        "turn_on": {
            "name": "Turn on",
            "description": "Turn on one or more lights.",
            "fields": {
                "brightness_pct": {
                    "name": "Brightness",
                    "description": "Brightness percentage.",
                    "example": 50,
                    "selector": {"number": {"min": 0, "max": 100}},
                },
                "color_name": {
                    "name": "Color",
                    "description": "Color name.",
                    "selector": {"text": None},
                },
            },
            "target": _TARGET,
        },
        "turn_off": {
            "name": "Turn off",
            "description": "Turn off one or more lights.",
            "fields": {},
            "target": _TARGET,
        },
        "toggle": {
            "name": "Toggle",
            "description": "Toggle one or more lights.",
            "fields": {},
            "target": _TARGET,
        },
    },
    "switch": {
        "turn_on": {
            "name": "Turn on",
            "description": "Turn a switch on.",
            "fields": {},
            "target": _TARGET,
        },
        "turn_off": {
            "name": "Turn off",
            "description": "Turn a switch off.",
            "fields": {},
            "target": _TARGET,
        },
        "toggle": {
            "name": "Toggle",
            "description": "Toggle a switch.",
            "fields": {},
            "target": _TARGET,
        },
    },
    "media_player": {
        "turn_on": {
            "name": "Turn on",
            "description": "Turn a media player on.",
            "fields": {},
            "target": _TARGET,
        },
        "turn_off": {
            "name": "Turn off",
            "description": "Turn a media player off.",
            "fields": {},
            "target": _TARGET,
        },
        "media_play": {
            "name": "Play",
            "description": "Start playing media.",
            "fields": {},
            "target": _TARGET,
        },
        "media_pause": {
            "name": "Pause",
            "description": "Pause playing media.",
            "fields": {},
            "target": _TARGET,
        },
        "volume_set": {
            "name": "Set volume",
            "description": "Set the playback volume.",
            "fields": {
                "volume_level": {
                    "name": "Level",
                    "description": "Volume 0..1.",
                    "required": True,
                    "selector": {"number": {"min": 0, "max": 1}},
                }
            },
            "target": _TARGET,
        },
    },
    "scene": {
        "turn_on": {
            "name": "Activate",
            "description": "Activate a scene.",
            "fields": {},
            "target": _TARGET,
        },
    },
    "climate": {
        "set_temperature": {
            "name": "Set target temperature",
            "description": "Set the target temperature.",
            "fields": {
                "temperature": {
                    "name": "Temperature",
                    "description": "Target temperature.",
                    "selector": {"number": {"min": 7, "max": 35}},
                }
            },
            "target": _TARGET,
        },
        "set_hvac_mode": {
            "name": "Set HVAC mode",
            "description": "Set the HVAC operation mode.",
            "fields": {
                "hvac_mode": {
                    "name": "Mode",
                    "description": "heat, cool, off, ...",
                    "selector": {"select": {"options": ["heat", "cool", "off"]}},
                }
            },
            "target": _TARGET,
        },
    },
    "lock": {
        "lock": {"name": "Lock", "description": "Lock a lock.", "fields": {}, "target": _TARGET},
        "unlock": {
            "name": "Unlock",
            "description": "Unlock a lock.",
            "fields": {},
            "target": _TARGET,
        },
    },
    "cover": {
        "open_cover": {
            "name": "Open",
            "description": "Open a cover.",
            "fields": {},
            "target": _TARGET,
        },
        "close_cover": {
            "name": "Close",
            "description": "Close a cover.",
            "fields": {},
            "target": _TARGET,
        },
    },
    # A domain with services but NO entities in the fixture — must generate no tools.
    "homeassistant": {
        "restart": {"name": "Restart", "description": "Restart Home Assistant.", "fields": {}},
    },
}


async def eventually(
    predicate: Callable[[], bool], *, timeout: float = 2.0, interval: float = 0.02
) -> None:
    """Await until predicate() is true or fail the test (test-only helper)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(interval)
    raise AssertionError("condition not met within timeout")


class FakeHAServer:
    """Minimal in-process Home Assistant WebSocket API double."""

    def __init__(
        self,
        *,
        token: str = "test-token",
        states: list[dict[str, Any]] | None = None,
        services: dict[str, Any] | None = None,
        entity_registry: list[dict[str, Any]] | None = None,
        device_registry: list[dict[str, Any]] | None = None,
        area_registry: list[dict[str, Any]] | None = None,
    ) -> None:
        self.token = token
        self.states = list(states if states is not None else DEFAULT_STATES)
        self.services = dict(services if services is not None else DEFAULT_SERVICES)
        self.entity_registry = list(
            entity_registry if entity_registry is not None else DEFAULT_ENTITY_REGISTRY
        )
        self.device_registry = list(
            device_registry if device_registry is not None else DEFAULT_DEVICE_REGISTRY
        )
        self.area_registry = list(
            area_registry if area_registry is not None else DEFAULT_AREA_REGISTRY
        )
        self.service_calls: list[dict[str, Any]] = []
        self.subscriptions: dict[str, int] = {}
        self.auth_attempts = 0
        self.fail_service_calls = False
        self.port = 0
        self._server: Server | None = None
        self._connections: set[ServerConnection] = set()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    async def start(self) -> None:
        self._server = await serve(self._handler, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def drop_connections(self) -> None:
        """Close all live connections (simulates HA restart). Clears subscriptions."""
        self.subscriptions = {}
        for ws in list(self._connections):
            await ws.close()

    async def _handler(self, ws: ServerConnection) -> None:
        self._connections.add(ws)
        try:
            await ws.send(json.dumps({"type": "auth_required", "ha_version": "2026.7.0"}))
            msg = json.loads(await ws.recv())
            self.auth_attempts += 1
            if msg.get("type") != "auth" or msg.get("access_token") != self.token:
                await ws.send(
                    json.dumps({"type": "auth_invalid", "message": "Invalid access token"})
                )
                return
            await ws.send(json.dumps({"type": "auth_ok", "ha_version": "2026.7.0"}))
            async for raw in ws:
                await self._handle_command(ws, json.loads(raw))
        except Exception:
            pass  # connection torn down mid-test — fine
        finally:
            self._connections.discard(ws)

    async def _handle_command(self, ws: ServerConnection, msg: dict[str, Any]) -> None:
        msg_id = int(msg["id"])
        match msg.get("type"):
            case "subscribe_events":
                self.subscriptions[str(msg.get("event_type", "*"))] = msg_id
                await self._send_result(ws, msg_id, None)
            case "get_states":
                await self._send_result(ws, msg_id, self.states)
            case "get_services":
                await self._send_result(ws, msg_id, self.services)
            case "config/entity_registry/list":
                await self._send_result(ws, msg_id, self.entity_registry)
            case "config/device_registry/list":
                await self._send_result(ws, msg_id, self.device_registry)
            case "config/area_registry/list":
                await self._send_result(ws, msg_id, self.area_registry)
            case "call_service":
                if self.fail_service_calls:
                    await ws.send(
                        json.dumps(
                            {
                                "id": msg_id,
                                "type": "result",
                                "success": False,
                                "error": {"code": "service_validation_error", "message": "boom"},
                            }
                        )
                    )
                    return
                self.service_calls.append(
                    {
                        "domain": msg.get("domain"),
                        "service": msg.get("service"),
                        "service_data": msg.get("service_data"),
                        "target": msg.get("target"),
                    }
                )
                await self._send_result(ws, msg_id, {"context": {"id": "ctx-1"}})
            case other:
                await ws.send(
                    json.dumps(
                        {
                            "id": msg_id,
                            "type": "result",
                            "success": False,
                            "error": {
                                "code": "unknown_command",
                                "message": f"Unknown command: {other}",
                            },
                        }
                    )
                )

    async def _send_result(self, ws: ServerConnection, msg_id: int, result: Any) -> None:
        await ws.send(
            json.dumps({"id": msg_id, "type": "result", "success": True, "result": result})
        )

    async def push_state_changed(
        self,
        entity_id: str,
        old_state: str | None,
        new_state: str | None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        sub_id = self.subscriptions["state_changed"]
        attrs = attributes or {}
        data: dict[str, Any] = {
            "entity_id": entity_id,
            "old_state": (
                {"entity_id": entity_id, "state": old_state, "attributes": attrs}
                if old_state is not None
                else None
            ),
            "new_state": (
                {"entity_id": entity_id, "state": new_state, "attributes": attrs}
                if new_state is not None
                else None
            ),
        }
        await self._broadcast(
            {
                "id": sub_id,
                "type": "event",
                "event": {"event_type": "state_changed", "data": data},
            }
        )

    async def push_registry_updated(self, kind: str, data: dict[str, Any]) -> None:
        """kind: 'entity' | 'device' | 'area'."""
        event_type = f"{kind}_registry_updated"
        sub_id = self.subscriptions[event_type]
        await self._broadcast(
            {
                "id": sub_id,
                "type": "event",
                "event": {"event_type": event_type, "data": data},
            }
        )

    async def _broadcast(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message)
        for ws in list(self._connections):
            await ws.send(payload)
