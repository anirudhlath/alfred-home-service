"""Scene feature — activates Home Assistant scenes."""

from __future__ import annotations

import logging
from typing import Any

from alfred_sdk import BaseFeature, tool
from alfred_sdk.context import ContextEntry, ContextSnapshot

logger = logging.getLogger(__name__)


class SceneFeature(BaseFeature):
    """Smart home scene management."""

    feature_name = "scenes"

    def __init__(self, ctx: Any) -> None:
        super().__init__()
        self.ha = ctx.ha

    async def get_context(self) -> ContextSnapshot:
        """Return current state of all scene entities from HA."""
        try:
            states = await self.ha.get_states()
            entries = [
                ContextEntry(
                    entity_id=s["entity_id"],
                    state=s.get("state", "unknown"),
                    attributes=s.get("attributes", {}),
                )
                for s in states
                if s["entity_id"].startswith("scene.")
            ]
            return ContextSnapshot(controllable={"scene": entries})
        except Exception as e:
            logger.warning("Could not query HA for scene context: %s", e)
            return ContextSnapshot()

    @tool
    async def set_scene(self, scene_name: str) -> dict[str, Any]:
        """Activate a Home Assistant scene.

        Args:
            scene_name: The scene to activate.
        """
        entity_id = f"scene.{scene_name.replace(' ', '_').lower()}"
        await self.ha.call_service("scene", "turn_on", {"entity_id": entity_id})
        return {"scene": scene_name, "activated": True}
