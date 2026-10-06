"""The one entry builder for Alfred live state.

Maps an HA entity state to the (domain, kind, ContextEntry) Alfred's
LiveStateWriter takes. replace()'s snapshot and every update() both go through
build_entry, so the two can never classify an entity differently.
"""

from __future__ import annotations

from collections.abc import Container, Mapping
from typing import Literal

from alfred_sdk.context import ContextEntry, ContextSnapshot

from app.ha_connection import HAEntityState

# Mirrors alfred_sdk.live_state.LiveStateKind, which mypy cannot see (alfred-sdk has no
# py.typed); the writer validates the value at runtime either way.
Kind = Literal["controllable", "sensor"]

# Attributes kept in live state — everything else is dropped to keep the prompts
# small (HA attributes can be huge, e.g. weather forecasts).
CONTEXT_ATTR_ALLOWLIST = frozenset(
    {
        "friendly_name",
        "device_class",
        "brightness",
        "current_temperature",
        "temperature",
        "media_title",
        "battery_level",
        "unit_of_measurement",
    }
)


def build_entry(
    state: HAEntityState, catalog_domains: Container[str]
) -> tuple[str, Kind, ContextEntry]:
    """(domain, kind, entry): a domain HA offers services for is controllable."""
    domain = state.entity_id.split(".", 1)[0]
    kind: Kind = "controllable" if domain in catalog_domains else "sensor"
    attributes = {k: v for k, v in state.attributes.items() if k in CONTEXT_ATTR_ALLOWLIST}
    return (
        domain,
        kind,
        ContextEntry(entity_id=state.entity_id, state=state.state, attributes=attributes),
    )


def build_snapshot(
    states: Mapping[str, HAEntityState], catalog_domains: Container[str]
) -> ContextSnapshot:
    """Every entity, grouped as Alfred's ContextSnapshot, in entity-ID order."""
    controllable: dict[str, list[ContextEntry]] = {}
    sensors: dict[str, list[ContextEntry]] = {}
    for entity_id in sorted(states):
        domain, kind, entry = build_entry(states[entity_id], catalog_domains)
        bucket = controllable if kind == "controllable" else sensors
        bucket.setdefault(domain, []).append(entry)
    return ContextSnapshot(controllable=controllable, sensors=sensors)
