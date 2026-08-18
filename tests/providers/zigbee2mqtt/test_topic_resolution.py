"""Topic resolution from MQTT discovery data for the Zigbee2MQTT provider."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from homeassistant.components.mqtt import debug_info as mqtt_debug_info
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.domain.credentials import (
    credential_from_slot,
)
from custom_components.lock_code_manager.domain.exceptions import LockDisconnected
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers.zigbee2mqtt import (
    Zigbee2MQTTLock,
)

from .conftest import (
    Z2M_FULL_TOPIC,
    Z2M_TOPIC_NAME,
    _minimal_lock,
    async_discover_z2m_lock,
)


def _build_lock(hass: HomeAssistant, lock_entity: er.RegistryEntry) -> Zigbee2MQTTLock:
    """Construct a provider instance around a discovered lock entity."""
    mqtt_entry: ConfigEntry = hass.config_entries.async_entries("mqtt")[0]
    return Zigbee2MQTTLock(
        hass, dr.async_get(hass), er.async_get(hass), mqtt_entry, lock_entity
    )


async def test_debug_info_shape_pin(
    hass: HomeAssistant, mqtt_lock_discovered: er.RegistryEntry
) -> None:
    """
    Pin the shape of mqtt.debug_info.info_for_device that the provider relies on.

    The provider reads entities[*].entity_id and
    entities[*].discovery_data.payload.state_topic / command_topic. If this
    test fails after a Home Assistant bump, _resolve_device_topic() needs
    updating in the same commit.
    """
    device_id = mqtt_lock_discovered.device_id
    assert device_id is not None
    info = mqtt_debug_info.info_for_device(hass, device_id)
    entities = info["entities"]
    entry = next(
        e for e in entities if e["entity_id"] == mqtt_lock_discovered.entity_id
    )
    payload = entry["discovery_data"]["payload"]
    assert payload["state_topic"] == Z2M_FULL_TOPIC
    assert payload["command_topic"] == f"{Z2M_FULL_TOPIC}/set"


async def test_resolve_device_topic_from_state_topic(
    hass: HomeAssistant, mqtt_lock_discovered: er.RegistryEntry
) -> None:
    """The device topic comes verbatim from the discovery state_topic."""
    lock = _build_lock(hass, mqtt_lock_discovered)
    assert lock._resolve_device_topic() == Z2M_FULL_TOPIC
    assert lock._get_topic() == Z2M_FULL_TOPIC
    assert lock._get_topic("set") == f"{Z2M_FULL_TOPIC}/set"
    assert lock._get_topic("get") == f"{Z2M_FULL_TOPIC}/get"


async def test_resolve_device_topic_multi_level_base_topic(
    hass: HomeAssistant, mqtt_mock, mqtt_teardown
) -> None:
    """Multi-level base topics (home/z2m) resolve verbatim — no wildcard parsing."""
    entity = await async_discover_z2m_lock(
        hass, ieee="0xdeadbeef", name="BackDoor", base_topic="home/z2m"
    )
    lock = _build_lock(hass, entity)
    assert lock._resolve_device_topic() == "home/z2m/BackDoor"


async def test_resolve_device_topic_command_topic_fallback(
    hass: HomeAssistant, mqtt_mock, mqtt_teardown
) -> None:
    """Without a state_topic, the command_topic minus /set is used."""
    entity = await async_discover_z2m_lock(
        hass, ieee="0xfeed", name="SideDoor", include_state_topic=False
    )
    lock = _build_lock(hass, entity)
    assert lock._resolve_device_topic() == "zigbee2mqtt/SideDoor"


def test_resolve_device_topic_no_device_id() -> None:
    """A lock entity without a device resolves to no topic."""
    lock = _minimal_lock()
    assert lock._resolve_device_topic() is None


async def test_resolve_device_topic_no_discovery_data(
    hass: HomeAssistant, zigbee2mqtt_lock_with_device: Zigbee2MQTTLock
) -> None:
    """A registry-created entity with no discovery data resolves to no topic."""
    assert zigbee2mqtt_lock_with_device._resolve_device_topic() is None


async def test_resolve_device_topic_non_z2m_device(
    hass: HomeAssistant, zigbee2mqtt_lock_wrong_identifier: Zigbee2MQTTLock
) -> None:
    """A device without a zigbee2mqtt_* identifier never resolves a topic."""
    assert zigbee2mqtt_lock_wrong_identifier._resolve_device_topic() is None


async def test_push_subscription_moves_on_topic_drift(
    hass: HomeAssistant, mqtt_lock_discovered: er.RegistryEntry
) -> None:
    """A re-fired discovery with a new base topic moves the push subscription."""
    lock = _build_lock(hass, mqtt_lock_discovered)
    await lock._async_ensure_device_subscription()
    assert lock._subscribed_topic == Z2M_FULL_TOPIC

    # Bridge base topic changes (e.g. Z2M reconfigured); discovery re-fires.
    await async_discover_z2m_lock(hass, base_topic="renamed_bridge")

    lock.setup_push_subscription()
    await hass.async_block_till_done()
    assert lock._subscribed_topic == f"renamed_bridge/{Z2M_TOPIC_NAME}"


async def test_push_subscription_kept_when_topic_transiently_unresolvable(
    hass: HomeAssistant, mqtt_lock_discovered: er.RegistryEntry
) -> None:
    """If resolution transiently fails, the existing subscription is kept."""
    lock = _build_lock(hass, mqtt_lock_discovered)
    await lock._async_ensure_device_subscription()
    assert lock._push_unsubs

    with patch.object(lock, "_resolve_device_topic", return_value=None):
        lock.setup_push_subscription()
        await hass.async_block_till_done()

    assert lock._push_unsubs
    assert lock._subscribed_topic == Z2M_FULL_TOPIC


async def test_teardown_clears_subscribed_topic(
    hass: HomeAssistant, mqtt_lock_discovered: er.RegistryEntry
) -> None:
    """Teardown resets the recorded topic so re-setup subscribes fresh."""
    lock = _build_lock(hass, mqtt_lock_discovered)
    await lock._async_ensure_device_subscription()
    lock.teardown_push_subscription()
    assert lock._subscribed_topic is None
    assert not lock._push_unsubs


async def test_unresolvable_topic_never_publishes(
    hass: HomeAssistant,
    mqtt_mock,
    zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    mqtt_teardown,
) -> None:
    """No discovery data → LockDisconnected, and nothing is published anywhere."""
    lock = zigbee2mqtt_lock_with_device
    mqtt_mock.async_publish.reset_mock()

    credential = credential_from_slot(1, SlotCredential.known("1234"))
    with pytest.raises(LockDisconnected):
        await lock.async_set_credential(
            1, credential, "1234", name=None, source="direct"
        )
    with pytest.raises(LockDisconnected):
        await lock.async_get_users()

    mqtt_mock.async_publish.assert_not_called()


async def test_two_bridges_resolve_independent_topics(
    hass: HomeAssistant, mqtt_lock_discovered: er.RegistryEntry
) -> None:
    """Locks on different bridges with different base topics don't cross wires."""
    second = await async_discover_z2m_lock(
        hass, ieee="0xbee2", name="Outbuilding", base_topic="z2m_outbuilding"
    )
    lock_a = _build_lock(hass, mqtt_lock_discovered)
    lock_b = _build_lock(hass, second)
    assert lock_a._resolve_device_topic() == Z2M_FULL_TOPIC
    assert lock_b._resolve_device_topic() == "z2m_outbuilding/Outbuilding"
