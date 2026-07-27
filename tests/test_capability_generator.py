"""Tests for CapabilityGenerator output: tool set, audience/risk tagging, shapes."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.capability_generator import CapabilityGenerator, GeneratedToolSpec
from app.entity_index import EntityIndex
from app.ha_connection import HAEntityState
from app.risk_map import RiskMap, load_reflex_config
from tests.fake_ha import DEFAULT_SERVICES

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


@pytest.fixture
def generator() -> CapabilityGenerator:
    return CapabilityGenerator(
        RiskMap.load(CONFIG_DIR / "risk_map.yaml"),
        load_reflex_config(CONFIG_DIR / "reflex_tools.yaml"),
    )


@pytest.fixture
def specs(generator: CapabilityGenerator, built_index: EntityIndex) -> list[GeneratedToolSpec]:
    return generator.generate(DEFAULT_SERVICES, built_index)


def _by_name(specs: list[GeneratedToolSpec]) -> dict[str, GeneratedToolSpec]:
    return {s.tool_name: s for s in specs}


def test_generated_tool_set_is_exactly_expected(specs: list[GeneratedToolSpec]) -> None:
    assert {s.tool_name for s in specs} == {
        # reflex tier (from config/reflex_tools.yaml ∩ catalog ∩ entities)
        "home.light_turn_on",
        "home.light_turn_off",
        "home.switch_turn_on",
        "home.switch_turn_off",
        "home.media_player_turn_on",
        "home.media_player_turn_off",
        "home.media_player_media_play",
        "home.media_player_media_pause",
        "home.media_player_volume_set",
        "home.scene_turn_on",
        # conscious tier (remaining domains with entities)
        "home.climate_set_temperature",
        "home.climate_set_hvac_mode",
        "home.lock_lock",
        "home.lock_unlock",
        "home.cover_open_cover",
        "home.cover_close_cover",
        # escape hatch
        "home.call_service",
    }
    # light.toggle exists in the catalog but is not in reflex_tools.yaml → absent
    # homeassistant.restart has no entities → absent


def test_audience_and_risk_tagging(specs: list[GeneratedToolSpec]) -> None:
    by_name = _by_name(specs)
    for name in (
        "home.light_turn_on",
        "home.switch_turn_off",
        "home.media_player_volume_set",
        "home.scene_turn_on",
    ):
        assert by_name[name].audience == "reflex"
        assert by_name[name].risk == "benign"
    assert by_name["home.climate_set_temperature"].audience == "conscious"
    assert by_name["home.climate_set_temperature"].risk == "elevated"
    assert by_name["home.lock_unlock"].risk == "critical"
    # garage-door cover in the fixture elevates ALL cover tools to critical
    assert by_name["home.cover_close_cover"].risk == "critical"
    assert by_name["home.call_service"].audience == "conscious"
    assert by_name["home.call_service"].risk == "critical"


def test_reflex_tool_fields_are_compact(specs: list[GeneratedToolSpec]) -> None:
    light_on = _by_name(specs)["home.light_turn_on"]
    assert [f.name for f in light_on.fields] == ["brightness_pct"]  # color_name excluded
    assert light_on.targeted is True
    assert light_on.method_name == "light_turn_on"
    assert light_on.description == "Turn on one or more lights."


def test_conscious_tool_fields_from_catalog(specs: list[GeneratedToolSpec]) -> None:
    set_temp = _by_name(specs)["home.climate_set_temperature"]
    assert [f.name for f in set_temp.fields] == ["temperature"]
    field = set_temp.fields[0]
    assert field.type == "float"  # number selector
    assert "Target temperature." in field.description


def test_build_tool_meta_injects_live_values(
    generator: CapabilityGenerator,
    specs: list[GeneratedToolSpec],
    built_index: EntityIndex,
) -> None:
    light_on = _by_name(specs)["home.light_turn_on"]
    meta = generator.build_tool_meta(light_on, built_index)
    assert meta.name == "home.light_turn_on"
    assert meta.audience == "reflex"
    assert meta.risk == "benign"
    target_desc = meta.parameters["target"].description
    assert "Available areas: Bedroom, Living Room." in target_desc
    assert "Bedroom Lamp" in target_desc and "Closet Light" in target_desc
    assert meta.parameters["brightness_pct"].type == "float"
    assert "Example: 50." in meta.parameters["brightness_pct"].description


def test_call_service_meta_shape(
    generator: CapabilityGenerator,
    specs: list[GeneratedToolSpec],
    built_index: EntityIndex,
) -> None:
    escape = _by_name(specs)["home.call_service"]
    meta = generator.build_tool_meta(escape, built_index)
    assert set(meta.parameters) == {"domain", "service", "entity_id", "data"}
    assert meta.parameters["data"].type == "dict"


def test_untargeted_service_has_no_target_param(generator: CapabilityGenerator) -> None:
    catalog: dict[str, Any] = {
        "vacuum": {
            "start": {"name": "Start", "description": "Start cleaning.", "fields": {}}
        }  # no "target" key
    }
    index = EntityIndex()
    index.rebuild(
        entity_registry=[],
        device_registry=[],
        area_registry=[],
        states={
            "vacuum.robo": HAEntityState(entity_id="vacuum.robo", state="docked", attributes={})
        },
    )
    specs = generator.generate(catalog, index)
    by_name = _by_name(specs)
    assert by_name["home.vacuum_start"].targeted is False
    meta = generator.build_tool_meta(by_name["home.vacuum_start"], index)
    assert "target" not in meta.parameters
    assert by_name["home.vacuum_start"].risk == "elevated"  # vacuum is elevated in risk map
