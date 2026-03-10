"""Thin wrapper around Home Assistant REST API."""

from __future__ import annotations

import httpx
from typing import Any


class HomeAssistantClient:
    """Async client for Home Assistant's REST API."""

    def __init__(self, host: str, token: str):
        self.host = host.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

    async def get_states(self) -> list[dict[str, Any]]:
        """Get all entity states."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{self.host}/api/states", headers=self.headers)
            resp.raise_for_status()
            return resp.json()

    async def call_service(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Call an HA service (e.g., light/turn_on)."""
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.host}/api/services/{domain}/{service}",
                headers=self.headers,
                json=data,
            )
            resp.raise_for_status()
            return resp.json()

    async def get_entity_state(self, entity_id: str) -> dict[str, Any]:
        """Get state of a single entity."""
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{self.host}/api/states/{entity_id}", headers=self.headers
            )
            resp.raise_for_status()
            return resp.json()
