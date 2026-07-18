"""Lighting feature — controls smart home lights via Home Assistant."""

from __future__ import annotations

from typing import Any

from alfred_sdk import BaseFeature, tool
from alfred_sdk.context import ContextSnapshot

from alfred_ext.ha_utils import context_for_domain, to_entity_id


class LightingFeature(BaseFeature):  # type: ignore[misc] # alfred-sdk has no py.typed yet
    """Smart home lighting controls."""

    feature_name = "lighting"

    def __init__(self, ctx: Any) -> None:
        super().__init__()
        self.ha = ctx.ha

    async def get_context(self) -> ContextSnapshot:
        """Return current state of all light entities from HA."""
        return await context_for_domain(self.ha, "light")

    @tool  # type: ignore[untyped-decorator] # alfred-sdk has no py.typed yet
    async def dim_lights(self, room: str, level: int) -> dict[str, Any]:
        """Dim the lights in a room.

        Args:
            room: The room name.
            level: Brightness level 0-100.
        """
        entity_id = to_entity_id("light", room)
        brightness = int(level * 2.55)  # Convert 0-100 to 0-255
        await self.ha.call_service(
            "light", "turn_on", {"entity_id": entity_id, "brightness": brightness}
        )
        return {"entity_id": entity_id, "brightness": level}

    @tool  # type: ignore[untyped-decorator] # alfred-sdk has no py.typed yet
    async def turn_off_lights(self, room: str) -> dict[str, Any]:
        """Turn off all lights in a room.

        Args:
            room: The room name.
        """
        entity_id = to_entity_id("light", room)
        await self.ha.call_service("light", "turn_off", {"entity_id": entity_id})
        return {"entity_id": entity_id, "state": "off"}
