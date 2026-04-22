"""Payload parsing tests for Zigbee2MQTT lock provider."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.models import SlotCode
from custom_components.lock_code_manager.providers.zigbee2mqtt import (
    Zigbee2MQTTLock,
    _mqtt_payload_pin_has_code_value,
)

from .conftest import _minimal_lock


def test_mqtt_payload_pin_has_code_value_rejects_bool() -> None:
    """Boolean JSON must not count as a PIN payload (truthiness trap)."""
    assert _mqtt_payload_pin_has_code_value(False) is False
    assert _mqtt_payload_pin_has_code_value(True) is False


def test_users_enabled_without_pin_key_skips_push() -> None:
    """Do not infer EMPTY when expose_pin hides pin_code (enabled user, key absent)."""
    lock = _minimal_lock()
    lock.coordinator = MagicMock()
    lock._process_z2m_device_payload({"users": {"5": {"status": "enabled"}}})
    lock.coordinator.push_update.assert_not_called()


def test_users_enabled_with_numeric_zero_pin_updates() -> None:
    """Numeric zero is a valid digit; it must not be treated as a missing PIN."""
    lock = _minimal_lock()
    lock.coordinator = MagicMock()
    lock._process_z2m_device_payload(
        {"users": {"2": {"status": "enabled", "pin_code": 0}}}
    )
    lock.coordinator.push_update.assert_called_once_with({2: "0"})


def test_users_enabled_pin_null_clears_slot() -> None:
    """Explicit null pin in MQTT means cleared at the device."""
    lock = _minimal_lock()
    lock.coordinator = MagicMock()
    lock._process_z2m_device_payload(
        {"users": {"5": {"status": "enabled", "pin_code": None}}}
    )
    lock.coordinator.push_update.assert_called_once_with({5: SlotCode.EMPTY})


def test_users_non_numeric_slot_key_skipped() -> None:
    """Invalid user slot keys in ``users`` are ignored."""
    lock = _minimal_lock()
    lock.coordinator = MagicMock()
    lock._process_z2m_device_payload(
        {"users": {"bad": {"status": "enabled", "pin_code": "1"}}}
    )
    lock.coordinator.push_update.assert_not_called()


async def test_pin_code_get_disabled_or_empty_pin_sets_future_none() -> None:
    """PIN response with disabled user, empty pin, or numeric zero."""
    loop = asyncio.get_running_loop()
    lock = _minimal_lock()

    fut_disabled = loop.create_future()
    lock._pending_codes[7] = fut_disabled
    lock._process_z2m_device_payload(
        {"pin_code": {"user": 7, "user_enabled": False, "pin_code": "1234"}}
    )
    assert fut_disabled.done() and fut_disabled.result() is None

    fut_empty = loop.create_future()
    lock._pending_codes[8] = fut_empty
    lock._process_z2m_device_payload(
        {"pin_code": {"user": 8, "user_enabled": True, "pin_code": ""}}
    )
    assert fut_empty.done() and fut_empty.result() is None

    fut_zero = loop.create_future()
    lock._pending_codes[9] = fut_zero
    lock._process_z2m_device_payload(
        {"pin_code": {"user": 9, "user_enabled": True, "pin_code": 0}}
    )
    assert fut_zero.done() and fut_zero.result() == "0"


async def test_pin_code_deleted_schedules_refresh_task(
    hass: HomeAssistant,
    zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
) -> None:
    """Z2M action payloads request a coordinator refresh via async task."""
    lock = zigbee2mqtt_lock_with_device
    lock.coordinator = MagicMock()
    lock.coordinator.async_request_refresh = AsyncMock(return_value=None)

    lock._process_z2m_device_payload({"action": "pin_code_deleted", "action_user": 4})
    await hass.async_block_till_done()

    lock.coordinator.async_request_refresh.assert_awaited_once()


def test_pin_code_get_invalid_user_type_returns_early() -> None:
    """Non-numeric ``pin_code.user`` does not touch pending futures."""
    lock = _minimal_lock()
    lock.coordinator = MagicMock()
    fut = MagicMock(spec=["cancel", "done", "set_result"])
    fut.done.return_value = False
    lock._pending_codes[1] = fut  # type: ignore[assignment]
    lock._process_z2m_device_payload(
        {"pin_code": {"user": "notint", "user_enabled": True, "pin_code": "1"}}
    )
    fut.done.assert_not_called()
    fut.set_result.assert_not_called()


async def test_mqtt_payload_invalid_json_ignored(
    hass: HomeAssistant,
    zigbee2mqtt_lock_with_device: Zigbee2MQTTLock,
) -> None:
    """Subscription callback skips invalid JSON payloads."""
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
        ) as mock_subscribe,
    ):
        mock_subscribe.return_value = lambda: None
        lock.setup_push_subscription()
        await hass.async_block_till_done()
        cb = mock_subscribe.call_args[0][2]
        cb(SimpleNamespace(payload=b"not json {{{"))
        await hass.async_block_till_done()

    lock.coordinator.push_update.assert_not_called()


def test_keypad_unlock_action_fires_code_slot_event() -> None:
    """keypad_unlock with action_user fires async_fire_code_slot_event."""
    lock = _minimal_lock()
    lock.coordinator = MagicMock()
    lock.async_fire_code_slot_event = MagicMock()

    lock._process_z2m_device_payload({"action": "keypad_unlock", "action_user": 3})

    lock.async_fire_code_slot_event.assert_called_once_with(
        code_slot=3,
        to_locked=False,
        action_text="keypad_unlock",
        source_data={"action": "keypad_unlock", "action_user": 3},
    )


def test_keypad_lock_action_fires_code_slot_event() -> None:
    """keypad_lock with action_user fires event with to_locked=True."""
    lock = _minimal_lock()
    lock.coordinator = MagicMock()
    lock.async_fire_code_slot_event = MagicMock()

    lock._process_z2m_device_payload({"action": "keypad_lock", "action_user": 1})

    lock.async_fire_code_slot_event.assert_called_once_with(
        code_slot=1,
        to_locked=True,
        action_text="keypad_lock",
        source_data={"action": "keypad_lock", "action_user": 1},
    )


def test_lock_action_without_action_user_does_not_fire_event() -> None:
    """Lock action without action_user is ignored (manual key turn, no PIN)."""
    lock = _minimal_lock()
    lock.coordinator = MagicMock()
    lock.async_fire_code_slot_event = MagicMock()

    lock._process_z2m_device_payload({"action": "manual_lock"})

    lock.async_fire_code_slot_event.assert_not_called()


def test_lock_action_with_non_numeric_user_is_ignored() -> None:
    """Lock action with non-numeric action_user is ignored."""
    lock = _minimal_lock()
    lock.coordinator = MagicMock()
    lock.async_fire_code_slot_event = MagicMock()

    lock._process_z2m_device_payload({"action": "keypad_unlock", "action_user": "bad"})

    lock.async_fire_code_slot_event.assert_not_called()


def test_rf_unlock_action_fires_event() -> None:
    """rf_unlock with action_user fires event."""
    lock = _minimal_lock()
    lock.coordinator = MagicMock()
    lock.async_fire_code_slot_event = MagicMock()

    lock._process_z2m_device_payload({"action": "rf_unlock", "action_user": 5})

    lock.async_fire_code_slot_event.assert_called_once_with(
        code_slot=5,
        to_locked=False,
        action_text="rf_unlock",
        source_data={"action": "rf_unlock", "action_user": 5},
    )


def test_unknown_action_does_not_fire_event() -> None:
    """An unrecognized action value is ignored entirely."""
    lock = _minimal_lock()
    lock.coordinator = MagicMock()
    lock.async_fire_code_slot_event = MagicMock()

    lock._process_z2m_device_payload({"action": "something_else", "action_user": 1})

    lock.async_fire_code_slot_event.assert_not_called()
