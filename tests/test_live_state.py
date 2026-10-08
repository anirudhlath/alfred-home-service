"""The one entry builder: replace() and update() classify every entity the same way."""

from __future__ import annotations

from alfred_sdk.context import ContextEntry

from app.entity_index import EntityIndex
from app.ha_connection import HAEntityState
from app.live_state import build_entry, build_snapshot
from tests.fake_ha import DEFAULT_SERVICES

WEATHER = HAEntityState(
    entity_id="weather.home",
    state="sunny",
    attributes={"friendly_name": "Home", "forecast": [{"big": "blob"}]},
)


def test_entry_keeps_only_allowlisted_attributes() -> None:
    domain, kind, entry = build_entry(WEATHER, DEFAULT_SERVICES, {})
    assert (domain, kind) == ("weather", "sensor")  # no weather services in the catalog
    assert entry == ContextEntry(
        entity_id="weather.home", state="sunny", attributes={"friendly_name": "Home"}
    )


def test_a_domain_with_services_is_controllable() -> None:
    lamp = HAEntityState(
        entity_id="light.bedroom_lamp",
        state="on",
        attributes={"brightness": 128, "effect_list": ["colorloop", "random"]},
    )
    assert build_entry(lamp, DEFAULT_SERVICES, {}) == (
        "light",
        "controllable",
        ContextEntry(entity_id="light.bedroom_lamp", state="on", attributes={"brightness": 128}),
    )


def test_snapshot_and_entry_classify_alike(default_states_map: dict[str, HAEntityState]) -> None:
    states = {**default_states_map, WEATHER.entity_id: WEATHER}

    snapshot = build_snapshot(states, DEFAULT_SERVICES, {})

    for state in states.values():
        domain, kind, entry = build_entry(state, DEFAULT_SERVICES, {})
        bucket = snapshot.controllable if kind == "controllable" else snapshot.sensors
        assert entry in bucket[domain]
    assert "light" in snapshot.controllable
    assert "lock" in snapshot.controllable
    assert "sensor" in snapshot.sensors
    assert "binary_sensor" in snapshot.sensors
    assert "forecast" not in snapshot.sensors["weather"][0].attributes


def test_snapshot_entities_are_sorted(default_states_map: dict[str, HAEntityState]) -> None:
    snapshot = build_snapshot(default_states_map, DEFAULT_SERVICES, {})
    ids = [e.entity_id for e in snapshot.controllable["light"]]
    assert ids == sorted(ids)


def test_an_entity_in_a_room_carries_its_area() -> None:
    lamp = HAEntityState(
        entity_id="light.bedroom_lamp", state="on", attributes={"friendly_name": "Bedroom Lamp"}
    )
    _, _, entry = build_entry(lamp, DEFAULT_SERVICES, {"light.bedroom_lamp": "Bedroom"})
    assert entry.attributes == {"friendly_name": "Bedroom Lamp", "area": "Bedroom"}


def test_an_entity_in_no_room_carries_no_area() -> None:
    _, _, entry = build_entry(WEATHER, DEFAULT_SERVICES, {"light.bedroom_lamp": "Bedroom"})
    assert "area" not in entry.attributes


def _entries(snapshot: object) -> dict[str, ContextEntry]:
    return {
        entry.entity_id: entry
        for groups in (snapshot.controllable, snapshot.sensors)  # type: ignore[attr-defined]
        for entries in groups.values()
        for entry in entries
    }


def test_a_snapshot_carries_entity_and_device_rooms(
    default_states_map: dict[str, HAEntityState], built_index: EntityIndex
) -> None:
    entries = _entries(build_snapshot(default_states_map, DEFAULT_SERVICES, built_index.areas()))

    assert entries["light.bedroom_lamp"].attributes["area"] == "Bedroom"  # its own area
    assert entries["media_player.tv"].attributes["area"] == "Living Room"  # its device's
    assert "area" not in entries["scene.movie_night"].attributes  # none
