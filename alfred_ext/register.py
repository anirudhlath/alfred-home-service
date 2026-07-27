"""Alfred SDK client construction for home-service (contract C1).

The SDK is the ONLY coupling to Alfred (Pillar 2). No HA connection and no
feature discovery happen at import time — capabilities are generated at
runtime after the first successful HA connection (see app/server.py).
"""

from __future__ import annotations

import os

from alfred_sdk import AlfredClient
from alfred_sdk.feature import CredentialField, CredentialSchema


def build_credentials_schema() -> CredentialSchema:
    """Self-describing credential needs — rendered by Alfred's settings UI."""
    return CredentialSchema(
        fields={
            "url": CredentialField(
                label="Home Assistant URL",
                field_type="url",
                required=True,
                placeholder="http://192.168.50.159:8123",
                default="http://homeassistant.local:8123",
                help_text="Base URL of the Home Assistant instance.",
            ),
            "token": CredentialField(
                label="Long-lived access token",
                field_type="password",
                required=True,
                help_text="HA profile → Security → Long-lived access tokens.",
            ),
        }
    )


def build_client() -> AlfredClient:
    """Build the AlfredClient with credential metadata (contract C1)."""
    host = os.getenv("SERVICE_HOST", "localhost")
    return AlfredClient(
        service_name="home-service",
        service_endpoint=f"http://{host}:8000/mcp",
        credentials_schema=build_credentials_schema(),
        credentials_endpoint=f"http://{host}:8000/credentials",
    )
