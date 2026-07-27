"""Thin REST fallback for Home Assistant /api/states snapshots.

The WebSocket HAConnection (app/ha_connection.py) is the primary interface.
This client is retained ONLY as a manual/debug fallback for state snapshots
per the design spec — it is not wired into the runtime.
"""

from __future__ import annotations

from typing import Any

import httpx


class HomeAssistantClient:
    """Async client for Home Assistant's REST API (states snapshot only)."""

    def __init__(self, host: str, token: str) -> None:
        self.host = host.rstrip("/")
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        """Get or create the shared long-lived httpx client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def get_states(self) -> list[dict[str, Any]]:
        """Get all entity states via REST."""
        client = self._get_client()
        resp = await client.get(f"{self.host}/api/states", headers=self.headers)
        resp.raise_for_status()
        return resp.json()  # type: ignore[no-any-return]
