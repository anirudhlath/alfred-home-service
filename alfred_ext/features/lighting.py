"""Lighting feature — controls smart home lights via Home Assistant."""

from __future__ import annotations

import logging
from typing import Any

from alfred_sdk import BaseFeature, tool
from alfred_sdk.context import ContextEntry, ContextSnapshot

logger = logging.getLogger(__name__)


class LightingFeature(BaseFeature):
    """Smart home lighting controls."""

    feature_name = "lighting"

    def __init__(self, ctx: Any) -> None:
        super().__init__()
        self.ha = ctx.ha

    async def get_context(self) -> ContextSnapshot:
        """Return current state of all light entities from HA."""
        try:
            states = await self.ha.get_states()
            entries = [
                ContextEntry(
                    entity_id=s["entity_id"],
                    state=s.get("state", "unknown"),
                    attributes=s.get("attributes", {}),
                )
                for s in states
                if s["entity_id"].startswith("light.")
            ]
            return ContextSnapshot(controllable={"light": entries})
        except Exception as e:
            logger.warning("Could not query HA for light context: %s", e)
            return ContextSnapshot()

    @tool
    async def dim_lights(self, room: str, level: int) -> dict[str, Any]:
        """Dim the lights in a room.

        Args:
            room: The room name.
            level: Brightness level 0-100.
        """
        entity_id = f"light.{room.replace(' ', '_').lower()}"
        brightness = int(level * 2.55)  # Convert 0-100 to 0-255
        await self.ha.call_service(
            "light", "turn_on", {"entity_id": entity_id, "brightness": brightness}
        )
        return {"entity_id": entity_id, "brightness": level}

    @tool
    async def turn_off_lights(self, room: str) -> dict[str, Any]:
        """Turn off all lights in a room.

        Args:
            room: The room name.
        """
        entity_id = f"light.{room.replace(' ', '_').lower()}"
        await self.ha.call_service("light", "turn_off", {"entity_id": entity_id})
        return {"entity_id": entity_id, "state": "off"}
