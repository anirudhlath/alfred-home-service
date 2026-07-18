"""Alfred integration for home-service.

Optional — this module is only used when alfred-sdk is installed.
The home-service works independently without it.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

import alfred_ext.features as features_pkg
from alfred_sdk import AlfredClient
from app.ha_client import HomeAssistantClient

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

ha = HomeAssistantClient(
    host=os.getenv("HA_HOST", "http://homeassistant.local:8123"),
    token=os.getenv("HA_TOKEN", ""),
)

client = AlfredClient(
    service_name="home-service",
    service_endpoint=f"http://{os.getenv('SERVICE_HOST', 'localhost')}:8000/mcp",
)


class HomeServiceContext:
    """Shared dependencies for all home-service features."""

    def __init__(self, ha: HomeAssistantClient) -> None:
        self.ha = ha


client.discover_features(
    package=features_pkg,
    ctx=HomeServiceContext(ha=ha),
)
