"""Tests for HomeCapabilitiesFeature: handler binding, dispatch, context."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from alfred_sdk import AlfredClient

from app.capability_generator import CapabilityGenerator
from app.entity_index import EntityIndex
from app.ha_connection import HAEntityState
from app.home_feature import HomeCapabilitiesContext, HomeCapabilitiesFeature
from app.risk_map import RiskMap, load_reflex_config
from tests.fake_ha import DEFAULT_SERVICES

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


class StubHA:
    """Implements HAConnectionLike without a socket."""

    def __init__(self, states: dict[str, HAEntityState], services_catalog: dict[str, Any]) -> None:
        self.states = states
        self.services_catalog = services_catalog
        self.calls: list[tuple[str, str, dict[str, Any] | None, list[str] | None]] = []

    async def call_service(
        self,
        domain: str,
        service: str,
        service_data: dict[str, Any] | None = None,
        entity_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((domain, service, service_data, entity_ids))
        return {"context": {"id": "stub"}}


@pytest.fixture
def feature_env(
    built_index: EntityIndex, default_states_map: dict[str, HAEntityState]
) -> tuple[HomeCapabilitiesFeature, StubHA, HomeCapabilitiesContext]:
    generator = CapabilityGenerator(
        RiskMap.load(CONFIG_DIR / "risk_map.yaml"),
        load_reflex_config(CONFIG_DIR / "reflex_tools.yaml"),
    )
    specs = generator.generate(DEFAULT_SERVICES, built_index)
    stub = StubHA(default_states_map, DEFAULT_SERVICES)
    ctx = HomeCapabilitiesContext(conn=stub, index=built_index, generator=generator, specs=specs)
    return HomeCapabilitiesFeature(ctx), stub, ctx


def test_every_tool_has_a_bound_handler(
    feature_env: tuple[HomeCapabilitiesFeature, StubHA, HomeCapabilitiesContext],
) -> None:
    feature, _stub, ctx = feature_env
    for spec in ctx.specs:
        handler = getattr(feature, spec.method_name, None)
        assert callable(handler), f"missing handler for {spec.tool_name}"
    # get_tools names match the dispatch-binding convention (last segment = attr)
    for meta in feature.get_tools():
        assert hasattr(feature, meta.name.split(".")[-1])


async def test_dispatch_via_alfred_client_resolves_area(
    feature_env: tuple[HomeCapabilitiesFeature, StubHA, HomeCapabilitiesContext],
) -> None:
    _feature, stub, ctx = feature_env
    client = AlfredClient(service_name="home-service", service_endpoint="http://x/mcp")
    client.discover_features_from_classes([HomeCapabilitiesFeature], ctx=ctx)
    result = await client.dispatch(
        "home.light_turn_on", {"target": "Living Room", "brightness_pct": 50}
    )
    assert result["status"] == "ok"
    assert result["entity_ids"] == ["light.living_room_lamp"]
    assert stub.calls == [("light", "turn_on", {"brightness_pct": 50}, ["light.living_room_lamp"])]


async def test_dispatch_friendly_name_and_scene(
    feature_env: tuple[HomeCapabilitiesFeature, StubHA, HomeCapabilitiesContext],
) -> None:
    feature, stub, _ctx = feature_env
    await feature._execute_by_name("home.scene_turn_on", {"target": "movie night"})
    assert stub.calls[-1] == ("scene", "turn_on", {}, ["scene.movie_night"])


async def test_call_service_escape_hatch(
    feature_env: tuple[HomeCapabilitiesFeature, StubHA, HomeCapabilitiesContext],
) -> None:
    feature, stub, _ctx = feature_env
    result = await feature._execute_by_name(
        "home.call_service",
        {"domain": "lock", "service": "unlock", "entity_id": "lock.front_door"},
    )
    assert result["status"] == "ok"
    assert stub.calls == [("lock", "unlock", {}, ["lock.front_door"])]


async def test_missing_target_raises(
    feature_env: tuple[HomeCapabilitiesFeature, StubHA, HomeCapabilitiesContext],
) -> None:
    feature, _stub, _ctx = feature_env
    with pytest.raises(ValueError, match="target"):
        await feature._execute_by_name("home.light_turn_on", {"brightness_pct": 10})


async def test_unknown_target_raises_lookup_error(
    feature_env: tuple[HomeCapabilitiesFeature, StubHA, HomeCapabilitiesContext],
) -> None:
    feature, _stub, _ctx = feature_env
    with pytest.raises(LookupError, match="Areas:"):
        await feature._execute_by_name("home.light_turn_on", {"target": "attic"})


async def test_get_context_buckets_and_filters_attributes(
    feature_env: tuple[HomeCapabilitiesFeature, StubHA, HomeCapabilitiesContext],
) -> None:
    feature, stub, _ctx = feature_env
    stub.states["weather.home"] = HAEntityState(
        entity_id="weather.home",
        state="sunny",
        attributes={"friendly_name": "Home", "forecast": [{"big": "blob"}]},
    )
    snapshot = await feature.get_context()
    # domains with services → controllable; without → sensors
    assert "light" in snapshot.controllable
    assert "lock" in snapshot.controllable
    assert "sensor" in snapshot.sensors
    assert "binary_sensor" in snapshot.sensors
    assert "weather" in snapshot.sensors  # not in DEFAULT_SERVICES catalog
    lights = {e.entity_id: e for e in snapshot.controllable["light"]}
    assert lights["light.bedroom_lamp"].state == "on"
    assert lights["light.bedroom_lamp"].attributes["brightness"] == 128
    weather = snapshot.sensors["weather"][0]
    assert "forecast" not in weather.attributes  # filtered by allowlist
    assert weather.attributes["friendly_name"] == "Home"


def test_to_manifest_propagates_audience_and_risk(
    feature_env: tuple[HomeCapabilitiesFeature, StubHA, HomeCapabilitiesContext],
) -> None:
    feature, _stub, _ctx = feature_env
    manifest = feature.to_manifest()
    assert manifest.name == "home"
    tools = {t.name: t for t in manifest.tools}
    assert tools["home.light_turn_on"].audience == "reflex"
    assert tools["home.light_turn_on"].risk == "benign"
    assert tools["home.lock_unlock"].audience == "conscious"
    assert tools["home.lock_unlock"].risk == "critical"
