"""Lighting feature — controls smart home lights via Home Assistant."""

from __future__ import annotations

from typing import Any

from alfred_sdk import BaseFeature, tool


class LightingFeature(BaseFeature):
    """Smart home lighting controls."""

    feature_name = "lighting"

    def __init__(self, ctx: Any) -> None:
        super().__init__()
        self.ha = ctx.ha

    @tool
    async def dim_lights(self, room: str, level: int) -> dict:
        """Dim the lights in a room.

        Args:
            room: The room to dim.
            level: Brightness level 0-100.
        """
        entity_id = f"light.{room}"
        brightness = int(level * 2.55)  # Convert 0-100 to 0-255
        await self.ha.call_service(
            "light", "turn_on", {"entity_id": entity_id, "brightness": brightness}
        )
        return {"entity_id": entity_id, "brightness": level}

    @tool
    async def turn_off_lights(self, room: str) -> dict:
        """Turn off all lights in a room.

        Args:
            room: The room to turn off.
        """
        entity_id = f"light.{room}"
        await self.ha.call_service("light", "turn_off", {"entity_id": entity_id})
        return {"entity_id": entity_id, "state": "off"}
