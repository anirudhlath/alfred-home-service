"""Thin wrapper around Home Assistant REST API."""

from __future__ import annotations

from typing import Any

import httpx


class HomeAssistantClient:
    """Async client for Home Assistant's REST API."""

    def __init__(self, host: str, token: str) -> None:
        self.host = host.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """Get or create the shared httpx client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def get_states(self) -> list[dict[str, Any]]:
        """Get all entity states."""
        client = self._get_client()
        resp = await client.get(f"{self.host}/api/states", headers=self.headers)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def call_service(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Call an HA service (e.g., light/turn_on)."""
        client = self._get_client()
        resp = await client.post(
            f"{self.host}/api/services/{domain}/{service}",
            headers=self.headers,
            json=data,
        )
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]

    async def get_entity_state(self, entity_id: str) -> dict[str, Any]:
        """Get state of a single entity."""
        client = self._get_client()
        resp = await client.get(
            f"{self.host}/api/states/{entity_id}", headers=self.headers
        )
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]
