"""Scene feature — activates Home Assistant scenes."""

from __future__ import annotations

from typing import Any

from alfred_sdk import BaseFeature, tool


class SceneFeature(BaseFeature):
    """Smart home scene management."""

    feature_name = "scenes"

    def __init__(self, ctx: Any) -> None:
        super().__init__()
        self.ha = ctx.ha

    @tool
    async def set_scene(self, scene_name: str) -> dict:
        """Activate a Home Assistant scene.

        Args:
            scene_name: The scene to activate.
        """
        entity_id = f"scene.{scene_name}"
        await self.ha.call_service("scene", "turn_on", {"entity_id": entity_id})
        return {"scene": scene_name, "activated": True}
