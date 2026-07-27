"""CapabilityGenerator — generates the tool surface from HA's own registries.

Crosses the HA service catalog (get_services) with the EntityIndex and emits
SDK ToolMeta tagged with audience ("reflex" | "conscious") and risk
("benign" | "elevated" | "critical") per config/risk_map.yaml (contract C9).

Live area/entity values are injected into parameter descriptions at manifest
build time — the Reflex Engine renders parameter descriptions into its prompt
(alfred core/reflex/engine.py "Include parameter descriptions").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from alfred_sdk.feature import ToolMeta, ToolParameter

from app.entity_index import EntityIndex
from app.risk_map import Risk, RiskMap

Audience = Literal["reflex", "conscious"]

# Contract C9 audience rule: tools over these domains → audience reflex, risk benign.
REFLEX_DOMAINS = frozenset({"light", "switch", "media_player", "scene"})

MAX_LISTED_ENTITIES = 30  # cap injected entity lists to bound prompt size


@dataclass(frozen=True)
class FieldSpec:
    """A non-target tool parameter derived from an HA service field."""

    name: str
    type: str
    description: str


@dataclass(frozen=True)
class GeneratedToolSpec:
    """Static shape of one generated tool (descriptions get live values later)."""

    tool_name: str  # e.g. "home.light_turn_on" — what Alfred dispatches to /mcp
    method_name: str  # e.g. "light_turn_on" — bound attribute on the feature
    domain: str | None  # None → generic call_service escape hatch
    service: str | None
    description: str
    audience: Audience
    risk: Risk
    fields: tuple[FieldSpec, ...]
    targeted: bool


def _field_type(fdef: dict[str, Any]) -> str:
    selector = fdef.get("selector") or {}
    if "number" in selector:
        return "float"
    if "boolean" in selector:
        return "bool"
    if "object" in selector:
        return "dict"
    return "str"


def _field_spec(name: str, fdef: dict[str, Any] | None) -> FieldSpec:
    fdef = fdef or {}
    description = str(fdef.get("description") or fdef.get("name") or name)
    example = fdef.get("example")
    if example is not None:
        description += f" Example: {example}."
    if fdef.get("required"):
        description += " (required)"
    return FieldSpec(name=name, type=_field_type(fdef), description=description)


def _service_description(svc: dict[str, Any], domain: str, service: str) -> str:
    raw = str(svc.get("description") or svc.get("name") or f"{domain}.{service}")
    return raw.split("\n")[0]


class CapabilityGenerator:
    """HA service catalog × EntityIndex → tagged tool specs and manifests."""

    def __init__(self, risk_map: RiskMap, reflex_config: dict[str, dict[str, list[str]]]) -> None:
        self._risk_map = risk_map
        self._reflex_config = reflex_config

    def generate(self, catalog: dict[str, Any], index: EntityIndex) -> list[GeneratedToolSpec]:
        """Build the static tool set (frozen for the process lifetime).

        New HA domains appearing later require a service restart; entity/area
        renames stay live via build_tool_meta() + context snapshots.
        """
        specs: list[GeneratedToolSpec] = []
        domains_present = index.domains()
        for domain in sorted(catalog):
            services: dict[str, Any] = catalog[domain] or {}
            if domain not in domains_present:
                continue  # a service domain with no entities gets no tools
            if domain in REFLEX_DOMAINS:
                specs.extend(self._reflex_specs(domain, services))
            else:
                specs.extend(self._conscious_specs(domain, services, index))
        specs.append(
            GeneratedToolSpec(
                tool_name="home.call_service",
                method_name="call_service",
                domain=None,
                service=None,
                description=(
                    "Call any Home Assistant service directly — escape hatch for "
                    "operations without a dedicated tool."
                ),
                audience="conscious",
                risk="critical",  # can reach locks/alarms; v1 requires confirmation
                fields=(),
                targeted=False,
            )
        )
        return specs

    def _reflex_specs(self, domain: str, services: dict[str, Any]) -> list[GeneratedToolSpec]:
        specs: list[GeneratedToolSpec] = []
        for service, extra_fields in self._reflex_config.get(domain, {}).items():
            svc = services.get(service)
            if svc is None:
                continue  # this HA doesn't offer the service
            catalog_fields: dict[str, Any] = svc.get("fields") or {}
            fields = tuple(
                _field_spec(f, catalog_fields.get(f)) for f in extra_fields if f in catalog_fields
            )
            specs.append(
                GeneratedToolSpec(
                    tool_name=f"home.{domain}_{service}",
                    method_name=f"{domain}_{service}",
                    domain=domain,
                    service=service,
                    description=_service_description(svc, domain, service),
                    audience="reflex",
                    risk="benign",  # contract C9 audience rule
                    fields=fields,
                    targeted=True,
                )
            )
        return specs

    def _conscious_specs(
        self, domain: str, services: dict[str, Any], index: EntityIndex
    ) -> list[GeneratedToolSpec]:
        risk = self._risk_map.domain_tool_risk(domain, index)
        specs: list[GeneratedToolSpec] = []
        for service in sorted(services):
            svc: dict[str, Any] = services[service] or {}
            catalog_fields: dict[str, Any] = svc.get("fields") or {}
            fields = tuple(_field_spec(name, fdef) for name, fdef in sorted(catalog_fields.items()))
            specs.append(
                GeneratedToolSpec(
                    tool_name=f"home.{domain}_{service}",
                    method_name=f"{domain}_{service}",
                    domain=domain,
                    service=service,
                    description=_service_description(svc, domain, service),
                    audience="conscious",
                    risk=risk,
                    fields=fields,
                    targeted=svc.get("target") is not None,
                )
            )
        return specs

    def build_tool_meta(self, spec: GeneratedToolSpec, index: EntityIndex) -> ToolMeta:
        """Build a ToolMeta with LIVE area/entity values in parameter descriptions."""
        parameters: dict[str, ToolParameter] = {}
        if spec.domain is None:
            parameters = {
                "domain": ToolParameter(type="str", description="HA service domain, e.g. 'light'."),
                "service": ToolParameter(type="str", description="Service name, e.g. 'turn_on'."),
                "entity_id": ToolParameter(
                    type="str",
                    description="Optional entity_id to target, e.g. 'light.living_room_lamp'.",
                ),
                "data": ToolParameter(type="dict", description="Optional service data fields."),
            }
        else:
            if spec.targeted:
                parameters["target"] = ToolParameter(
                    type="str", description=self._target_description(spec.domain, index)
                )
            for f in spec.fields:
                parameters[f.name] = ToolParameter(type=f.type, description=f.description)
        return ToolMeta(
            name=spec.tool_name,
            description=spec.description,
            parameters=parameters,
            audience=spec.audience,
            risk=spec.risk,
        )

    def _target_description(self, domain: str, index: EntityIndex) -> str:
        entities = index.entities_for_domain(domain)
        areas = sorted({e.area for e in entities if e.area is not None})
        names = sorted({e.friendly_name for e in entities})[:MAX_LISTED_ENTITIES]
        description = "Area name, entity friendly name, or entity_id."
        if areas:
            description += f" Available areas: {', '.join(areas)}."
        if names:
            description += f" Available {domain} entities: {', '.join(names)}."
        return description
