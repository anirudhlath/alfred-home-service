"""Tests for StateForwarder — mapping, bounded queue, MQTT publish loop."""

from __future__ import annotations

import aiomqtt
import pytest
from alfred_sdk.events import StateChangedEvent

from app.state_forwarder import MQTT_TOPIC, StateForwarder
from tests.fake_ha import eventually


def test_build_event_maps_contract_c11_fields() -> None:
    event = StateForwarder.build_event(
        "light.bedroom_lamp", "on", "off", {"friendly_name": "Bedroom Lamp"}
    )
    assert event is not None
    assert event.event_type == "state_changed"
    assert event.domain == "home"
    assert event.source == "home-service"
    assert event.entity_id == "light.bedroom_lamp"
    assert event.old_state == "on"
    assert event.new_state == "off"
    assert event.attributes == {"friendly_name": "Bedroom Lamp"}


def test_build_event_attribute_only_update_forwards_equal_states() -> None:
    event = StateForwarder.build_event("light.a", "on", "on", {"brightness": 10})
    assert event is not None
    assert event.old_state == event.new_state == "on"


def test_build_event_entity_removed_skips() -> None:
    assert StateForwarder.build_event("light.a", "on", None, {}) is None


def test_build_event_new_entity_has_null_old_state() -> None:
    event = StateForwarder.build_event("light.new", None, "off", {})
    assert event is not None
    assert event.old_state is None


async def test_queue_full_drops_with_warning() -> None:
    forwarder = StateForwarder(host="mqtt-unused", port=1883, queue_size=1)
    await forwarder.on_state_changed("light.a", "on", "off", {})
    await forwarder.on_state_changed("light.b", "on", "off", {})  # dropped
    assert forwarder.pending_count() == 1


class _FakeMqttClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, str]] = []

    async def __aenter__(self) -> _FakeMqttClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def publish(self, topic: str, payload: str) -> None:
        self.published.append((topic, str(payload)))


class _FlakyThenGoodFactory:
    """First connection attempt raises MqttError; second works."""

    def __init__(self, good: _FakeMqttClient) -> None:
        self._good = good
        self.attempts = 0

    def __call__(self, host: str, port: int) -> _FakeMqttClient:
        self.attempts += 1
        if self.attempts == 1:
            raise aiomqtt.MqttError("broker down")
        return self._good


async def test_publish_loop_publishes_event_json(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeMqttClient()
    monkeypatch.setattr("app.state_forwarder.aiomqtt.Client", lambda host, port: fake)
    forwarder = StateForwarder(host="broker", port=1883)
    await forwarder.on_state_changed("light.a", "on", "off", {"friendly_name": "A"})
    await forwarder.start()
    await eventually(lambda: len(fake.published) == 1)
    topic, payload = fake.published[0]
    assert topic == MQTT_TOPIC
    event = StateChangedEvent.model_validate_json(payload)
    assert event.entity_id == "light.a"
    assert event.new_state == "off"
    await forwarder.stop()


async def test_publish_loop_retries_after_mqtt_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeMqttClient()
    factory = _FlakyThenGoodFactory(fake)
    monkeypatch.setattr("app.state_forwarder.aiomqtt.Client", factory)
    forwarder = StateForwarder(host="broker", port=1883)
    # shrink backoff for the test
    forwarder._initial_backoff = 0.02
    await forwarder.on_state_changed("light.a", "on", "off", {})
    await forwarder.start()
    await eventually(lambda: len(fake.published) == 1)
    assert factory.attempts == 2
    await forwarder.stop()


def test_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MQTT_HOST", "broker.local")
    monkeypatch.setenv("MQTT_PORT", "2883")
    forwarder = StateForwarder()
    assert forwarder._host == "broker.local"
    assert forwarder._port == 2883
