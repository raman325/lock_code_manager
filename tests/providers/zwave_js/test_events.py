"""Test Z-Wave JS event handling: credential push updates and operation notifications."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry
from zwave_js_server.const.command_class.access_control import UserCredentialType
from zwave_js_server.event import Event as ZwaveEvent
from zwave_js_server.model.node import Node

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from custom_components.lock_code_manager.const import (
    ATTR_ACTION_TEXT,
    ATTR_CODE_SLOT,
    ATTR_FROM,
    ATTR_TO,
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
    EVENT_LOCK_STATE_CHANGED,
)
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers.zwave_js import ZWaveJSLock

from .helpers import async_capture_events

# ---------------------------------------------------------------------------
# Push subscription tests
# ---------------------------------------------------------------------------


async def test_subscribe_push_updates(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test subscribing to push updates."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    # Subscribe to push updates (idempotent - may already be subscribed)
    zwave_js_lock.subscribe_push_updates()

    assert zwave_js_lock._push_unsubs

    zwave_js_lock.unsubscribe_push_updates()
    assert not zwave_js_lock._push_unsubs

    await zwave_js_lock.async_unload(False)


async def test_subscribe_is_idempotent(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that calling subscribe multiple times is safe."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    zwave_js_lock.subscribe_push_updates()
    first_unsubs = list(zwave_js_lock._push_unsubs)

    zwave_js_lock.subscribe_push_updates()
    assert list(zwave_js_lock._push_unsubs) == first_unsubs

    zwave_js_lock.unsubscribe_push_updates()
    await zwave_js_lock.async_unload(False)


async def test_subscribe_push_no_crash_on_client_not_ready(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that push subscription handles client not ready without crashing."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    zwave_js_lock.unsubscribe_push_updates()
    assert not zwave_js_lock._push_unsubs

    # Make client appear not ready — should not crash
    with patch.object(
        zwave_js_lock, "_get_client_state", return_value=(False, "not connected")
    ):
        zwave_js_lock.subscribe_push_updates()

    # Should not have subscribed (no crash, no retry timer)
    assert not zwave_js_lock._push_unsubs

    await zwave_js_lock.async_unload(False)


async def test_subscribe_push_no_crash_on_node_error(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that push subscription handles node.on error without crashing."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    zwave_js_lock.unsubscribe_push_updates()
    assert not zwave_js_lock._push_unsubs

    # Make node.on raise — should not crash
    with patch.object(lock_schlage_be469, "on", side_effect=ValueError("not ready")):
        zwave_js_lock.subscribe_push_updates()

    assert not zwave_js_lock._push_unsubs

    await zwave_js_lock.async_unload(False)


# Three subscriptions registered (credential added, modified, deleted)
_EXPECTED_PUSH_UNSUB_COUNT = 3


async def test_subscribe_registers_three_credential_listeners(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that subscribing registers listeners for all three credential events."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    zwave_js_lock.subscribe_push_updates()
    assert len(zwave_js_lock._push_unsubs) == _EXPECTED_PUSH_UNSUB_COUNT

    zwave_js_lock.unsubscribe_push_updates()
    await zwave_js_lock.async_unload(False)


# ---------------------------------------------------------------------------
# Event filter tests
# ---------------------------------------------------------------------------


async def test_event_filter_matches_correct_node(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    lock_schlage_be469: Node,
) -> None:
    """Test that event filter matches events for the correct node."""
    dev_reg = dr.async_get(hass)
    device = dev_reg.async_get(zwave_js_lock.lock.device_id)
    assert device is not None

    event_data = {
        "home_id": zwave_js_lock.node.client.driver.controller.home_id,
        "node_id": lock_schlage_be469.node_id,
        "device_id": zwave_js_lock.lock.device_id,
    }

    assert zwave_js_lock._zwave_js_event_filter(event_data) is True

    event_data_wrong_node = {
        "home_id": zwave_js_lock.node.client.driver.controller.home_id,
        "node_id": 999,
        "device_id": zwave_js_lock.lock.device_id,
    }
    assert zwave_js_lock._zwave_js_event_filter(event_data_wrong_node) is False


# ---------------------------------------------------------------------------
# Notification event tests (operation events — lock/unlock)
# ---------------------------------------------------------------------------


async def test_notification_event_keypad_lock_fires_lock_state_changed(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that a keypad lock notification event fires EVENT_LOCK_STATE_CHANGED."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    # Capture LCM lock state changed events
    events = async_capture_events(hass, EVENT_LOCK_STATE_CHANGED)

    # Create a notification event matching the pattern from Home Assistant core tests
    # Type 6 = ACCESS_CONTROL, Event 5 = KEYPAD_LOCK_OPERATION
    event = ZwaveEvent(
        type="notification",
        data={
            "source": "node",
            "event": "notification",
            "nodeId": lock_schlage_be469.node_id,
            "endpointIndex": 0,
            "ccId": 113,  # Notification CC
            "args": {
                "type": 6,  # ACCESS_CONTROL
                "event": 5,  # KEYPAD_LOCK_OPERATION
                "label": "Access Control",
                "eventLabel": "Keypad lock operation",
                "parameters": {"userId": 1},
            },
        },
    )
    lock_schlage_be469.receive_event(event)
    await hass.async_block_till_done()

    # Verify the LCM event was fired
    assert len(events) == 1
    assert events[0].data[ATTR_CODE_SLOT] == 1
    assert events[0].data[ATTR_ACTION_TEXT] == "Keypad lock operation"
    assert events[0].data[ATTR_TO] == "locked"
    assert events[0].data[ATTR_FROM] == "unlocked"

    await zwave_js_lock.async_unload(False)


async def test_notification_event_keypad_unlock_fires_lock_state_changed(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that a keypad unlock notification event fires EVENT_LOCK_STATE_CHANGED."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    events = async_capture_events(hass, EVENT_LOCK_STATE_CHANGED)

    # Type 6 = ACCESS_CONTROL, Event 6 = KEYPAD_UNLOCK_OPERATION
    event = ZwaveEvent(
        type="notification",
        data={
            "source": "node",
            "event": "notification",
            "nodeId": lock_schlage_be469.node_id,
            "endpointIndex": 0,
            "ccId": 113,
            "args": {
                "type": 6,
                "event": 6,  # KEYPAD_UNLOCK_OPERATION
                "label": "Access Control",
                "eventLabel": "Keypad unlock operation",
                "parameters": {"userId": 2},
            },
        },
    )
    lock_schlage_be469.receive_event(event)
    await hass.async_block_till_done()

    assert len(events) == 1
    assert events[0].data[ATTR_CODE_SLOT] == 2
    assert events[0].data[ATTR_ACTION_TEXT] == "Keypad unlock operation"
    assert events[0].data[ATTR_TO] == "unlocked"
    assert events[0].data[ATTR_FROM] == "locked"

    await zwave_js_lock.async_unload(False)


# ---------------------------------------------------------------------------
# Credential node event push tests
# ---------------------------------------------------------------------------


def _make_credential_added_event(
    node_id: int,
    credential_slot: int,
    credential_type: int = UserCredentialType.PIN_CODE,
    user_id: int = 1,
) -> ZwaveEvent:
    """Build a 'credential added' ZwaveEvent for a given slot."""
    return ZwaveEvent(
        type="credential added",
        data={
            "source": "node",
            "event": "credential added",
            "nodeId": node_id,
            "endpointIndex": 0,
            "args": {
                "userId": user_id,
                "credentialType": credential_type,
                "credentialSlot": credential_slot,
            },
        },
    )


def _make_credential_modified_event(
    node_id: int,
    credential_slot: int,
    credential_type: int = UserCredentialType.PIN_CODE,
    user_id: int = 1,
) -> ZwaveEvent:
    """Build a 'credential modified' ZwaveEvent for a given slot."""
    return ZwaveEvent(
        type="credential modified",
        data={
            "source": "node",
            "event": "credential modified",
            "nodeId": node_id,
            "endpointIndex": 0,
            "args": {
                "userId": user_id,
                "credentialType": credential_type,
                "credentialSlot": credential_slot,
            },
        },
    )


def _make_credential_deleted_event(
    node_id: int,
    credential_slot: int,
    credential_type: int = UserCredentialType.PIN_CODE,
    user_id: int = 1,
) -> ZwaveEvent:
    """Build a 'credential deleted' ZwaveEvent for a given slot."""
    return ZwaveEvent(
        type="credential deleted",
        data={
            "source": "node",
            "event": "credential deleted",
            "nodeId": node_id,
            "endpointIndex": 0,
            "args": {
                "userId": user_id,
                "credentialType": credential_type,
                "credentialSlot": credential_slot,
            },
        },
    )


async def test_credential_added_pin_pushes_unreadable(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    lock_schlage_be469: Node,
) -> None:
    """Test that a 'credential added' event for a PIN pushes SlotCredential.unreadable()."""
    mock_coordinator = MagicMock()
    mock_coordinator.data = {}
    zwave_js_lock.coordinator = mock_coordinator

    zwave_js_lock.subscribe_push_updates()

    lock_schlage_be469.receive_event(
        _make_credential_added_event(lock_schlage_be469.node_id, credential_slot=2)
    )
    await hass.async_block_till_done()

    mock_coordinator.push_update.assert_called_once_with(
        {2: SlotCredential.unreadable()}
    )

    zwave_js_lock.unsubscribe_push_updates()


async def test_credential_modified_pin_pushes_unreadable(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    lock_schlage_be469: Node,
) -> None:
    """Test that a 'credential modified' event for a PIN pushes SlotCredential.unreadable()."""
    mock_coordinator = MagicMock()
    mock_coordinator.data = {}
    zwave_js_lock.coordinator = mock_coordinator

    zwave_js_lock.subscribe_push_updates()

    lock_schlage_be469.receive_event(
        _make_credential_modified_event(lock_schlage_be469.node_id, credential_slot=3)
    )
    await hass.async_block_till_done()

    mock_coordinator.push_update.assert_called_once_with(
        {3: SlotCredential.unreadable()}
    )

    zwave_js_lock.unsubscribe_push_updates()


async def test_credential_deleted_pin_pushes_empty(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    lock_schlage_be469: Node,
) -> None:
    """Test that a 'credential deleted' event for a PIN pushes SlotCredential.empty()."""
    mock_coordinator = MagicMock()
    mock_coordinator.data = {5: SlotCredential.unreadable()}
    zwave_js_lock.coordinator = mock_coordinator

    zwave_js_lock.subscribe_push_updates()

    lock_schlage_be469.receive_event(
        _make_credential_deleted_event(lock_schlage_be469.node_id, credential_slot=5)
    )
    await hass.async_block_till_done()

    mock_coordinator.push_update.assert_called_once_with({5: SlotCredential.empty()})

    zwave_js_lock.unsubscribe_push_updates()


async def test_credential_added_non_pin_ignored(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    lock_schlage_be469: Node,
) -> None:
    """Test that 'credential added' for a non-PIN credential type is ignored."""
    mock_coordinator = MagicMock()
    mock_coordinator.data = {}
    zwave_js_lock.coordinator = mock_coordinator

    zwave_js_lock.subscribe_push_updates()

    # Use a non-PIN credential type (e.g. RFID = 2)
    lock_schlage_be469.receive_event(
        _make_credential_added_event(
            lock_schlage_be469.node_id,
            credential_slot=2,
            credential_type=2,  # not PIN_CODE (1)
        )
    )
    await hass.async_block_till_done()

    mock_coordinator.push_update.assert_not_called()

    zwave_js_lock.unsubscribe_push_updates()


async def test_credential_deleted_non_pin_ignored(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    lock_schlage_be469: Node,
) -> None:
    """Test that 'credential deleted' for a non-PIN credential type is ignored."""
    mock_coordinator = MagicMock()
    mock_coordinator.data = {4: SlotCredential.unreadable()}
    zwave_js_lock.coordinator = mock_coordinator

    zwave_js_lock.subscribe_push_updates()

    lock_schlage_be469.receive_event(
        _make_credential_deleted_event(
            lock_schlage_be469.node_id,
            credential_slot=4,
            credential_type=2,  # not PIN_CODE (1)
        )
    )
    await hass.async_block_till_done()

    mock_coordinator.push_update.assert_not_called()

    zwave_js_lock.unsubscribe_push_updates()
