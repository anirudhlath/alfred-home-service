"""State forwarder — every HA state_changed → MQTT home/state_changed (contract C11).

The alfred bus bridge maps MQTT `home/state_changed` → Redis stream
`alfred:home:state_changed` (alfred bus/bridge.py mqtt_topic_to_stream_key),
feeding Reflex, triggers, and context. This retires the HA-side MQTT
automation. Forward EVERYTHING — Tier-1 visibility; SLM gating happens in core.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from typing import Any

import aiomqtt
from alfred_sdk.events import StateChangedEvent
from loguru import logger

MQTT_TOPIC = "home/state_changed"


class StateForwarder:
    """Bounded-queue MQTT publisher for state_changed events."""

    def __init__(
        self, host: str | None = None, port: int | None = None, *, queue_size: int = 1000
    ) -> None:
        self._host = host if host is not None else os.getenv("MQTT_HOST", "localhost")
        self._port = port if port is not None else int(os.getenv("MQTT_PORT", "1883"))
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_size)
        self._task: asyncio.Task[None] | None = None
        self._initial_backoff = 1.0
        self._max_backoff = 30.0

    @staticmethod
    def build_event(
        entity_id: str,
        old_state: str | None,
        new_state: str | None,
        attributes: dict[str, Any],
    ) -> StateChangedEvent | None:
        """Map an HA state_changed to the bus schema. None → skip (entity removed)."""
        if new_state is None:
            return None
        return StateChangedEvent(
            domain="home",
            source="home-service",
            entity_id=entity_id,
            old_state=old_state,
            new_state=new_state,
            attributes=attributes,
        )

    async def on_state_changed(
        self,
        entity_id: str,
        old_state: str | None,
        new_state: str | None,
        attributes: dict[str, Any],
    ) -> None:
        """HAConnection state listener — forward everything, no source filtering."""
        event = self.build_event(entity_id, old_state, new_state, attributes)
        if event is None:
            return
        try:
            self._queue.put_nowait(event.model_dump_json())
        except asyncio.QueueFull:
            logger.warning("MQTT forward queue full — dropping state_changed for {}", entity_id)

    def pending_count(self) -> int:
        return self._queue.qsize()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._publish_loop(), name="state-forwarder")

    async def stop(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
        self._task = None

    async def _publish_loop(self) -> None:
        backoff = self._initial_backoff
        inflight: str | None = None  # retained across reconnects — not lost on MqttError
        while True:
            try:
                async with aiomqtt.Client(self._host, self._port) as client:
                    backoff = self._initial_backoff
                    while True:
                        if inflight is None:
                            inflight = await self._queue.get()
                        await client.publish(MQTT_TOPIC, inflight)
                        inflight = None
            except aiomqtt.MqttError as exc:
                logger.warning("MQTT unavailable ({}) — retrying in {:.1f}s", exc, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self._max_backoff)
