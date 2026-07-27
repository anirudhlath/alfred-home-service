"""Tests for EntityIndex build + resolution."""

from __future__ import annotations

import pytest

from app.entity_index import EntityIndex
from app.ha_connection import HAEntityState
from tests.fake_ha import (
    DEFAULT_AREA_REGISTRY,
    DEFAULT_DEVICE_REGISTRY,
    DEFAULT_ENTITY_REGISTRY,
)


def test_counts_and_disabled_exclusion(built_index: EntityIndex) -> None:
    # 10 enabled registry entries ∪ 10 states = 11 unique
    # (light.closet is registry-only, sensor.outdoor_temp is states-only)
    assert built_index.entity_count() == 11
    assert built_index.area_count() == 3
    assert built_index.get("light.disabled_lamp") is None
    assert built_index.get("sensor.outdoor_temp") is not None
    assert built_index.get("light.closet") is not None


def test_domains_and_area_names(built_index: EntityIndex) -> None:
    assert built_index.domains() == {
        "light",
        "switch",
        "media_player",
        "scene",
        "climate",
        "lock",
        "cover",
        "binary_sensor",
        "sensor",
    }
    assert built_index.area_names() == ["Bedroom", "Garage", "Living Room"]


def test_friendly_name_precedence(built_index: EntityIndex) -> None:
    # state attribute wins
    assert built_index.get("light.bedroom_lamp").friendly_name == "Bedroom Lamp"  # type: ignore[union-attr]
    # registry original_name for a registry-only entity with no state
    assert built_index.get("light.closet").friendly_name == "Closet Light"  # type: ignore[union-attr]
    # entity_id fallback for a states-only entity without friendly_name
    index = EntityIndex()
    index.rebuild(
        entity_registry=[],
        device_registry=[],
        area_registry=[],
        states={
            "sensor.raw_thing": HAEntityState(
                entity_id="sensor.raw_thing", state="1", attributes={}
            )
        },
    )
    assert index.get("sensor.raw_thing").friendly_name == "raw thing"  # type: ignore[union-attr]


def test_area_via_device_and_device_name(built_index: EntityIndex) -> None:
    tv = built_index.get("media_player.tv")
    assert tv is not None
    assert tv.area == "Living Room"  # from device dev-tv
    assert tv.device == "Living Room TV"
    garage = built_index.get("cover.garage_door")
    assert garage is not None
    assert garage.area == "Garage"
    assert garage.device_class == "garage"  # from state attributes


def test_resolve_by_area_name(built_index: EntityIndex) -> None:
    assert built_index.resolve("light", "Living Room") == ["light.living_room_lamp"]
    assert built_index.resolve("light", "living room") == ["light.living_room_lamp"]


def test_resolve_by_friendly_name_and_entity_id(built_index: EntityIndex) -> None:
    assert built_index.resolve("light", "Bedroom Lamp") == ["light.bedroom_lamp"]
    assert built_index.resolve("scene", "Movie Night") == ["scene.movie_night"]
    assert built_index.resolve("light", "light.closet") == ["light.closet"]


def test_resolve_unknown_raises_with_options(built_index: EntityIndex) -> None:
    with pytest.raises(LookupError) as exc_info:
        built_index.resolve("light", "attic")
    message = str(exc_info.value)
    assert "attic" in message
    assert "Bedroom" in message  # available areas listed
    assert "Closet Light" in message  # available entities listed


def test_resolve_rejects_wrong_domain_entity_id(built_index: EntityIndex) -> None:
    with pytest.raises(LookupError):
        built_index.resolve("light", "switch.coffee_maker")


def test_rebuild_replaces_previous_data(built_index: EntityIndex) -> None:
    renamed_areas = [
        {**a, "name": "Lounge"} if a["area_id"] == "living_room" else a
        for a in DEFAULT_AREA_REGISTRY
    ]
    built_index.rebuild(
        entity_registry=DEFAULT_ENTITY_REGISTRY,
        device_registry=DEFAULT_DEVICE_REGISTRY,
        area_registry=renamed_areas,
        states={},
    )
    assert built_index.resolve("light", "Lounge") == ["light.living_room_lamp"]
    with pytest.raises(LookupError):
        built_index.resolve("light", "Living Room")
