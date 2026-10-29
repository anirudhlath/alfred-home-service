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
        "home.script_turn_on",  # elevated: Reflex may only ask before running it
        # conscious tier (remaining domains with entities, and the rest of an
        # elevated reflex domain)
        "home.script_turn_off",
        "home.script_toggle",
        "home.script_reload",
        "home.script_movie_night",
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
    assert by_name["home.script_turn_on"].audience == "reflex"
    assert by_name["home.script_turn_on"].risk == "elevated"
    assert by_name["home.climate_set_temperature"].audience == "conscious"
    assert by_name["home.climate_set_temperature"].risk == "elevated"
    assert by_name["home.lock_unlock"].risk == "critical"
    # garage-door cover in the fixture elevates ALL cover tools to critical
    assert by_name["home.cover_close_cover"].risk == "critical"
    assert by_name["home.call_service"].audience == "conscious"
    assert by_name["home.call_service"].risk == "critical"


def test_script_turn_on_is_a_reflex_tool_that_is_still_elevated(
    generator: CapabilityGenerator, specs: list[GeneratedToolSpec], built_index: EntityIndex
) -> None:
    script_on = _by_name(specs)["home.script_turn_on"]
    assert script_on.audience == "reflex"
    assert script_on.risk == "elevated"
    assert script_on.fields == ()  # the catalog's `variables` field is not offered to Reflex
    assert script_on.targeted is True
    assert _by_name(specs)["home.light_turn_on"].risk == "benign"
    meta = generator.build_tool_meta(script_on, built_index)
    assert (meta.audience, meta.risk) == ("reflex", "elevated")
    assert set(meta.parameters) == {"target"}
    assert "Movie Night Script" in meta.parameters["target"].description


def test_rest_of_an_elevated_reflex_domain_stays_conscious(
    generator: CapabilityGenerator, specs: list[GeneratedToolSpec], built_index: EntityIndex
) -> None:
    by_name = _by_name(specs)
    for name in (
        "home.script_turn_off",
        "home.script_toggle",
        "home.script_reload",
        "home.script_movie_night",
    ):
        assert by_name[name].audience == "conscious"
        assert by_name[name].risk == "elevated"
    # a conscious tool keeps its targeted logic
    assert by_name["home.script_turn_off"].targeted is True
    assert by_name["home.script_reload"].targeted is False
    assert by_name["home.script_movie_night"].targeted is False
    # a service configured as a reflex tool is generated once, not again as a conscious one
    names = [s.tool_name for s in specs]
    assert len(names) == len(set(names))
    # a benign reflex domain keeps only its compact tier
    assert "home.light_toggle" not in names
    assert "home.scene_turn_off" not in names


