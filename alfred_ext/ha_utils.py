"""Shared HA utilities for home-service features."""

from __future__ import annotations

import logging
from typing import Any

from alfred_sdk.context import ContextEntry, ContextSnapshot

logger = logging.getLogger(__name__)


def to_entity_id(domain: str, name: str) -> str:
    """Normalise a human-readable name to a valid HA entity_id."""
    return f"{domain}.{name.replace(' ', '_').lower()}"


async def context_for_domain(ha: Any, domain: str) -> ContextSnapshot:
    """Fetch HA states and return a ContextSnapshot filtered to a single domain."""
    try:
        states = await ha.get_states()
        entries = [
            ContextEntry(
                entity_id=s["entity_id"],
                state=s.get("state", "unknown"),
                attributes=s.get("attributes", {}),
            )
            for s in states
            if s["entity_id"].startswith(f"{domain}.")
        ]
        return ContextSnapshot(controllable={domain: entries})
    except Exception as e:
        logger.warning("Could not query HA for %s context: %s", domain, e)
        return ContextSnapshot()
