"""Test Z-Wave JS event handling: credential push updates and operation notifications."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from zwave_js_server.const import CommandClass
from zwave_js_server.const.command_class.access_control import UserCredentialType
from zwave_js_server.const.command_class.lock import CodeSlotStatus
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
from custom_components.lock_code_manager.domain.exceptions import LockDisconnected
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
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
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
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
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
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
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
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
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


async def test_subscribe_push_cleans_up_partial_subscription_on_error(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """A failure partway through subscribing releases the unsubs already registered."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    zwave_js_lock.unsubscribe_push_updates()
    assert not zwave_js_lock._push_unsubs

    # First node.on succeeds (registers an unsub), the second raises -> the
    # partial registration must be cleaned up, not leaked.
    first_unsub = MagicMock()
    with (
        patch.object(
            lock_schlage_be469,
            "on",
            side_effect=[first_unsub, ValueError("not ready")],
        ),
        pytest.raises(LockDisconnected),
    ):
        # Call the raw subscriber directly; the public wrapper swallows
        # LockDisconnected, so it would hide the raise we want to assert.
        zwave_js_lock.setup_push_subscription()

    assert not zwave_js_lock._push_unsubs
    first_unsub.assert_called_once()

    await zwave_js_lock.async_unload(False)


# Three subscriptions registered (credential added, modified, deleted)
_EXPECTED_PUSH_UNSUB_COUNT = 3