def test_reflex_tier_is_not_specific_to_any_domain(
    built_index: EntityIndex, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reflex domain's risk comes from the risk map: no domain is named in the rule."""
    monkeypatch.setattr("app.capability_generator.REFLEX_DOMAINS", frozenset({"climate"}))
    generator = CapabilityGenerator(
        RiskMap.load(CONFIG_DIR / "risk_map.yaml"), {"climate": {"set_temperature": []}}
    )
    by_name = _by_name(generator.generate(DEFAULT_SERVICES, built_index))
    set_temp = by_name["home.climate_set_temperature"]
    assert (set_temp.audience, set_temp.risk, set_temp.fields) == ("reflex", "elevated", ())
    set_mode = by_name["home.climate_set_hvac_mode"]
    assert (set_mode.audience, set_mode.risk) == ("conscious", "elevated")
    assert [f.name for f in set_mode.fields] == ["hvac_mode"]


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


_TEMPERATURE: dict[str, Any] = {
    "name": "Temperature",
    "description": "Target temperature.",
    "example": 21,
    "required": True,
    "selector": {"number": {"min": 7, "max": 35}},
}


def _set_temperature(
    generator: CapabilityGenerator, index: EntityIndex, fields: dict[str, Any]
) -> GeneratedToolSpec:
    """The climate.set_temperature tool generated from a catalog with these fields."""
    catalog: dict[str, Any] = {
        "climate": {
            "set_temperature": {
                "name": "Set target temperature",
                "description": "Set the target temperature.",
                "fields": fields,
                "target": {"entity": [{}]},
            }
        }
    }
    return _by_name(generator.generate(catalog, index))["home.climate_set_temperature"]


def test_section_fields_become_top_level_params(
    generator: CapabilityGenerator, built_index: EntityIndex
) -> None:
    spec = _set_temperature(
        generator,
        built_index,
        {
            "temperature": _TEMPERATURE,
            "advanced_fields": {
                "collapsed": True,
                "fields": {
                    "target_temp_high": {"description": "Upper bound."},
                    "target_temp_low": {"description": "Lower bound."},
                },
            },
        },
    )
    assert [f.name for f in spec.fields] == ["target_temp_high", "target_temp_low", "temperature"]
    meta = generator.build_tool_meta(spec, built_index)
    assert set(meta.parameters) == {"target", "target_temp_high", "target_temp_low", "temperature"}


def test_nested_field_carries_over_like_a_top_level_one(
    generator: CapabilityGenerator, built_index: EntityIndex
) -> None:
    top_level = _set_temperature(generator, built_index, {"temperature": _TEMPERATURE})
    nested = _set_temperature(
        generator,
        built_index,
        {"advanced_fields": {"collapsed": True, "fields": {"temperature": _TEMPERATURE}}},
    )
    assert nested.fields == top_level.fields
    field = nested.fields[0]
    assert field.type == "float"  # number selector
    assert "Target temperature." in field.description
    assert "Example: 21." in field.description


@pytest.mark.parametrize("section_first", [True, False])
def test_top_level_field_wins_a_name_collision(
    generator: CapabilityGenerator, built_index: EntityIndex, section_first: bool
) -> None:
    section = {
        "collapsed": True,
        "fields": {"temperature": {"description": "Shadowed.", "selector": {"text": None}}},
    }
    entries = [("advanced_fields", section), ("temperature", _TEMPERATURE)]
    fields = dict(entries if section_first else reversed(entries))
    spec = _set_temperature(generator, built_index, fields)
    top_level_only = _set_temperature(generator, built_index, {"temperature": _TEMPERATURE})
    assert [f.name for f in spec.fields] == ["temperature"]
    assert spec.fields == top_level_only.fields


def test_first_section_wins_a_name_collision_between_sections(
    generator: CapabilityGenerator, built_index: EntityIndex
) -> None:
    spec = _set_temperature(
        generator,
        built_index,
        {
            "basic_fields": {"fields": {"temperature": {"description": "From the first."}}},
            "advanced_fields": {"fields": {"temperature": {"description": "From the second."}}},
        },
    )
    assert [f.name for f in spec.fields] == ["temperature"]
    assert spec.fields[0].description == "From the first."


@pytest.mark.parametrize(
    "section", [{"collapsed": True}, {"collapsed": True, "fields": None}], ids=["absent", "null"]
)
def test_section_without_a_fields_dict_adds_no_params(
    generator: CapabilityGenerator, built_index: EntityIndex, section: dict[str, Any]
) -> None:
    spec = _set_temperature(
        generator, built_index, {"temperature": _TEMPERATURE, "advanced_fields": section}
    )
    assert [f.name for f in spec.fields] == ["temperature"]


def test_reflex_tool_can_name_a_field_inside_a_section(built_index: EntityIndex) -> None:
    generator = CapabilityGenerator(
        RiskMap.load(CONFIG_DIR / "risk_map.yaml"),
        {"light": {"turn_on": ["brightness_pct", "flash", "advanced_fields"]}},
    )
    light_on = _by_name(generator.generate(DEFAULT_SERVICES, built_index))["home.light_turn_on"]
    # flash sits in light.turn_on's advanced_fields section; the section itself is no field
    assert [f.name for f in light_on.fields] == ["brightness_pct", "flash"]


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
    index = _vacuum_index()
    specs = generator.generate(catalog, index)
    by_name = _by_name(specs)
    assert by_name["home.vacuum_start"].targeted is False
    meta = generator.build_tool_meta(by_name["home.vacuum_start"], index)
    assert "target" not in meta.parameters
    assert by_name["home.vacuum_start"].risk == "elevated"  # vacuum is elevated in risk map


def _vacuum_index() -> EntityIndex:
    index = EntityIndex()
    index.rebuild(
        entity_registry=[],
        device_registry=[],
        area_registry=[],
        states={
            "vacuum.robo": HAEntityState(entity_id="vacuum.robo", state="docked", attributes={})
        },
    )
    return index


def test_required_fields_and_target_are_required(generator: CapabilityGenerator) -> None:
    catalog: dict[str, Any] = {
        "vacuum": {
            "send_command": {
                "name": "Send command",
                "description": "Send a command.",
                "fields": {
                    "command": {"description": "Command to send.", "required": True},
                    "params": {"description": "Extra parameters.", "selector": {"object": {}}},
                },
                "target": {"entity": {"domain": "vacuum"}},
            }
        }
    }
    index = _vacuum_index()
    spec = _by_name(generator.generate(catalog, index))["home.vacuum_send_command"]
    meta = generator.build_tool_meta(spec, index)
    assert meta.input_schema["required"] == ["target", "command"]
    assert meta.parameters["command"].required is True
    assert meta.parameters["params"].required is False
    assert "(required)" not in meta.parameters["command"].description


def test_call_service_requires_domain_and_service(
    generator: CapabilityGenerator,
    specs: list[GeneratedToolSpec],
    built_index: EntityIndex,
) -> None:
    meta = generator.build_tool_meta(_by_name(specs)["home.call_service"], built_index)
    assert meta.input_schema["required"] == ["domain", "service"]
