"""EntityIndex — discovered HA entity metadata + human-name resolution.

Built from the HA entity/device/area registries plus live states; rebuilt on
registry-updated events. Replaces the deleted to_entity_id() name-guessing:
all tool execution resolves areas / friendly names → real entity IDs here.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.ha_connection import HAEntityState


class EntityInfo(BaseModel):
    """Resolved metadata for one entity."""

    entity_id: str
    friendly_name: str
    domain: str
    device_class: str | None = None
    area: str | None = None
    device: str | None = None


class EntityIndex:
    """entity_id → EntityInfo, with area / friendly-name → entity_ids resolution."""

    def __init__(self) -> None:
        self._entities: dict[str, EntityInfo] = {}
        self._area_names: list[str] = []

    def rebuild(
        self,
        *,
        entity_registry: list[dict[str, Any]],
        device_registry: list[dict[str, Any]],
        area_registry: list[dict[str, Any]],
        states: dict[str, HAEntityState],
    ) -> None:
        areas = {str(a["area_id"]): str(a["name"]) for a in area_registry}
        devices = {str(d["id"]): d for d in device_registry}
        reg_by_id = {str(e["entity_id"]): e for e in entity_registry}

        entities: dict[str, EntityInfo] = {}
        for entity_id in sorted(set(states) | set(reg_by_id)):
            reg = reg_by_id.get(entity_id, {})
            if reg.get("disabled_by"):
                continue
            st = states.get(entity_id)
            attributes: dict[str, Any] = st.attributes if st is not None else {}
            device = devices.get(str(reg.get("device_id") or ""), {})
            area_id = reg.get("area_id") or device.get("area_id")
            friendly = (
                attributes.get("friendly_name")
                or reg.get("name")
                or reg.get("original_name")
                or entity_id.split(".", 1)[-1].replace("_", " ")
            )
            entities[entity_id] = EntityInfo(
                entity_id=entity_id,
                friendly_name=str(friendly),
                domain=entity_id.split(".", 1)[0],
                device_class=attributes.get("device_class"),
                area=areas.get(str(area_id)) if area_id else None,
                device=(device.get("name_by_user") or device.get("name")) if device else None,
            )
        self._entities = entities
        self._area_names = sorted(areas.values())

    def get(self, entity_id: str) -> EntityInfo | None:
        return self._entities.get(entity_id)

    def entity_count(self) -> int:
        return len(self._entities)

    def area_count(self) -> int:
        return len(self._area_names)

    def area_names(self) -> list[str]:
        return list(self._area_names)

    def domains(self) -> set[str]:
        return {e.domain for e in self._entities.values()}

    def entities_for_domain(self, domain: str) -> list[EntityInfo]:
        return sorted(
            (e for e in self._entities.values() if e.domain == domain),
            key=lambda e: e.entity_id,
        )

    def resolve(self, domain: str, target: str) -> list[str]:
        """Resolve an area name / friendly name / entity_id to concrete entity_ids.

        Raises LookupError listing the available options so the calling LLM
        can self-correct from the in-band /mcp error.
        """
        wanted = target.strip().casefold()
        candidates = self.entities_for_domain(domain)
        exact = [e.entity_id for e in candidates if e.entity_id.casefold() == wanted]
        if exact:
            return exact
        by_area = [
            e.entity_id for e in candidates if e.area is not None and e.area.casefold() == wanted
        ]
        if by_area:
            return by_area
        by_name = [e.entity_id for e in candidates if e.friendly_name.casefold() == wanted]
        if by_name:
            return by_name
        available_areas = sorted({e.area for e in candidates if e.area is not None})
        available_names = sorted(e.friendly_name for e in candidates)
        raise LookupError(
            f"No {domain} entity matches '{target}'. "
            f"Areas: {', '.join(available_areas) or 'none'}. "
            f"{domain} entities: {', '.join(available_names) or 'none'}."
        )
