"""Test Z-Wave JS event handling: push updates, notifications, and duplicate codes."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from zwave_js_server.const import CommandClass
from zwave_js_server.const.command_class.lock import (
    LOCK_USERCODE_PROPERTY,
    LOCK_USERCODE_STATUS_PROPERTY,
    CodeSlotStatus,
)
from zwave_js_server.event import Event as ZwaveEvent
from zwave_js_server.model.node import Node

from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN, STATE_ON
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.issue_registry import async_get as async_get_issue_registry

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
from custom_components.lock_code_manager.exceptions import DuplicateCodeError
from custom_components.lock_code_manager.models import (
    LockCodeManagerConfigEntryRuntimeData,
    SlotCode,
)
from custom_components.lock_code_manager.providers.zwave_js import ZWaveJSLock

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def async_capture_events(
    hass: HomeAssistant, event_name: str
) -> list[Event[dict[str, Any]]]:
    """Create a helper that captures events."""
    events: list[Event[dict[str, Any]]] = []

    @callback
    def capture_events(event: Event[dict[str, Any]]) -> None:
        events.append(event)

    hass.bus.async_listen(event_name, capture_events)
    return events


def _make_duplicate_code_event(node_id: int, user_id: int | None = None) -> ZwaveEvent:
    """Create a duplicate code notification ZwaveEvent."""
    params: dict[str, Any] = {}
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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def mock_get_usercode_from_node():
    """
    Mock get_usercode_from_node for all tests.

    V1 set/clear calls get_usercode_from_node to poll the slot from the device.
    In tests, the node doesn't have a real Z-Wave JS server connection, so we
    mock the function. Individual tests can access the mock via parameter name.
    """
    with patch(
        "custom_components.lock_code_manager.providers.zwave_js.get_usercode_from_node",
        new_callable=AsyncMock,
    ) as mock:
        yield mock


@pytest.fixture(name="zwave_js_lock")
async def zwave_js_lock_fixture(
    hass: HomeAssistant,
    zwave_integration: MockConfigEntry,
    lock_entity: er.RegistryEntry,
    lock_schlage_be469: Node,
) -> ZWaveJSLock:
    """Create a ZWaveJSLock instance for testing."""
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    return ZWaveJSLock(
        hass=hass,
        dev_reg=dev_reg,
        ent_reg=ent_reg,
        lock_config_entry=zwave_integration,
        lock=lock_entity,
    )


@pytest.fixture
def mock_zwave_usercodes(zwave_client: MagicMock):
    """
    Mock Z-Wave JS usercode functions with mutable state.

    Both ``get_usercodes`` and ``get_usercode`` read from a shared mutable
    ``codes`` dict.  The client's ``async_send_command`` is wrapped so that
    set / clear Z-Wave commands automatically update ``codes``, preventing
    the coordinator refresh from overwriting optimistic push updates with
    stale data.

    Yields ``(mock_get_usercodes, mock_get_usercode, codes)`` where *codes*
    is ``dict[int, dict]`` keyed by slot number.
    """
    codes: dict[int, dict] = {}

    original_side_effect = zwave_client.async_send_command.side_effect

    async def _send_command_with_codes(message, require_schema=None):
        if message.get("command") == "node.set_value":
            vid = message.get("valueId", {})
            if vid.get("commandClass") == CommandClass.USER_CODE:
                slot = vid.get("propertyKey")
                if slot is not None:
                    prop = vid.get("property")
                    if prop == "userIdStatus":
                        # Clear operation
                        codes[slot] = {
                            "code_slot": slot,
                            "in_use": False,
                            "usercode": "",
                        }
                    elif prop == "userCode":
                        # Set operation
                        codes[slot] = {
                            "code_slot": slot,
                            "in_use": True,
                            "usercode": str(message["value"]),
                        }
        return await original_side_effect(message, require_schema)

    with (
        patch(
            "custom_components.lock_code_manager.providers.zwave_js.get_usercodes",
        ) as mock_all,
        patch(
            "custom_components.lock_code_manager.providers.zwave_js.get_usercode",
        ) as mock_one,
    ):
        mock_all.side_effect = lambda node: list(codes.values())
        mock_one.side_effect = lambda node, slot: codes.get(
            slot, {"code_slot": slot, "in_use": False, "usercode": ""}
        )
        zwave_client.async_send_command.side_effect = _send_command_with_codes
        yield mock_all, mock_one, codes
        zwave_client.async_send_command.side_effect = original_side_effect


async def _setup_lcm_entry(
    hass: HomeAssistant,
    lock_entity_id: str,
    slots: dict[str, dict],
    mock_zwave_usercodes: tuple[MagicMock, MagicMock, dict[int, dict]],
) -> MockConfigEntry:
    """Set up a full LCM config entry with real platform entities."""
    _mock_all, _mock_one, codes = mock_zwave_usercodes

    for k, v in slots.items():
        pin = v.get(CONF_PIN, "")
        in_use = bool(pin)
        codes[int(k)] = {"code_slot": int(k), "usercode": pin, "in_use": in_use}

    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [lock_entity_id],
            CONF_SLOTS: slots,
        },
        unique_id=f"duplicate_code_test_{lock_entity_id}",
    )
    lcm_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(lcm_entry.entry_id)
    await hass.async_block_till_done()
    return lcm_entry


def _get_enabled_switch_entity_id(hass: HomeAssistant, entry_id: str, slot: int) -> str:
    """Get the enabled switch entity ID for a slot."""
    ent_reg = er.async_get(hass)
    uid = f"{entry_id}|{slot}|{CONF_ENABLED}"
    entity_id = ent_reg.async_get_entity_id("switch", DOMAIN, uid)
    assert entity_id, f"Switch entity not found for slot {slot}"
    return entity_id


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

    assert zwave_js_lock._value_update_unsub is not None

    zwave_js_lock.unsubscribe_push_updates()
    assert zwave_js_lock._value_update_unsub is None

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
    first_unsub = zwave_js_lock._value_update_unsub

    zwave_js_lock.subscribe_push_updates()
    assert zwave_js_lock._value_update_unsub is first_unsub

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
    assert zwave_js_lock._value_update_unsub is None

    # Make client appear not ready — should not crash
    with patch.object(
        zwave_js_lock, "_get_client_state", return_value=(False, "not connected")
    ):
        zwave_js_lock.subscribe_push_updates()

    # Should not have subscribed (no crash, no retry timer)
    assert zwave_js_lock._value_update_unsub is None

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
    assert zwave_js_lock._value_update_unsub is None

    # Make node.on raise — should not crash
    with patch.object(lock_schlage_be469, "on", side_effect=ValueError("not ready")):
        zwave_js_lock.subscribe_push_updates()

    assert zwave_js_lock._value_update_unsub is None

    await zwave_js_lock.async_unload(False)


# ---------------------------------------------------------------------------
# Event filter tests
# ---------------------------------------------------------------------------


async def test_event_filter_matches_correct_node(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that event filter matches events for the correct node."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

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

    await zwave_js_lock.async_unload(False)


# ---------------------------------------------------------------------------
# Notification event tests
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
# Push value update tests
# ---------------------------------------------------------------------------


async def test_push_update_masked_code_sends_unknown(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """
    Test that push updates with masked codes send SlotCode.UNREADABLE_CODE.

    When a push update arrives with a masked code (all asterisks) and the slot
    is in use, the coordinator should receive SlotCode.UNREADABLE_CODE.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    # Set up a mock coordinator (push_update is synchronous)
    mock_coordinator = MagicMock()
    mock_coordinator.data = {}
    zwave_js_lock.coordinator = mock_coordinator

    # Subscribe to push updates
    zwave_js_lock.subscribe_push_updates()

    # Mock code_slot_in_use to return True (slot has a code)
    with patch.object(zwave_js_lock, "code_slot_in_use", return_value=True):
        # Simulate a value update event with masked code
        event = ZwaveEvent(
            type="value updated",
            data={
                "source": "node",
                "event": "value updated",
                "nodeId": lock_schlage_be469.node_id,
                "args": {
                    "commandClass": CommandClass.USER_CODE,
                    "property": "userCode",
                    "propertyKey": 2,
                    "newValue": "****",
                },
            },
        )
        lock_schlage_be469.receive_event(event)
        await hass.async_block_till_done()

        # Coordinator should receive SlotCode.UNREADABLE_CODE for masked codes
        mock_coordinator.push_update.assert_called_once_with(
            {2: SlotCode.UNREADABLE_CODE}
        )


