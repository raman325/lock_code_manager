"""Fixtures for Zigbee2MQTT provider E2E tests.

Provides a lightweight MQTT message bus that patches async_subscribe and
async_publish at the HA MQTT component boundary. The Z2M provider code runs
completely unpatched, exercising the full provider -> HA MQTT API path.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.mqtt.models import ReceiveMessage
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import (
    CONF_ENABLED,
    CONF_LOCKS,
    CONF_NAME,
    CONF_PIN,
    CONF_SLOTS,
    DOMAIN,
)
from custom_components.lock_code_manager.providers.zigbee2mqtt import Zigbee2MQTTLock

Z2M_TOPIC_NAME = "TestLockZ2M"
Z2M_FULL_TOPIC = f"zigbee2mqtt/{Z2M_TOPIC_NAME}"
Z2M_GET_TOPIC = f"{Z2M_FULL_TOPIC}/get"
Z2M_SET_TOPIC = f"{Z2M_FULL_TOPIC}/set"
Z2M_LOCK_ENTITY_ID = "lock.mqtt_test_z2m"


@dataclass
class MqttMessageBus:
    """Lightweight MQTT message bus for E2E testing.

    Tracks subscriptions and publishes so tests can fire incoming messages
    and verify outgoing publishes without a real Paho client.
    """

    subscriptions: dict[str, list[Callable]] = field(default_factory=dict)
    publishes: list[tuple[str, str]] = field(default_factory=list)

    def subscribe(self, topic: str, callback: Callable) -> Callable[[], None]:
        """Register a subscription callback and return an unsubscribe handle."""
        self.subscriptions.setdefault(topic, []).append(callback)

        def unsub() -> None:
            self.subscriptions.get(topic, []).remove(callback)

        return unsub

    async def publish(
        self,
        hass: HomeAssistant,
        topic: str,
        payload: str,
        **kwargs: Any,
    ) -> None:
        """Record a published message."""
        self.publishes.append((topic, payload))

    def fire_message(self, topic: str, payload: dict[str, Any]) -> None:
        """Simulate an incoming MQTT message on a topic.

        Creates a ReceiveMessage-like object and dispatches to all
        registered callbacks for the topic.
        """
        msg = MagicMock(spec=ReceiveMessage)
        msg.topic = topic
        msg.payload = json.dumps(payload).encode()
        msg.qos = 0
        msg.retain = False
        for cb in self.subscriptions.get(topic, []):
            cb(msg)


@pytest.fixture
def mqtt_bus() -> MqttMessageBus:
    """Create a fresh MQTT message bus for the test."""
    return MqttMessageBus()


@pytest.fixture
def mqtt_patches(mqtt_bus: MqttMessageBus):
    """Patch async_subscribe and async_publish at the HA MQTT component boundary.

    Also patches mqtt_config_entry_enabled to return True so the provider
    does not bail out during setup.
    """

    async def fake_subscribe(hass, topic, callback, *args, **kwargs):
        return mqtt_bus.subscribe(topic, callback)

    async def fake_publish(hass, topic, payload, *args, **kwargs):
        await mqtt_bus.publish(hass, topic, payload, **kwargs)

    with (
        patch(
            "custom_components.lock_code_manager.providers.zigbee2mqtt.async_subscribe",
            side_effect=fake_subscribe,
        ),
        patch(
            "custom_components.lock_code_manager.providers.zigbee2mqtt.async_publish",
            side_effect=fake_publish,
        ),
        patch(
            "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
            return_value=True,
        ),
    ):
        yield


@pytest.fixture
async def z2m_device_and_entity(
    hass: HomeAssistant,
) -> tuple[dr.DeviceEntry, er.RegistryEntry]:
    """Create a Z2M device and lock entity in the registries."""
    mqtt_entry = MockConfigEntry(domain="mqtt")
    mqtt_entry.add_to_hass(hass)
    mqtt_entry._async_set_state(hass, mqtt_entry.state, None)

    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    device = dev_reg.async_get_or_create(
        config_entry_id=mqtt_entry.entry_id,
        connections=set(),
        identifiers={("mqtt", "zigbee2mqtt_0xc0ffee")},
        name=Z2M_TOPIC_NAME,
    )

    lock_entity = ent_reg.async_get_or_create(
        "lock",
        "mqtt",
        "test_z2m",
        config_entry=mqtt_entry,
        device_id=device.id,
    )

    hass.states.async_set(lock_entity.entity_id, "locked")

    return device, lock_entity


@pytest.fixture
async def z2m_lock(
    hass: HomeAssistant,
    z2m_device_and_entity: tuple[dr.DeviceEntry, er.RegistryEntry],
    mqtt_patches,
) -> Zigbee2MQTTLock:
    """Create a Zigbee2MQTTLock with MQTT bus patched and device subscription active."""
    _, lock_entity = z2m_device_and_entity
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    mqtt_entry = hass.config_entries.async_entries("mqtt")[0]

    lock = Zigbee2MQTTLock(hass, dev_reg, ent_reg, mqtt_entry, lock_entity)
    # Ensure the device topic subscription is established for the fixture
    await lock._async_ensure_device_subscription()

    return lock


@pytest.fixture
async def z2m_lock_with_coordinator(
    hass: HomeAssistant,
    z2m_lock: Zigbee2MQTTLock,
    mqtt_bus: MqttMessageBus,
) -> Zigbee2MQTTLock:
    """Attach a real LCM coordinator to the Z2M lock.

    Sets up a Lock Code Manager config entry that manages the Z2M lock entity,
    then wires the coordinator onto the lock instance.
    """
    config = {
        CONF_LOCKS: [z2m_lock.lock.entity_id],
        CONF_SLOTS: {
            1: {CONF_NAME: "slot1", CONF_PIN: "1234", CONF_ENABLED: True},
            2: {CONF_NAME: "slot2", CONF_PIN: "5678", CONF_ENABLED: True},
        },
    }

    # Patch the class map so LCM setup uses our already-instantiated lock
    # instead of creating a new one (which would fail without real MQTT).
    def fake_create_lock(hass, dev_reg, ent_reg, config_entry, lock_entity_id):
        return z2m_lock

    with patch(
        "custom_components.lock_code_manager.helpers.INTEGRATIONS_CLASS_MAP",
        {"mqtt": type(z2m_lock)},
    ):
        # Intercept lock creation to return our existing lock instance
        with patch(
            "custom_components.lock_code_manager.helpers.async_create_lock_instance",
            side_effect=fake_create_lock,
        ):
            lcm_entry = MockConfigEntry(
                domain=DOMAIN, data=config, unique_id="test_z2m_e2e"
            )
            lcm_entry.add_to_hass(hass)
            await hass.config_entries.async_setup(lcm_entry.entry_id)
            await hass.async_block_till_done()

    return z2m_lock
