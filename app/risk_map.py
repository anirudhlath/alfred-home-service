"""Risk mapping and reflex-tier tool config — data, not code (contract C9)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml

from app.entity_index import EntityIndex

Risk = Literal["benign", "elevated", "critical"]

_RISK_ORDER: dict[Risk, int] = {"benign": 0, "elevated": 1, "critical": 2}


class RiskMap:
    """Domain/device_class → risk tier, loaded from config/risk_map.yaml."""

    def __init__(
        self,
        critical_domains: set[str],
        critical_cover_device_classes: set[str],
        elevated_domains: set[str],
    ) -> None:
        self._critical_domains = critical_domains
        self._critical_cover_device_classes = critical_cover_device_classes
        self._elevated_domains = elevated_domains

    @classmethod
    def load(cls, path: Path) -> RiskMap:
        raw: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
        critical = raw.get("critical") or {}
        elevated = raw.get("elevated") or {}
        return cls(
            critical_domains={str(d) for d in critical.get("domains") or []},
            critical_cover_device_classes={
                str(d) for d in critical.get("cover_device_classes") or []
            },
            elevated_domains={str(d) for d in elevated.get("domains") or []},
        )

    def risk_for(self, domain: str, device_class: str | None) -> Risk:
        if domain in self._critical_domains:
            return "critical"
        if domain == "cover" and device_class in self._critical_cover_device_classes:
            return "critical"
        if domain in self._elevated_domains:
            return "elevated"
        return "benign"

    def domain_tool_risk(self, domain: str, index: EntityIndex) -> Risk:
        """Max risk across the domain's discovered entities.

        A garage-door cover makes ALL cover tools critical (safety-first v1 —
        relaxing later is easy, the reverse is not).
        """
        risks: list[Risk] = [
            self.risk_for(domain, e.device_class) for e in index.entities_for_domain(domain)
        ]
        if not risks:
            risks = [self.risk_for(domain, None)]
        return max(risks, key=lambda r: _RISK_ORDER[r])


def load_reflex_config(path: Path) -> dict[str, dict[str, list[str]]]:
    """domain → service → extra optional field names for the compact reflex tier."""
    raw: dict[str, Any] = yaml.safe_load(path.read_text()) or {}
    return {
        str(domain): {
            str(service): [str(f) for f in fields or []]
            for service, fields in (services or {}).items()
        }
        for domain, services in raw.items()
    }
