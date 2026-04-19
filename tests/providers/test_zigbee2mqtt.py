"""Tests for the Zigbee2MQTT lock provider."""

from types import SimpleNamespace
from unittest.mock import MagicMock

from custom_components.lock_code_manager.models import SlotCode
from custom_components.lock_code_manager.providers.zigbee2mqtt import Zigbee2MQTTLock


def _minimal_lock() -> Zigbee2MQTTLock:
    """Build a Zigbee2MQTTLock without Home Assistant test harness."""
    lock_entity = SimpleNamespace(entity_id="lock.test", device_id=None)
    return Zigbee2MQTTLock(
        MagicMock(),
        MagicMock(),
        MagicMock(),
        None,
        lock_entity,  # type: ignore[arg-type]
    )


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