async def test_subscribe_registers_three_credential_listeners(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
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
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
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
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
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
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
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


async def test_notification_event_non_access_control_ignored(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """A notification of a non Access Control type is ignored (no LCM event)."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    events = async_capture_events(hass, EVENT_LOCK_STATE_CHANGED)

    # Type 8 = POWER_MANAGEMENT (not ACCESS_CONTROL) → handler returns early
    event = ZwaveEvent(
        type="notification",
        data={
            "source": "node",
            "event": "notification",
            "nodeId": lock_schlage_be469.node_id,
            "endpointIndex": 0,
            "ccId": 113,
            "args": {
                "type": 8,
                "event": 1,
                "label": "Power Management",
                "eventLabel": "Power has been applied",
                "parameters": {},
            },
        },
    )
    lock_schlage_be469.receive_event(event)
    await hass.async_block_till_done()

    assert events == []

    await zwave_js_lock.async_unload(False)


# ---------------------------------------------------------------------------
# Credential node event push tests
# ---------------------------------------------------------------------------


def _make_credential_added_event(
    node_id: int,
    credential_slot: int,
    credential_type: int = UserCredentialType.PIN_CODE,
    user_id: int = 1,
    credential_data: str | None = None,
) -> ZwaveEvent:
    """Build a 'credential added' ZwaveEvent for a given slot."""
    args: dict = {
        "userId": user_id,
        "credentialType": credential_type,
        "credentialSlot": credential_slot,
    }
    if credential_data is not None:
        args["data"] = credential_data
    return ZwaveEvent(
        type="credential added",
        data={
            "source": "node",
            "event": "credential added",
            "nodeId": node_id,
            "endpointIndex": 0,
            "args": args,
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
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
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


async def test_credential_added_pin_with_data_pushes_known(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    lock_schlage_be469: Node,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """A credential event that carries the value pushes the readable state."""
    mock_coordinator = MagicMock()
    mock_coordinator.data = {}
    zwave_js_lock.coordinator = mock_coordinator

    zwave_js_lock.subscribe_push_updates()

    lock_schlage_be469.receive_event(
        _make_credential_added_event(
            lock_schlage_be469.node_id, credential_slot=2, credential_data="1234"
        )
    )
    await hass.async_block_till_done()

    mock_coordinator.push_update.assert_called_once_with(
        {2: SlotCredential.known("1234")}
    )

    zwave_js_lock.unsubscribe_push_updates()


async def test_credential_modified_pin_pushes_unreadable(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    lock_schlage_be469: Node,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
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
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
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
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """Test that 'credential added' for a non-PIN credential type is ignored."""
    mock_coordinator = MagicMock()
    mock_coordinator.data = {}
    zwave_js_lock.coordinator = mock_coordinator

    zwave_js_lock.subscribe_push_updates()

    lock_schlage_be469.receive_event(
        _make_credential_added_event(
            lock_schlage_be469.node_id,
            credential_slot=2,
            credential_type=UserCredentialType.PASSWORD,  # not PIN_CODE
        )
    )
    await hass.async_block_till_done()

    mock_coordinator.push_update.assert_not_called()

    zwave_js_lock.unsubscribe_push_updates()


async def test_credential_deleted_non_pin_ignored(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    lock_schlage_be469: Node,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
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
            credential_type=UserCredentialType.PASSWORD,  # not PIN_CODE
        )
    )
    await hass.async_block_till_done()

    mock_coordinator.push_update.assert_not_called()

    zwave_js_lock.unsubscribe_push_updates()


# ---------------------------------------------------------------------------
# User Code CC fallback push handling (issue #1251)
# ---------------------------------------------------------------------------


def _make_uc_value_event(
    node_id: int, property_name: str, code_slot: int, new_value
) -> ZwaveEvent:
    """Create a User Code CC value-updated ZwaveEvent."""
    return ZwaveEvent(
        type="value updated",
        data={
            "source": "node",
            "event": "value updated",
            "nodeId": node_id,
            "args": {
                "commandClass": CommandClass.USER_CODE,
                "property": property_name,
                "propertyKey": code_slot,
                "newValue": new_value,
            },
        },
    )


def _make_duplicate_code_event(node_id: int, user_id: int | None = None) -> ZwaveEvent:
    """Create a duplicate code notification ZwaveEvent."""
    params: dict = {}
    if user_id is not None:
        params["userId"] = user_id
    return ZwaveEvent(
        type="notification",
        data={
            "source": "node",
            "event": "notification",
            "nodeId": node_id,
            "endpointIndex": 0,
            "ccId": 113,
            "args": {
                "type": 6,  # ACCESS_CONTROL
                "event": 15,  # NEW_USER_CODE_NOT_ADDED_DUE_TO_DUPLICATE_CODE
                "label": "Access Control",
                "eventLabel": "New user code not added due to duplicate code",
                "parameters": params,
            },
        },
    )


async def test_subscribe_uc_mode_registers_value_listener_only(
    hass: HomeAssistant,
    uc_fallback_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """In UC-fallback mode, push uses one value-updated listener."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await uc_fallback_lock.async_setup_internal(lcm_entry)

    uc_fallback_lock.unsubscribe_push_updates()
    uc_fallback_lock.subscribe_push_updates()
    assert len(uc_fallback_lock._push_unsubs) == 1

    uc_fallback_lock.unsubscribe_push_updates()
    await uc_fallback_lock.async_unload(False)


async def test_subscribe_unknown_mode_registers_both_listener_sets(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """Before the capability probe runs, both listener sets are registered.

    The handlers are self-filtering and pushes are idempotent, so
    double-coverage is safe; it only happens when push subscription
    races ahead of the first capability probe.
    """
    zwave_js_lock.subscribe_push_updates()
    assert len(zwave_js_lock._push_unsubs) == 4

    zwave_js_lock.unsubscribe_push_updates()


async def test_uc_push_masked_code_sends_unreadable(
    hass: HomeAssistant,
    uc_fallback_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
    mock_uc_utils: dict,
) -> None:
    """A masked userCode value update pushes SlotCredential.unreadable()."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [uc_fallback_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await uc_fallback_lock.async_setup_internal(lcm_entry)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {}
    uc_fallback_lock.coordinator = mock_coordinator

    uc_fallback_lock.unsubscribe_push_updates()
    uc_fallback_lock.subscribe_push_updates()

    lock_schlage_be469.receive_event(
        _make_uc_value_event(lock_schlage_be469.node_id, "userCode", 2, "****")
    )
    await hass.async_block_till_done()

    mock_coordinator.push_update.assert_called_once_with(
        {2: SlotCredential.unreadable()}
    )

    uc_fallback_lock.unsubscribe_push_updates()
    await uc_fallback_lock.async_unload(False)


async def test_uc_push_status_available_ignored_when_pin_expected(
    hass: HomeAssistant,
    uc_fallback_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
    mock_uc_utils: dict,
) -> None:
    """A stale AVAILABLE status is ignored when LCM expects a PIN on the slot.

    Some locks send stale AVAILABLE events after a code was set; acting
    on them would cause infinite sync loops.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [uc_fallback_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await uc_fallback_lock.async_setup_internal(lcm_entry)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: SlotCredential.known("1234")}
    mock_coordinator.desired_credential.return_value = SlotCredential.known("1234")
    uc_fallback_lock.coordinator = mock_coordinator

    uc_fallback_lock.unsubscribe_push_updates()
    uc_fallback_lock.subscribe_push_updates()

    lock_schlage_be469.receive_event(
        _make_uc_value_event(
            lock_schlage_be469.node_id, "userIdStatus", 2, CodeSlotStatus.AVAILABLE
        )
    )
    await hass.async_block_till_done()

    mock_coordinator.push_update.assert_not_called()

    uc_fallback_lock.unsubscribe_push_updates()
    await uc_fallback_lock.async_unload(False)


async def test_uc_push_status_available_clears_slot_when_no_pin_expected(
    hass: HomeAssistant,
    uc_fallback_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
    mock_uc_utils: dict,
) -> None:
    """An AVAILABLE status pushes empty when LCM does not expect a PIN."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [uc_fallback_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await uc_fallback_lock.async_setup_internal(lcm_entry)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: SlotCredential.known("1234")}
    mock_coordinator.desired_credential.return_value = SlotCredential.empty()
    uc_fallback_lock.coordinator = mock_coordinator

    uc_fallback_lock.unsubscribe_push_updates()
    uc_fallback_lock.subscribe_push_updates()

    lock_schlage_be469.receive_event(
        _make_uc_value_event(
            lock_schlage_be469.node_id, "userIdStatus", 2, CodeSlotStatus.AVAILABLE
        )
    )
    await hass.async_block_till_done()

    mock_coordinator.push_update.assert_called_once_with({2: SlotCredential.empty()})

    uc_fallback_lock.unsubscribe_push_updates()
    await uc_fallback_lock.async_unload(False)


async def test_uc_duplicate_code_notification_marks_rejected(
    hass: HomeAssistant,
    uc_fallback_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
    mock_uc_utils: dict,
) -> None:
    """The duplicate-code notification marks the in-flight slot rejected."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await uc_fallback_lock.async_setup_internal(lcm_entry)

    uc_fallback_lock._set_in_progress_code_slot = 2
    lock_schlage_be469.receive_event(
        _make_duplicate_code_event(lock_schlage_be469.node_id, user_id=2)
    )
    await hass.async_block_till_done()

    assert 2 in uc_fallback_lock._rejected_code_slots
    assert uc_fallback_lock._set_in_progress_code_slot is None

    await uc_fallback_lock.async_unload(False)


async def test_uc_duplicate_code_notification_user_id_zero_marks_in_flight_slot(
    hass: HomeAssistant,
    uc_fallback_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
    mock_uc_utils: dict,
) -> None:
    """Firmwares reporting userId=0 still reject the slot LCM is setting."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await uc_fallback_lock.async_setup_internal(lcm_entry)

    uc_fallback_lock._set_in_progress_code_slot = 3
    lock_schlage_be469.receive_event(
        _make_duplicate_code_event(lock_schlage_be469.node_id, user_id=0)
    )
    await hass.async_block_till_done()

    assert 3 in uc_fallback_lock._rejected_code_slots
    assert uc_fallback_lock._set_in_progress_code_slot is None

    await uc_fallback_lock.async_unload(False)


async def test_uc_duplicate_code_notification_ignored_when_not_in_progress(
    hass: HomeAssistant,
    uc_fallback_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
    mock_uc_utils: dict,
) -> None:
    """Without an in-flight set, the notification is not attributed to any slot."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await uc_fallback_lock.async_setup_internal(lcm_entry)

    lock_schlage_be469.receive_event(
        _make_duplicate_code_event(lock_schlage_be469.node_id, user_id=2)
    )
    await hass.async_block_till_done()

    assert not uc_fallback_lock._rejected_code_slots

    await uc_fallback_lock.async_unload(False)


async def test_uc_set_in_progress_cleared_on_value_update(
    hass: HomeAssistant,
    uc_fallback_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
    mock_uc_utils: dict,
) -> None:
    """A userCode update for the in-flight slot closes the rejection window."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await uc_fallback_lock.async_setup_internal(lcm_entry)

    uc_fallback_lock.unsubscribe_push_updates()
    uc_fallback_lock.subscribe_push_updates()

    uc_fallback_lock._set_in_progress_code_slot = 2
    lock_schlage_be469.receive_event(
        _make_uc_value_event(lock_schlage_be469.node_id, "userCode", 2, "5678")
    )
    await hass.async_block_till_done()

    assert uc_fallback_lock._set_in_progress_code_slot is None

    uc_fallback_lock.unsubscribe_push_updates()
    await uc_fallback_lock.async_unload(False)