async def test_push_update_falsy_value_sends_empty(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that push updates with falsy values send SlotCode.EMPTY."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: "1234"}
    zwave_js_lock.coordinator = mock_coordinator
    zwave_js_lock.subscribe_push_updates()

    # Simulate a value update with empty string (falsy)
    event = ZwaveEvent(
        type="value updated",
        data={
            "source": "node",
            "event": "value updated",
            "nodeId": lock_schlage_be469.node_id,
            "args": {
                "commandClass": CommandClass.USER_CODE,
                "property": "userCode",
                "propertyKey": 2,
                "newValue": "",
            },
        },
    )
    lock_schlage_be469.receive_event(event)
    await hass.async_block_till_done()

    mock_coordinator.push_update.assert_called_once_with({2: SlotCode.EMPTY})


async def test_push_update_all_zeros_not_in_use_sends_empty(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that all-zeros with slot_in_use=False sends SlotCode.EMPTY."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: "1234"}
    zwave_js_lock.coordinator = mock_coordinator
    zwave_js_lock.subscribe_push_updates()

    with patch.object(zwave_js_lock, "code_slot_in_use", return_value=False):
        event = ZwaveEvent(
            type="value updated",
            data={
                "source": "node",
                "event": "value updated",
                "nodeId": lock_schlage_be469.node_id,
                "args": {
                    "commandClass": CommandClass.USER_CODE,
                    "property": "userCode",
                    "propertyKey": 2,
                    "newValue": "0000",
                },
            },
        )
        lock_schlage_be469.receive_event(event)
        await hass.async_block_till_done()

        mock_coordinator.push_update.assert_called_once_with({2: SlotCode.EMPTY})


async def test_push_update_duplicate_value_skipped(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that duplicate push updates are silently skipped."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: "1234"}  # Already has this value
    zwave_js_lock.coordinator = mock_coordinator
    zwave_js_lock.subscribe_push_updates()

    with patch.object(zwave_js_lock, "code_slot_in_use", return_value=True):
        event = ZwaveEvent(
            type="value updated",
            data={
                "source": "node",
                "event": "value updated",
                "nodeId": lock_schlage_be469.node_id,
                "args": {
                    "commandClass": CommandClass.USER_CODE,
                    "property": "userCode",
                    "propertyKey": 2,
                    "newValue": "1234",
                },
            },
        )
        lock_schlage_be469.receive_event(event)
        await hass.async_block_till_done()

        # push_update should NOT be called (duplicate)
        mock_coordinator.push_update.assert_not_called()


async def test_push_update_masked_code_with_unknown_in_use_sends_unknown(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that masked codes with slot_in_use=None still send UNREADABLE_CODE."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {}
    zwave_js_lock.coordinator = mock_coordinator
    zwave_js_lock.subscribe_push_updates()

    # slot_in_use returns None (indeterminate) — should still treat masked as UNREADABLE_CODE
    with patch.object(zwave_js_lock, "code_slot_in_use", return_value=None):
        event = ZwaveEvent(
            type="value updated",
            data={
                "source": "node",
                "event": "value updated",
                "nodeId": lock_schlage_be469.node_id,
                "args": {
                    "commandClass": CommandClass.USER_CODE,
                    "property": "userCode",
                    "propertyKey": 2,
                    "newValue": "****",
                },
            },
        )
        lock_schlage_be469.receive_event(event)
        await hass.async_block_till_done()

        mock_coordinator.push_update.assert_called_once_with(
            {2: SlotCode.UNREADABLE_CODE}
        )


# ---------------------------------------------------------------------------
# userIdStatus push update tests
# ---------------------------------------------------------------------------


async def test_push_update_user_id_status_available_clears_slot(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """
    Test that userIdStatus=AVAILABLE push update clears the slot.

    When the lock sends a userIdStatus update with AVAILABLE status,
    it means the slot has been cleared. This should update the coordinator
    to mark the slot as empty.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    # Set up a mock coordinator with existing data
    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: "1234"}  # Slot has a PIN
    mock_coordinator.slot_expects_pin.return_value = False  # No expected PIN
    zwave_js_lock.coordinator = mock_coordinator

    # Subscribe to push updates
    zwave_js_lock.subscribe_push_updates()

    # Simulate userIdStatus=AVAILABLE event (slot cleared)
    event = ZwaveEvent(
        type="value updated",
        data={
            "args": {
                "commandClass": CommandClass.USER_CODE,
                "property": LOCK_USERCODE_STATUS_PROPERTY,
                "propertyKey": 2,
                "newValue": CodeSlotStatus.AVAILABLE,
            },
        },
    )
    lock_schlage_be469.emit("value updated", event.data)
    await hass.async_block_till_done()

    # Coordinator should be updated with SlotCode.EMPTY
    mock_coordinator.push_update.assert_called_once_with({2: SlotCode.EMPTY})

    zwave_js_lock.unsubscribe_push_updates()
    await zwave_js_lock.async_unload(False)


async def test_push_update_user_id_status_available_skipped_when_already_empty(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """
    Test that userIdStatus=AVAILABLE is skipped when slot already empty.

    If the coordinator already shows the slot as empty, we shouldn't
    push another update.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    # Set up a mock coordinator - slot already empty
    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: SlotCode.EMPTY}  # Slot already empty
    zwave_js_lock.coordinator = mock_coordinator

    # Subscribe to push updates
    zwave_js_lock.subscribe_push_updates()

    # Simulate userIdStatus=AVAILABLE event
    event = ZwaveEvent(
        type="value updated",
        data={
            "args": {
                "commandClass": CommandClass.USER_CODE,
                "property": LOCK_USERCODE_STATUS_PROPERTY,
                "propertyKey": 2,
                "newValue": CodeSlotStatus.AVAILABLE,
            },
        },
    )
    lock_schlage_be469.emit("value updated", event.data)
    await hass.async_block_till_done()

    # Coordinator should NOT be updated (already empty)
    mock_coordinator.push_update.assert_not_called()

    zwave_js_lock.unsubscribe_push_updates()
    await zwave_js_lock.async_unload(False)


async def test_push_update_user_id_status_enabled_ignored(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """
    Test that userIdStatus=ENABLED push updates are ignored.

    We only care about AVAILABLE status for clearing slots.
    ENABLED status doesn't tell us the PIN value.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    # Set up a mock coordinator
    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: ""}
    zwave_js_lock.coordinator = mock_coordinator

    # Subscribe to push updates
    zwave_js_lock.subscribe_push_updates()

    # Simulate userIdStatus=ENABLED event
    event = ZwaveEvent(
        type="value updated",
        data={
            "args": {
                "commandClass": CommandClass.USER_CODE,
                "property": LOCK_USERCODE_STATUS_PROPERTY,
                "propertyKey": 2,
                "newValue": CodeSlotStatus.ENABLED,
            },
        },
    )
    lock_schlage_be469.emit("value updated", event.data)
    await hass.async_block_till_done()

    # Coordinator should NOT be updated
    mock_coordinator.push_update.assert_not_called()

    zwave_js_lock.unsubscribe_push_updates()
    await zwave_js_lock.async_unload(False)


async def test_push_update_user_id_status_available_ignored_when_slot_expects_pin(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """
    Test that userIdStatus=AVAILABLE is ignored when slot expects a PIN.

    This prevents sync loops where the lock sends stale AVAILABLE status
    after a code was successfully set.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    # Set up a mock coordinator with existing PIN
    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: "1234"}  # Slot has a PIN
    zwave_js_lock.coordinator = mock_coordinator

    # Subscribe to push updates
    zwave_js_lock.subscribe_push_updates()

    # Mock _slot_expects_pin to return True (LCM expects a PIN on this slot)
    with patch.object(zwave_js_lock, "_slot_expects_pin", return_value=True):
        # Simulate userIdStatus=AVAILABLE event (stale status from lock)
        event = ZwaveEvent(
            type="value updated",
            data={
                "args": {
                    "commandClass": CommandClass.USER_CODE,
                    "property": LOCK_USERCODE_STATUS_PROPERTY,
                    "propertyKey": 2,
                    "newValue": CodeSlotStatus.AVAILABLE,
                },
            },
        )
        lock_schlage_be469.emit("value updated", event.data)
        await hass.async_block_till_done()

        # Coordinator should NOT be updated (AVAILABLE ignored)
        mock_coordinator.push_update.assert_not_called()

    zwave_js_lock.unsubscribe_push_updates()
    await zwave_js_lock.async_unload(False)


async def test_push_update_user_id_status_available_clears_when_slot_inactive(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """
    Test that userIdStatus=AVAILABLE clears slot when LCM doesn't expect a PIN.

    When the slot is inactive (active=OFF), AVAILABLE status should clear
    the coordinator as expected.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    # Set up a mock coordinator with existing PIN
    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: "1234"}  # Slot has a PIN
    zwave_js_lock.coordinator = mock_coordinator

    # Subscribe to push updates
    zwave_js_lock.subscribe_push_updates()

    # Mock _slot_expects_pin to return False (slot is inactive)
    with patch.object(zwave_js_lock, "_slot_expects_pin", return_value=False):
        # Simulate userIdStatus=AVAILABLE event
        event = ZwaveEvent(
            type="value updated",
            data={
                "args": {
                    "commandClass": CommandClass.USER_CODE,
                    "property": LOCK_USERCODE_STATUS_PROPERTY,
                    "propertyKey": 2,
                    "newValue": CodeSlotStatus.AVAILABLE,
                },
            },
        )
        lock_schlage_be469.emit("value updated", event.data)
        await hass.async_block_till_done()

        # Coordinator SHOULD be updated (slot cleared)
        mock_coordinator.push_update.assert_called_once_with({2: SlotCode.EMPTY})

    zwave_js_lock.unsubscribe_push_updates()
    await zwave_js_lock.async_unload(False)


# ---------------------------------------------------------------------------
# Duplicate code notification tests
# ---------------------------------------------------------------------------


async def test_duplicate_code_notification_marks_rejected(
    hass: HomeAssistant,
    zwave_integration: MockConfigEntry,
    lock_entity: er.RegistryEntry,
    lock_schlage_be469: Node,
    mock_zwave_usercodes: tuple[MagicMock, MagicMock, dict[int, dict]],
) -> None:
    """Test that event 15 marks the slot rejected and clears in-progress."""
    lcm_entry = await _setup_lcm_entry(
        hass,
        lock_entity.entity_id,
        {"2": {CONF_NAME: "test", CONF_PIN: "1234", CONF_ENABLED: True}},
        mock_zwave_usercodes,
    )

    # Find the ZWaveJSLock instance created by LCM setup
    runtime_data: LockCodeManagerConfigEntryRuntimeData = lcm_entry.runtime_data
    lock_instance = runtime_data.locks[lock_entity.entity_id]
    lock_instance._set_in_progress_code_slot = 2

    lock_schlage_be469.receive_event(
        _make_duplicate_code_event(lock_schlage_be469.node_id, user_id=2)
    )
    await hass.async_block_till_done()

    # Slot should be marked as rejected for the sync manager to pick up
    assert 2 in lock_instance._rejected_code_slots

    # In-progress field should be cleared
    assert lock_instance._set_in_progress_code_slot is None

    await hass.config_entries.async_unload(lcm_entry.entry_id)


async def test_duplicate_code_notification_no_user_id_marks_rejected(
    hass: HomeAssistant,
    zwave_integration: MockConfigEntry,
    lock_entity: er.RegistryEntry,
    lock_schlage_be469: Node,
    mock_zwave_usercodes: tuple[MagicMock, MagicMock, dict[int, dict]],
) -> None:
    """Test event 15 with no userId in params still marks rejected using in-progress slot."""
    lcm_entry = await _setup_lcm_entry(
        hass,
        lock_entity.entity_id,
        {"3": {CONF_NAME: "test", CONF_PIN: "1234", CONF_ENABLED: True}},
        mock_zwave_usercodes,
    )

    runtime_data: LockCodeManagerConfigEntryRuntimeData = lcm_entry.runtime_data
    lock_instance = runtime_data.locks[lock_entity.entity_id]
    lock_instance._set_in_progress_code_slot = 3

    lock_schlage_be469.receive_event(
        _make_duplicate_code_event(lock_schlage_be469.node_id)
    )
    await hass.async_block_till_done()

    assert 3 in lock_instance._rejected_code_slots
    assert lock_instance._set_in_progress_code_slot is None

    await hass.config_entries.async_unload(lcm_entry.entry_id)


async def test_duplicate_code_notification_ignored_when_not_in_progress(
    hass: HomeAssistant,
    zwave_integration: MockConfigEntry,
    lock_entity: er.RegistryEntry,
    lock_schlage_be469: Node,
    mock_zwave_usercodes: tuple[MagicMock, MagicMock, dict[int, dict]],
) -> None:
    """Test event 15 is ignored when _set_in_progress_code_slot is None."""
    lcm_entry = await _setup_lcm_entry(
        hass,
        lock_entity.entity_id,
        {"2": {CONF_NAME: "test", CONF_PIN: "1234", CONF_ENABLED: True}},
        mock_zwave_usercodes,
    )
    switch_entity_id = _get_enabled_switch_entity_id(hass, lcm_entry.entry_id, 2)
    assert hass.states.get(switch_entity_id).state == STATE_ON

    # Do NOT set _set_in_progress_code_slot (external trigger)
    lock_schlage_be469.receive_event(
        _make_duplicate_code_event(lock_schlage_be469.node_id, user_id=2)
    )
    await hass.async_block_till_done()

    # Switch should still be on
    assert hass.states.get(switch_entity_id).state == STATE_ON

    # No repair issue created for slot_disabled
    issue_registry = async_get_issue_registry(hass)
    matching_issues = [
        issue
        for issue in issue_registry.issues.values()
        if issue.domain == DOMAIN and issue.issue_id.startswith("slot_disabled_")
    ]
    assert len(matching_issues) == 0

    await hass.config_entries.async_unload(lcm_entry.entry_id)


async def test_duplicate_code_notification_ignored_when_user_id_mismatches(
    hass: HomeAssistant,
    zwave_integration: MockConfigEntry,
    lock_entity: er.RegistryEntry,
    lock_schlage_be469: Node,
    mock_zwave_usercodes: tuple[MagicMock, MagicMock, dict[int, dict]],
) -> None:
    """Test event 15 is ignored when userId doesn't match in-progress slot."""
    lcm_entry = await _setup_lcm_entry(
        hass,
        lock_entity.entity_id,
        {
            "2": {CONF_NAME: "test2", CONF_PIN: "1234", CONF_ENABLED: True},
            "3": {CONF_NAME: "test3", CONF_PIN: "5678", CONF_ENABLED: True},
        },
        mock_zwave_usercodes,
    )
    switch_2 = _get_enabled_switch_entity_id(hass, lcm_entry.entry_id, 2)
    switch_3 = _get_enabled_switch_entity_id(hass, lcm_entry.entry_id, 3)

    runtime_data: LockCodeManagerConfigEntryRuntimeData = lcm_entry.runtime_data
    lock_instance = runtime_data.locks[lock_entity.entity_id]

    # LCM is setting slot 2, but notification says slot 3
    lock_instance._set_in_progress_code_slot = 2

    lock_schlage_be469.receive_event(
        _make_duplicate_code_event(lock_schlage_be469.node_id, user_id=3)
    )
    await hass.async_block_till_done()

    # Neither switch should be turned off
    assert hass.states.get(switch_2).state == STATE_ON
    assert hass.states.get(switch_3).state == STATE_ON

    # In-progress should NOT be cleared (it wasn't our event)
    assert lock_instance._set_in_progress_code_slot == 2

    await hass.config_entries.async_unload(lcm_entry.entry_id)


# ---------------------------------------------------------------------------
# In-progress tracking tests
# ---------------------------------------------------------------------------


async def test_set_in_progress_cleared_on_value_update(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that a push value update for the in-progress slot clears the field."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: ""}
    zwave_js_lock.coordinator = mock_coordinator

    zwave_js_lock.subscribe_push_updates()

    # Simulate LCM setting a code on slot 2
    zwave_js_lock._set_in_progress_code_slot = 2

    # Simulate a push value update for slot 2 (lock accepted the code)
    event = ZwaveEvent(
        type="value updated",
        data={
            "args": {
                "commandClass": CommandClass.USER_CODE,
                "property": LOCK_USERCODE_PROPERTY,
                "propertyKey": 2,
                "newValue": "1234",
            },
        },
    )
    lock_schlage_be469.emit("value updated", event.data)
    await hass.async_block_till_done()

    # In-progress should be cleared
    assert zwave_js_lock._set_in_progress_code_slot is None

    zwave_js_lock.unsubscribe_push_updates()
    await zwave_js_lock.async_unload(False)


async def test_internal_set_usercode_raises_duplicate_for_rejected_slot(
    hass: HomeAssistant,
    zwave_integration: MockConfigEntry,
    lock_entity: er.RegistryEntry,
    lock_schlage_be469: Node,
    mock_zwave_usercodes: tuple[MagicMock, MagicMock, dict[int, dict]],
) -> None:
    """
    Test async_internal_set_usercode raises DuplicateCodeError for rejected slots.

    When a slot is in _rejected_code_slots (marked by event 15), the next call to
    async_internal_set_usercode should raise DuplicateCodeError without calling the
    provider's async_set_usercode, and clear the slot from the rejected set.
    """
    lcm_entry = await _setup_lcm_entry(
        hass,
        lock_entity.entity_id,
        {"2": {CONF_NAME: "test", CONF_PIN: "1234", CONF_ENABLED: True}},
        mock_zwave_usercodes,
    )

    runtime_data: LockCodeManagerConfigEntryRuntimeData = lcm_entry.runtime_data
    lock_instance = runtime_data.locks[lock_entity.entity_id]

    # Mark slot 2 as rejected (simulating event 15)
    lock_instance.mark_code_rejected(2)
    assert 2 in lock_instance._rejected_code_slots

    # Attempt to set usercode should raise DuplicateCodeError
    with pytest.raises(DuplicateCodeError) as exc_info:
        await lock_instance.async_internal_set_usercode(2, "1234", source="sync")

    assert exc_info.value.code_slot == 2
    assert exc_info.value.conflicting_slot is None
    assert "duplicate detected by lock firmware" in str(exc_info.value)

    # Slot should be cleared from the rejected set
    assert 2 not in lock_instance._rejected_code_slots

    await hass.config_entries.async_unload(lcm_entry.entry_id)
