"""Scene feature — activates Home Assistant scenes."""

from __future__ import annotations

from typing import Any

from alfred_sdk import BaseFeature, tool
from alfred_sdk.context import ContextSnapshot

from alfred_ext.ha_utils import context_for_domain, to_entity_id


class SceneFeature(BaseFeature):
    """Smart home scene management."""

    feature_name = "scenes"

    def __init__(self, ctx: Any) -> None:
        super().__init__()
        self.ha = ctx.ha

    async def get_context(self) -> ContextSnapshot:
        """Return current state of all scene entities from HA."""
        return await context_for_domain(self.ha, "scene")

    @tool
    async def set_scene(self, scene_name: str) -> dict[str, Any]:
        """Activate a Home Assistant scene.

        Args:
            scene_name: The scene to activate.
        """
        entity_id = to_entity_id("scene", scene_name)
        await self.ha.call_service("scene", "turn_on", {"entity_id": entity_id})
        return {"scene": scene_name, "activated": True}
