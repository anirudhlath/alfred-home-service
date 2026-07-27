"""Tests for the data-driven risk map and reflex tool config."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from app.capability_generator import REFLEX_DOMAINS
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


def test_reflex_domains_disjoint_from_risk_map_critical_and_elevated() -> None:
    """Config-drift safety invariant (contract C9).

    CapabilityGenerator hardcodes risk="benign" for every tool over a
    REFLEX_DOMAINS domain (app/capability_generator.py `_reflex_specs`) — it
    never consults RiskMap for those domains at all. That is safe ONLY as
    long as no REFLEX_DOMAINS member also appears in the risk map's critical
    or elevated domain lists. RiskMap exposes no public accessor for its
    domain sets (by design — `risk_for`/`domain_tool_risk` are the only
    public surface), so this test loads the ACTUAL shipped
    config/risk_map.yaml directly to guard against that drift: if someone
    later adds e.g. "switch" or "media_player" under critical/elevated
    domains, a dangerous action would silently ship tagged reflex + benign,
    bypassing the elevated/critical confirmation flow entirely. This test
    MUST fail loudly the moment that happens.
    """
    raw: dict[str, Any] = yaml.safe_load((CONFIG_DIR / "risk_map.yaml").read_text()) or {}
    critical_domains = set(raw.get("critical", {}).get("domains") or [])
    elevated_domains = set(raw.get("elevated", {}).get("domains") or [])
    risky_domains = critical_domains | elevated_domains

    overlap = REFLEX_DOMAINS & risky_domains
    assert not overlap, (
        "REFLEX_DOMAINS overlaps risk_map.yaml critical/elevated domains: "
        f"{sorted(overlap)}. CapabilityGenerator._reflex_specs() hardcodes "
        "risk='benign' for every REFLEX_DOMAINS tool WITHOUT consulting "
        "RiskMap, so any domain in this overlap would ship as a reflex-tier "
        "benign tool despite being marked critical/elevated in "
        "config/risk_map.yaml — a dangerous action bypassing confirmation. "
        "Either remove the domain from risk_map.yaml's critical/elevated "
        "lists, or remove it from REFLEX_DOMAINS in app/capability_generator.py."
    )
