"""Shared fixtures and constants for Zigbee2MQTT provider tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.mqtt.models import ReceiveMessage
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
)
from custom_components.lock_code_manager.providers.zigbee2mqtt import (
    Zigbee2MQTTLock,
)

Z2M_TOPIC_NAME = "TestLockZ2M"
Z2M_FULL_TOPIC = f"zigbee2mqtt/{Z2M_TOPIC_NAME}"
Z2M_GET_TOPIC = f"{Z2M_FULL_TOPIC}/get"
Z2M_SET_TOPIC = f"{Z2M_FULL_TOPIC}/set"
Z2M_LOCK_UNIQUE_ID = "test_z2m"


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


def _minimal_lock() -> Zigbee2MQTTLock:
    """Build a Zigbee2MQTTLock without Home Assistant test harness."""
    lock_entity = SimpleNamespace(
        entity_id="lock.test",
        device_id=None,
        platform="mqtt",
        config_entry_id=None,
        unique_id=None,
    )
    return Zigbee2MQTTLock(
        MagicMock(),
        MagicMock(),
        MagicMock(),
        None,
        lock_entity,  # type: ignore[arg-type]
    )


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
        # Auto-respond to GET requests so the coordinator's initial
        # refresh doesn't block on 10-second timeouts per slot.
        if topic.endswith("/get"):
            try:
                body = json.loads(payload)
                slot = body.get("pin_code", {}).get("user")
                if slot is not None:
                    device_topic = topic.rsplit("/get", 1)[0]
                    mqtt_bus.fire_message(
                        device_topic,
                        {
                            "pin_code": {
                                "user": slot,
                                "user_enabled": False,
                                "pin_code": None,
                            }
                        },
                    )
            except json.JSONDecodeError, TypeError:
                pass

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
async def mqtt_lock_entity(hass: HomeAssistant) -> er.RegistryEntry:
    """Create an MQTT config entry, Z2M device, and lock entity.

    Sets the MQTT config entry to LOADED state so the provider's
    async_is_integration_connected check passes.
    """
    mqtt_entry = MockConfigEntry(domain="mqtt")
    mqtt_entry.add_to_hass(hass)
    mqtt_entry._async_set_state(hass, ConfigEntryState.LOADED, None)

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

    return lock_entity


@pytest.fixture
async def lcm_config_entry(
    hass: HomeAssistant,
    mqtt_lock_entity: er.RegistryEntry,
    mqtt_patches,
) -> MockConfigEntry:
    """Set up a full LCM config entry managing the Z2M lock.

    This goes through the real async_setup_entry path: LCM discovers the
    lock entity is from the mqtt platform, instantiates Zigbee2MQTTLock,
    creates the coordinator, entities, and sync managers.
    """
    entity_id = mqtt_lock_entity.entity_id
    config = {
        CONF_LOCKS: [entity_id],
        CONF_SLOTS: {
            1: {"name": "slot1", "pin": "1234", "enabled": True},
            2: {"name": "slot2", "pin": "5678", "enabled": True},
        },
    }
    lcm_entry = MockConfigEntry(domain=DOMAIN, data=config, unique_id="test_z2m_e2e")
    lcm_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(lcm_entry.entry_id)
    await hass.async_block_till_done()

    yield lcm_entry

    await hass.config_entries.async_unload(lcm_entry.entry_id)


def get_z2m_lock(lcm_entry: MockConfigEntry) -> Zigbee2MQTTLock:
    """Extract the Zigbee2MQTTLock from a loaded LCM config entry."""
    locks = lcm_entry.runtime_data.locks
    assert len(locks) == 1, f"Expected 1 lock, found {len(locks)}"
    lock = next(iter(locks.values()))
    assert isinstance(lock, Zigbee2MQTTLock)
    return lock


@pytest.fixture
def z2m_lock(hass, lcm_config_entry):
    """Extract the Z2M lock from the LCM config entry."""
    return get_z2m_lock(lcm_config_entry)


@pytest.fixture
async def zigbee2mqtt_lock_with_device(hass: HomeAssistant) -> Zigbee2MQTTLock:
    """Zigbee2MQTTLock backed by a device with Zigbee2MQTT identifiers."""
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
        "test_z2m_push",
        config_entry=mqtt_entry,
        device_id=device.id,
    )

    return Zigbee2MQTTLock(hass, dev_reg, ent_reg, mqtt_entry, lock_entity)


@pytest.fixture
async def zigbee2mqtt_lock_wrong_identifier(hass: HomeAssistant) -> Zigbee2MQTTLock:
    """Lock device that is MQTT but not recognized as Zigbee2MQTT."""
    mqtt_entry = MockConfigEntry(domain="mqtt")
    mqtt_entry.add_to_hass(hass)
    mqtt_entry._async_set_state(hass, mqtt_entry.state, None)

    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    device = dev_reg.async_get_or_create(
        config_entry_id=mqtt_entry.entry_id,
        connections=set(),
        identifiers={("mqtt", "other_bridge_123")},
        name="SomeLock",
    )

    lock_entity = ent_reg.async_get_or_create(
        "lock",
        "mqtt",
        "test_z2m_other",
        config_entry=mqtt_entry,
        device_id=device.id,
    )

    return Zigbee2MQTTLock(hass, dev_reg, ent_reg, mqtt_entry, lock_entity)
