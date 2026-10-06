"""Alfred live state: the one entry builder, and the publisher that keeps it in step.

build_entry maps an HA entity state to the (domain, kind, ContextEntry) Alfred's
LiveStateWriter takes. replace()'s snapshot and every update() both go through
it, so the two can never classify an entity differently.
"""

from __future__ import annotations

from collections.abc import Container, Mapping
from typing import Any, Literal

from alfred_sdk.context import ContextEntry, ContextSnapshot
from alfred_sdk.live_state import LiveStateWriter
from loguru import logger

from app.ha_connection import HAConnection, HAEntityState

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


class LiveStatePublisher:
    """Writes HA's states to Alfred as they change, and heals after a failed write.

    A failed write leaves Alfred's copy wrong in a way the next update cannot fix: an
    entity that changed during a Redis outage stays stale until it changes again, and a
    hash that a failed clear left behind survives underneath fresh updates. So any failed
    write marks the hash dirty, and while it is dirty the next state change republishes
    every entity instead of its own. A replace that lands clears the flag. Nothing here
    runs on a schedule: the heal rides on HA's next event.
    """

    def __init__(self, writer: LiveStateWriter, conn: HAConnection) -> None:
        self._writer = writer
        self._conn = conn
        self._dirty = False

    @property
    def dirty(self) -> bool:
        """A write failed and no replace has landed since."""
        return self._dirty

    async def publish(self) -> None:
        """Replace the whole hash with HA's current states (on connect, and to heal)."""
        # Built and handed over with no await between, so the writer's FIFO lock orders
        # it after every update requested before it.
        snapshot = build_snapshot(self._conn.states, self._conn.services_catalog)
        try:
            await self._writer.replace(snapshot)
        except Exception as exc:
            self._failed("replace", exc)
            return
        if self._dirty:
            self._dirty = False
            logger.info("Live state republished in full — back in step with HA")

    async def on_state_changed(
        self,
        entity_id: str,
        old_state: str | None,
        new_state: str | None,
        attributes: dict[str, Any],
    ) -> None:
        """HAConnection state listener; it applied the event to conn.states first."""
        if self._dirty:
            await self.publish()
            return
        try:
            state = self._conn.states.get(entity_id)
            if state is None:  # HA deleted the entity
                await self._writer.remove(entity_id)
                return
            domain, kind, entry = build_entry(state, self._conn.services_catalog)
            await self._writer.update(domain, kind, entry)
        except Exception as exc:
            self._failed(f"write of {entity_id}", exc)

    async def clear(self) -> None:
        """Drop the whole hash (at startup, on disconnect, at shutdown)."""
        try:
            await self._writer.clear()
        except Exception as exc:
            self._failed("clear", exc)

    def _failed(self, write: str, exc: Exception) -> None:
        # Once per outage: while dirty, every event retries quietly.
        if self._dirty:
            return
        self._dirty = True
        logger.warning(
            "Live state out of step with HA ({} failed: {}); "
            "the next connect or state change republishes it in full",
            write,
            exc,
        )
