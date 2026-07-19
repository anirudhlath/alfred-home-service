"""Tests for the data-driven risk map and reflex tool config."""

from __future__ import annotations

from pathlib import Path

from app.entity_index import EntityIndex
from app.ha_connection import HAEntityState
from app.risk_map import RiskMap, load_reflex_config

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


def test_risk_for_matches_contract_c9() -> None:
    risk_map = RiskMap.load(CONFIG_DIR / "risk_map.yaml")
    assert risk_map.risk_for("lock", None) == "critical"
    assert risk_map.risk_for("alarm_control_panel", None) == "critical"
    assert risk_map.risk_for("cover", "garage") == "critical"
    assert risk_map.risk_for("cover", "garage_door") == "critical"
    assert risk_map.risk_for("cover", "awning") == "elevated"
    assert risk_map.risk_for("cover", None) == "elevated"
    assert risk_map.risk_for("climate", None) == "elevated"
    assert risk_map.risk_for("script", None) == "elevated"
    assert risk_map.risk_for("vacuum", None) == "elevated"
    assert risk_map.risk_for("light", None) == "benign"
    assert risk_map.risk_for("media_player", None) == "benign"


def _index_with_covers(device_classes: list[str | None]) -> EntityIndex:
    states = {
        f"cover.c{i}": HAEntityState(
            entity_id=f"cover.c{i}",
            state="closed",
            attributes={"device_class": dc} if dc else {},
        )
        for i, dc in enumerate(device_classes)
    }
    index = EntityIndex()
    index.rebuild(entity_registry=[], device_registry=[], area_registry=[], states=states)
    return index


def test_domain_tool_risk_takes_max_over_entities() -> None:
    risk_map = RiskMap.load(CONFIG_DIR / "risk_map.yaml")
    assert risk_map.domain_tool_risk("cover", _index_with_covers(["awning"])) == "elevated"
    assert (
        risk_map.domain_tool_risk("cover", _index_with_covers(["awning", "garage"])) == "critical"
    )
    # no entities → base domain risk
    assert risk_map.domain_tool_risk("cover", _index_with_covers([])) == "elevated"
    assert risk_map.domain_tool_risk("light", _index_with_covers([])) == "benign"


def test_load_reflex_config() -> None:
    config = load_reflex_config(CONFIG_DIR / "reflex_tools.yaml")
    assert set(config) == {"light", "switch", "media_player", "scene"}
    assert config["light"]["turn_on"] == ["brightness_pct"]
    assert config["light"]["turn_off"] == []
    assert config["media_player"]["volume_set"] == ["volume_level"]
