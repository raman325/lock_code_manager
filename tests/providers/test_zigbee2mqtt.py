"""Tests for the Zigbee2MQTT lock provider."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.exceptions import LockDisconnected
from custom_components.lock_code_manager.models import SlotCode
from custom_components.lock_code_manager.providers.zigbee2mqtt import Zigbee2MQTTLock

Z2M_TOPIC_NAME = "TestLockZ2M"
Z2M_FULL_TOPIC = f"zigbee2mqtt/{Z2M_TOPIC_NAME}"


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


def test_users_enabled_without_pin_key_skips_push() -> None:
    """Do not infer EMPTY when expose_pin hides pin_code (enabled user, key absent)."""
    lock = _minimal_lock()
    lock.coordinator = MagicMock()
    lock._process_z2m_device_payload({"users": {"5": {"status": "enabled"}}})
    lock.coordinator.push_update.assert_not_called()


def test_users_enabled_with_pin_updates() -> None:
    """When pin_code is present, coordinator receives the value."""
    lock = _minimal_lock()
    lock.coordinator = MagicMock()
    lock._process_z2m_device_payload(
        {"users": {"5": {"status": "enabled", "pin_code": "4242"}}}
    )
    lock.coordinator.push_update.assert_called_once_with({5: "4242"})


def test_users_enabled_pin_null_clears_slot() -> None:
    """Explicit null pin in MQTT means cleared at the device."""
    lock = _minimal_lock()
    lock.coordinator = MagicMock()
    lock._process_z2m_device_payload(
        {"users": {"5": {"status": "enabled", "pin_code": None}}}
    )
    lock.coordinator.push_update.assert_called_once_with({5: SlotCode.EMPTY})


def test_non_enabled_user_is_empty() -> None:
    """Disabled or non-enabled statuses clear coordinator slot."""
    lock = _minimal_lock()
    lock.coordinator = MagicMock()
    lock._process_z2m_device_payload({"users": {"5": {"status": "disabled"}}})
    lock.coordinator.push_update.assert_called_once_with({5: SlotCode.EMPTY})


class TestPushSubscription:
    """MQTT push subscription lifecycle for Zigbee2MQTTLock."""

    async def test_subscribes_to_z2m_device_topic(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """setup_push_subscription registers async_subscribe on base_topic/friendly_name."""
        lock = zigbee2mqtt_lock_with_device
        lock.coordinator = MagicMock()

        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=True,
            ),
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.async_subscribe",
                new_callable=AsyncMock,
            ) as mock_async_subscribe,
        ):
            mock_async_subscribe.return_value = lambda: None

            lock.setup_push_subscription()
            await hass.async_block_till_done()

            mock_async_subscribe.assert_called_once()
            assert mock_async_subscribe.call_args[0][1] == Z2M_FULL_TOPIC

    async def test_mqtt_payload_delivers_push_update_on_event_loop(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """Incoming MQTT JSON schedules payload handling and coordinator.push_update."""
        lock = zigbee2mqtt_lock_with_device
        lock.coordinator = MagicMock()

        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=True,
            ),
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.async_subscribe",
                new_callable=AsyncMock,
            ) as mock_async_subscribe,
        ):
            mock_async_subscribe.return_value = lambda: None

            lock.setup_push_subscription()
            await hass.async_block_till_done()

            msg_callback = mock_async_subscribe.call_args[0][2]
            payload = {"users": {"7": {"status": "enabled", "pin_code": "9090"}}}
            msg = SimpleNamespace(payload=json.dumps(payload).encode())

            msg_callback(msg)
            await hass.async_block_till_done()

            lock.coordinator.push_update.assert_called_once_with({7: "9090"})

    async def test_setup_push_idempotent(
        self,
        hass: HomeAssistant,
        zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
    ) -> None:
        """Second setup_push_subscription does not subscribe again."""
        lock = zigbee2mqtt_lock_with_device
        lock.coordinator = MagicMock()

        with (
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.mqtt_config_entry_enabled",
                return_value=True,
            ),
            patch(
                "custom_components.lock_code_manager.providers.zigbee2mqtt.async_subscribe",
                new_callable=AsyncMock,
            ) as mock_async_subscribe,
        ):
            mock_async_subscribe.return_value = lambda: None

            lock.setup_push_subscription()
            await hass.async_block_till_done()
            lock.setup_push_subscription()
            await hass.async_block_till_done()

            mock_async_subscribe.assert_called_once()

    async def test_setup_push_raises_when_no_mqtt_topic(
        self,
        zigbee2mqtt_lock_wrong_identifier: Zigbee2MQTTLock,
    ) -> None:
        """Without a Z2M-friendly topic, setup raises LockDisconnected."""
        lock = zigbee2mqtt_lock_wrong_identifier
        lock.coordinator = MagicMock()

        with pytest.raises(LockDisconnected):
            lock.setup_push_subscription()
