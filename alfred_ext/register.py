"""Alfred integration for home-service.

Optional — this module is only used when alfred-sdk is installed.
The home-service works independently without it.
"""

from __future__ import annotations

import os
from alfred_sdk import AlfredClient

from app.ha_client import HomeAssistantClient

ha = HomeAssistantClient(
    host=os.getenv("HA_HOST", "http://homeassistant.local:8123"),
    token=os.getenv("HA_TOKEN", ""),
)

client = AlfredClient(
    service_name="home-service",
    service_endpoint=f"http://{os.getenv('HOSTNAME', 'home-service')}:8000/mcp",
)


@client.tool(name="smart_home.dim_lights", description="Dim lights in a room to a level (0-100)")
async def dim_lights(room: str, level: int) -> dict:
    entity_id = f"light.{room}"
    brightness = int(level * 2.55)  # Convert 0-100 to 0-255
    await ha.call_service("light", "turn_on", {"entity_id": entity_id, "brightness": brightness})
    return {"entity_id": entity_id, "brightness": level}


@client.tool(name="smart_home.turn_off_lights", description="Turn off all lights in a room")
async def turn_off_lights(room: str) -> dict:
    entity_id = f"light.{room}"
    await ha.call_service("light", "turn_off", {"entity_id": entity_id})
    return {"entity_id": entity_id, "state": "off"}


@client.tool(name="smart_home.set_scene", description="Activate a Home Assistant scene")
async def set_scene(scene_name: str) -> dict:
    entity_id = f"scene.{scene_name}"
    await ha.call_service("scene", "turn_on", {"entity_id": entity_id})
    return {"scene": scene_name, "activated": True}
