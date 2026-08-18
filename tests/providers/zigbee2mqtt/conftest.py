"""Shared fixtures and constants for Zigbee2MQTT provider tests."""

from __future__ import annotations

from collections.abc import AsyncGenerator
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import DEFAULT, MagicMock

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

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

Z2M_IEEE = "0xc0ffee"


def z2m_lock_discovery_payload(
    *,
    ieee: str = Z2M_IEEE,
    name: str = Z2M_TOPIC_NAME,
    base_topic: str = "zigbee2mqtt",
    include_state_topic: bool = True,
) -> dict[str, Any]:
    """
    Build a Zigbee2MQTT-shaped Home Assistant discovery payload for a lock.

    Mirrors what zigbee-herdsman-converters publishes: the device topic is
    ``<base_topic>/<friendly_name>`` and commands go to ``<device topic>/set``.
    """
    device_topic = f"{base_topic}/{name}"
    payload: dict[str, Any] = {
        "name": None,
        "command_topic": f"{device_topic}/set",
        "payload_lock": "LOCK",
        "payload_unlock": "UNLOCK",
        "state_locked": "LOCKED",
        "state_unlocked": "UNLOCKED",
        "value_template": "{{ value_json.state }}",
        "unique_id": f"{ieee}_lock_zigbee2mqtt",
        "device": {
            "identifiers": [f"zigbee2mqtt_{ieee}"],
            "name": name,
            "manufacturer": "Test",
            "model": "Test lock",
        },
    }
    if include_state_topic:
        payload["state_topic"] = device_topic
    return payload


async def async_discover_z2m_lock(
    hass: HomeAssistant,
    *,
    ieee: str = Z2M_IEEE,
    name: str = Z2M_TOPIC_NAME,
    base_topic: str = "zigbee2mqtt",
    include_state_topic: bool = True,
) -> er.RegistryEntry:
    """Fire a Z2M-style discovery config and return the created lock entity."""
    discovery_topic = f"homeassistant/lock/{ieee}/lock/config"
    async_fire_mqtt_message(
        hass,
        discovery_topic,
        json.dumps(
            z2m_lock_discovery_payload(
                ieee=ieee,
                name=name,
                base_topic=base_topic,
                include_state_topic=include_state_topic,
            )
        ),
    )
    await hass.async_block_till_done()
    ent_reg = er.async_get(hass)
    entity_id = ent_reg.async_get_entity_id("lock", "mqtt", f"{ieee}_lock_zigbee2mqtt")
    assert entity_id is not None, "discovery did not create the lock entity"
    # Seed a state so the entity is available.
    async_fire_mqtt_message(hass, f"{base_topic}/{name}", '{"state": "LOCKED"}')
    await hass.async_block_till_done()
    entry = ent_reg.async_get(entity_id)
    assert entry is not None
    return entry


@pytest.fixture
async def mqtt_teardown(hass: HomeAssistant, mqtt_client_mock) -> AsyncGenerator[None]:
    """
    Cancel the MQTT client's misc periodic timer after the test.

    HA's MQTT client cancels that timer only on socket close, which the paho
    client mock never fires. Fire it here so teardown does not trip the
    lingering-timer check in verify_cleanup. Any test that sets up the real
    MQTT integration via ``mqtt_mock`` needs this.
    """
    yield
    mqtt_client_mock.on_socket_close(
        mqtt_client_mock, None, MagicMock(fileno=MagicMock(return_value=-1))
    )
    await hass.async_block_till_done()


@pytest.fixture
async def mqtt_lock_discovered(
    hass: HomeAssistant, mqtt_mock, mqtt_teardown
) -> er.RegistryEntry:
    """Z2M lock entity created through real MQTT discovery (default base topic)."""
    return await async_discover_z2m_lock(hass)


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
def auto_get_responder(hass: HomeAssistant, mqtt_mock):
    """
    Answer pin_code GET publishes like a Zigbee2MQTT bridge would.

    The mocked paho client never echoes publishes back, so tests that run the
    coordinator's initial refresh need GET requests answered or every slot
    read blocks on its 10-second timeout.

    The hook lives on ``mqtt_mock.async_publish`` (the HA MQTT client mock),
    not the paho layer: that mock wraps the real client method, and a
    side_effect that returns ``unittest.mock.DEFAULT`` keeps the wrapped
    call (and therefore the real publish pass-through) intact.
    """

    def _respond(topic: str, payload: str, *args: Any, **kwargs: Any) -> Any:
        if not topic.endswith("/get"):
            return DEFAULT
        try:
            body = json.loads(payload)
        except json.JSONDecodeError, TypeError:
            return DEFAULT
        slot = body.get("pin_code", {}).get("user")
        if slot is None:
            return DEFAULT
        device_topic = topic.removesuffix("/get")
        async_fire_mqtt_message(
            hass,
            device_topic,
            json.dumps(
                {"pin_code": {"user": slot, "user_enabled": False, "pin_code": None}}
            ),
        )
        return DEFAULT

    mqtt_mock.async_publish.side_effect = _respond
    yield
    mqtt_mock.async_publish.side_effect = None


@pytest.fixture
async def zigbee2mqtt_lock_connected(
    hass: HomeAssistant, mqtt_lock_discovered: er.RegistryEntry
) -> Zigbee2MQTTLock:
    """Build a provider instance around a real-discovery lock with resolvable topics."""
    mqtt_entry = hass.config_entries.async_entries("mqtt")[0]
    return Zigbee2MQTTLock(
        hass, dr.async_get(hass), er.async_get(hass), mqtt_entry, mqtt_lock_discovered
    )


@pytest.fixture
async def lcm_config_entry(
    hass: HomeAssistant,
    mqtt_lock_discovered: er.RegistryEntry,
    auto_get_responder,
    mqtt_teardown,
) -> AsyncGenerator[MockConfigEntry]:
    """
    Set up a full LCM config entry managing the Z2M lock.

    This goes through the real async_setup_entry path: LCM discovers the
    lock entity is from the mqtt platform, instantiates Zigbee2MQTTLock,
    creates the coordinator, entities, and sync managers.
    """
    entity_id = mqtt_lock_discovered.entity_id
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
    await hass.async_block_till_done()


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
