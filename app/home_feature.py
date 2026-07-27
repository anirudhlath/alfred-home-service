"""HomeCapabilitiesFeature — the generated BaseFeature bound for SDK dispatch.

One feature instance carries ALL generated tools. Handlers are bound as
instance attributes named after each spec's method_name because
AlfredClient.discover_features_from_classes resolves dispatch callables via
getattr(instance, tool_meta.name.split(".")[-1]).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from alfred_sdk.context import ContextEntry, ContextSnapshot
from alfred_sdk.feature import BaseFeature, ToolMeta

from app.capability_generator import CapabilityGenerator, GeneratedToolSpec
from app.entity_index import EntityIndex
from app.ha_connection import HAEntityState

# Attributes kept in context snapshots — everything else is dropped to keep
# the Reflex prompt small (HA attributes can be huge, e.g. weather forecasts).
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


class HAConnectionLike(Protocol):
    """Structural slice of HAConnection used by generated capabilities."""

    states: dict[str, HAEntityState]
    services_catalog: dict[str, Any]

    async def call_service(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any] | None = None,
        entity_ids: list[str] | None = None,
    ) -> dict[str, Any]: ...


class HomeCapabilitiesContext:
    """Dependencies handed to HomeCapabilitiesFeature by AlfredClient discovery."""

    def __init__(
        self,
        conn: HAConnectionLike,
        index: EntityIndex,
        generator: CapabilityGenerator,
        specs: list[GeneratedToolSpec],
    ) -> None:
        self.conn = conn
        self.index = index
        self.generator = generator
        self.specs = specs


class HomeCapabilitiesFeature(BaseFeature):  # type: ignore[misc] # alfred-sdk has no py.typed yet
    """Generated Home Assistant control surface."""

    feature_name = "home"

    def __init__(self, ctx: HomeCapabilitiesContext) -> None:
        super().__init__()
        self._conn = ctx.conn
        self._index = ctx.index
        self._generator = ctx.generator
        self._specs = ctx.specs
        self._specs_by_name = {spec.tool_name: spec for spec in ctx.specs}
        for spec in ctx.specs:
            setattr(self, spec.method_name, self._make_handler(spec))

    def get_tools(self) -> list[ToolMeta]:
        """Override BaseFeature discovery — inject live area/entity values.

        Same override pattern as alfred's TriggerFeature.get_tools(). Called on
        every register(), so re-registration keeps descriptions fresh.
        """
        return [self._generator.build_tool_meta(spec, self._index) for spec in self._specs]

    def _make_handler(self, spec: GeneratedToolSpec) -> Callable[..., Awaitable[dict[str, Any]]]:
        async def handler(**params: Any) -> dict[str, Any]:
            return await self._execute(spec, params)

        handler.__name__ = spec.method_name
        return handler

    async def _execute_by_name(self, tool_name: str, params: dict[str, Any]) -> dict[str, Any]:
        """Test seam: run a tool by its registered name."""
        return await self._execute(self._specs_by_name[tool_name], params)

    async def _execute(self, spec: GeneratedToolSpec, params: dict[str, Any]) -> dict[str, Any]:
        if spec.domain is None or spec.service is None:
            # home.call_service escape hatch
            domain = str(params["domain"])
            service = str(params["service"])
            data = dict(params.get("data") or {})
            entity_id = params.get("entity_id")
            await self._conn.call_service(
                domain, service, data, [str(entity_id)] if entity_id else None
            )
            return {"domain": domain, "service": service, "entity_id": entity_id, "status": "ok"}
        entity_ids: list[str] | None = None
        if spec.targeted:
            target = params.get("target")
            if not target:
                raise ValueError(f"Missing required parameter 'target' for {spec.tool_name}")
            entity_ids = self._index.resolve(spec.domain, str(target))
        service_data = {
            f.name: params[f.name] for f in spec.fields if params.get(f.name) is not None
        }
        await self._conn.call_service(spec.domain, spec.service, service_data, entity_ids)
        return {
            "domain": spec.domain,
            "service": spec.service,
            "entity_ids": entity_ids,
            "service_data": service_data,
            "status": "ok",
        }

    async def get_context(self) -> ContextSnapshot:
        """Live context from the connection's state store (fed by WS events)."""
        controllable: dict[str, list[ContextEntry]] = {}
        sensors: dict[str, list[ContextEntry]] = {}
        catalog_domains = set(self._conn.services_catalog)
        for entity_id in sorted(self._conn.states):
            st = self._conn.states[entity_id]
            domain = entity_id.split(".", 1)[0]
            attrs = {k: v for k, v in st.attributes.items() if k in CONTEXT_ATTR_ALLOWLIST}
            entry = ContextEntry(entity_id=entity_id, state=st.state, attributes=attrs)
            bucket = controllable if domain in catalog_domains else sensors
            bucket.setdefault(domain, []).append(entry)
        return ContextSnapshot(controllable=controllable, sensors=sensors)
