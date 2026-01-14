"""Test the Z-Wave JS lock provider."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from zwave_js_server.const import CommandClass
from zwave_js_server.event import Event as ZwaveEvent
from zwave_js_server.model.node import Node

from homeassistant.components.zwave_js.const import DOMAIN as ZWAVE_JS_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

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
from custom_components.lock_code_manager.exceptions import LockDisconnected
from custom_components.lock_code_manager.providers.zwave_js import ZWaveJSLock


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

    lock = ZWaveJSLock(
        hass=hass,
        dev_reg=dev_reg,
        ent_reg=ent_reg,
        lock_config_entry=zwave_integration,
        lock=lock_entity,
    )
    return lock


# Properties tests


async def test_domain(zwave_js_lock: ZWaveJSLock) -> None:
    """Test domain property returns zwave_js."""
    assert zwave_js_lock.domain == ZWAVE_JS_DOMAIN


async def test_supports_push(zwave_js_lock: ZWaveJSLock) -> None:
    """Test that Z-Wave JS locks support push updates."""
    assert zwave_js_lock.supports_push is True


async def test_connection_check_interval_is_none(zwave_js_lock: ZWaveJSLock) -> None:
    """Test that connection check interval is None (uses config entry state)."""
    assert zwave_js_lock.connection_check_interval is None


async def test_node_property(
    zwave_js_lock: ZWaveJSLock,
    lock_schlage_be469: Node,
) -> None:
    """Test node property returns the correct Z-Wave node."""
    node = zwave_js_lock.node
    assert node.node_id == lock_schlage_be469.node_id


# Connection tests


async def test_is_connection_up_when_loaded(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test connection is up when config entry is loaded and client connected."""
    assert zwave_integration.state == ConfigEntryState.LOADED
    assert await zwave_js_lock.async_is_connection_up() is True


async def test_is_connection_down_when_not_loaded(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test connection is down when config entry not loaded."""
    await hass.config_entries.async_unload(zwave_integration.entry_id)
    await hass.async_block_till_done()

    assert zwave_integration.state != ConfigEntryState.LOADED
    assert await zwave_js_lock.async_is_connection_up() is False


# Usercode tests


async def test_get_usercodes_from_cache(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test reading usercodes from the Z-Wave JS value cache."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"1": {}, "2": {}, "3": {}},
        },
    )
    lcm_entry.add_to_hass(hass)

    await zwave_js_lock.async_setup(lcm_entry)

    # Slot 1: "9999" (in_use=True)
    # Slot 2: "1234" (in_use=True)
    # Slot 3: empty (in_use=False)
    codes = await zwave_js_lock.async_get_usercodes()

    assert codes[1] == "9999"
    assert codes[2] == "1234"
    assert codes[3] == ""

    await zwave_js_lock.async_unload(False)


async def test_set_usercode_calls_service(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that set_usercode calls the Z-Wave JS service."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    with patch.object(
        zwave_js_lock, "async_call_service", new_callable=AsyncMock
    ) as mock_service:
        result = await zwave_js_lock.async_set_usercode(4, "5678", "Test User")

        assert result is True
        mock_service.assert_called_once_with(
            ZWAVE_JS_DOMAIN,
            "set_lock_usercode",
            {
                "entity_id": zwave_js_lock.lock.entity_id,
                "code_slot": 4,
                "usercode": "5678",
            },
        )

    await zwave_js_lock.async_unload(False)


async def test_set_usercode_skips_when_unchanged(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that set_usercode returns False when code is already set."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    with patch.object(
        zwave_js_lock, "async_call_service", new_callable=AsyncMock
    ) as mock_service:
        # Slot 2 already has "1234" in the fixture
        result = await zwave_js_lock.async_set_usercode(2, "1234", "Test User")

        assert result is False
        mock_service.assert_not_called()

    await zwave_js_lock.async_unload(False)


async def test_clear_usercode_calls_service(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that clear_usercode calls the Z-Wave JS service."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    with patch.object(
        zwave_js_lock, "async_call_service", new_callable=AsyncMock
    ) as mock_service:
        result = await zwave_js_lock.async_clear_usercode(2)

        assert result is True
        mock_service.assert_called_once_with(
            ZWAVE_JS_DOMAIN,
            "clear_lock_usercode",
            {
                "entity_id": zwave_js_lock.lock.entity_id,
                "code_slot": 2,
            },
        )

    await zwave_js_lock.async_unload(False)


async def test_clear_usercode_skips_when_already_cleared(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that clear_usercode returns False when slot is already empty."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    with patch.object(
        zwave_js_lock, "async_call_service", new_callable=AsyncMock
    ) as mock_service:
        # Slot 3 is already empty in the fixture
        result = await zwave_js_lock.async_clear_usercode(3)

        assert result is False
        mock_service.assert_not_called()

    await zwave_js_lock.async_unload(False)


# Push updates tests


async def test_subscribe_push_updates(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test subscribing to push updates."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

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
    await zwave_js_lock.async_setup(lcm_entry)

    zwave_js_lock.subscribe_push_updates()
    first_unsub = zwave_js_lock._value_update_unsub

    zwave_js_lock.subscribe_push_updates()
    assert zwave_js_lock._value_update_unsub is first_unsub

    zwave_js_lock.unsubscribe_push_updates()
    await zwave_js_lock.async_unload(False)


# Event filter tests


async def test_event_filter_matches_correct_node(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that event filter matches events for the correct node."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

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


# Setup/unload tests


async def test_setup_registers_event_listener(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that setup registers an event listener for Z-Wave JS events."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)

    assert len(zwave_js_lock._listeners) == 0

    await zwave_js_lock.async_setup(lcm_entry)

    assert len(zwave_js_lock._listeners) == 1

    await zwave_js_lock.async_unload(False)

    assert len(zwave_js_lock._listeners) == 0


async def test_unload_cleans_up_push_subscription(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that unload cleans up push subscriptions."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    zwave_js_lock.subscribe_push_updates()
    assert zwave_js_lock._value_update_unsub is not None

    await zwave_js_lock.async_unload(False)
    assert zwave_js_lock._value_update_unsub is None


# Hard refresh tests


async def test_hard_refresh_calls_refresh_cc_values(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that hard refresh calls the node's refresh method."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"1": {}, "2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    with patch.object(
        lock_schlage_be469,
        "async_refresh_cc_values",
        new_callable=AsyncMock,
    ) as mock_refresh:
        codes = await zwave_js_lock.async_hard_refresh_codes()

        mock_refresh.assert_called_once_with(CommandClass.USER_CODE)
        assert isinstance(codes, dict)

    await zwave_js_lock.async_unload(False)


# Notification event tests


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


async def test_notification_event_keypad_lock_fires_lock_state_changed(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that a keypad lock notification event fires EVENT_LOCK_STATE_CHANGED."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Capture LCM lock state changed events
    events = async_capture_events(hass, EVENT_LOCK_STATE_CHANGED)

    # Create a notification event matching the pattern from HA core tests
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
    await zwave_js_lock.async_setup(lcm_entry)

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


# Masked usercode tests


async def test_get_usercodes_masked_pin_unmanaged_slot_skipped(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test mixed slots: managed with real code vs unmanaged with masked code.

    This test verifies behavior when the lock cache contains:
    - Slot 1: Managed by LCM, has real code "9999" -> should be returned
    - Slot 5: NOT managed by LCM, has masked code "****" -> should be skipped

    Unmanaged slots with masked PINs can't be resolved since there's no LCM
    config entry to look up the expected PIN, so they're skipped entirely.
    """
    # Configure LCM to only manage slot 1 (not slot 5)
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"1": {}},  # Only slot 1 is managed
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Mock the cache to include a slot with a masked PIN that isn't managed by LCM
    masked_slots = [
        {"code_slot": 1, "usercode": "9999", "in_use": True},  # Managed by LCM
        {"code_slot": 5, "usercode": "****", "in_use": True},  # NOT managed, masked
    ]

    with patch.object(
        zwave_js_lock, "_get_usercodes_from_cache", return_value=masked_slots
    ):
        codes = await zwave_js_lock.async_get_usercodes()

        # Slot 1 should have its code
        assert codes[1] == "9999"
        # Slot 5 should be skipped entirely (not in result) since it's masked
        # and can't be resolved
        assert 5 not in codes

    await zwave_js_lock.async_unload(False)


async def test_get_usercodes_masked_pin_resolved_when_active(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that masked PINs are resolved to the configured PIN when slot is active.

    When active=ON and the PIN entity has a valid numeric PIN, the masked code
    should be resolved to that PIN value.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Mock the cache to have a masked PIN on a managed slot
    masked_slots = [
        {"code_slot": 2, "usercode": "****", "in_use": True},
    ]

    # Mock _resolve_masked_code to return the expected PIN
    with (
        patch.object(
            zwave_js_lock, "_get_usercodes_from_cache", return_value=masked_slots
        ),
        patch.object(zwave_js_lock, "_resolve_masked_code", return_value="5678"),
    ):
        codes = await zwave_js_lock.async_get_usercodes()

        # Masked PIN should be resolved to the configured PIN
        assert codes[2] == "5678"

    await zwave_js_lock.async_unload(False)


async def test_get_usercodes_masked_pin_skipped_when_inactive(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that masked PINs are skipped when slot is inactive (active=OFF).

    When the slot is managed but active=OFF (or entities not ready), the masked
    code cannot be resolved and the slot is skipped entirely.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Mock the cache to have a masked PIN on a managed slot
    masked_slots = [
        {"code_slot": 2, "usercode": "****", "in_use": True},
    ]

    # Mock _resolve_masked_code to return None (can't resolve - slot inactive)
    with (
        patch.object(
            zwave_js_lock, "_get_usercodes_from_cache", return_value=masked_slots
        ),
        patch.object(zwave_js_lock, "_resolve_masked_code", return_value=None),
    ):
        codes = await zwave_js_lock.async_get_usercodes()

        # Slot should be skipped entirely since it can't be resolved
        assert 2 not in codes

    await zwave_js_lock.async_unload(False)


async def test_is_masked_code_detection(zwave_js_lock: ZWaveJSLock) -> None:
    """Test _is_masked_code helper correctly identifies masked codes."""
    # Masked codes (all asterisks)
    assert zwave_js_lock._is_masked_code("****") is True
    assert zwave_js_lock._is_masked_code("******") is True
    assert zwave_js_lock._is_masked_code("*") is True

    # Not masked codes
    assert zwave_js_lock._is_masked_code("") is False
    assert zwave_js_lock._is_masked_code("1234") is False
    assert zwave_js_lock._is_masked_code("***1") is False
    assert zwave_js_lock._is_masked_code("1***") is False
    assert zwave_js_lock._is_masked_code("12*4") is False


async def test_push_update_masked_code_resolved(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that push updates with masked codes resolve and update coordinator.

    When a push update arrives with a masked code and it can be resolved,
    the resolved PIN should be pushed to the coordinator.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Set up a mock coordinator (push_update is synchronous)
    mock_coordinator = MagicMock()
    mock_coordinator.data = {}
    zwave_js_lock.coordinator = mock_coordinator

    # Subscribe to push updates
    zwave_js_lock.subscribe_push_updates()

    # Mock _resolve_masked_code to return a PIN
    with patch.object(zwave_js_lock, "_resolve_masked_code", return_value="9876"):
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

        # Coordinator should receive the resolved PIN, not the masked value
        mock_coordinator.push_update.assert_called_once_with({2: "9876"})

    zwave_js_lock.unsubscribe_push_updates()
    await zwave_js_lock.async_unload(False)


async def test_push_update_masked_code_skipped_when_unresolvable(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that push updates with unresolvable masked codes are skipped.

    When a push update arrives with a masked code that cannot be resolved,
    the update should be skipped entirely to prevent infinite sync loops.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Set up a mock coordinator (push_update is synchronous)
    mock_coordinator = MagicMock()
    mock_coordinator.data = {}
    zwave_js_lock.coordinator = mock_coordinator

    # Subscribe to push updates
    zwave_js_lock.subscribe_push_updates()

    # Mock _resolve_masked_code to return None (can't resolve)
    with patch.object(zwave_js_lock, "_resolve_masked_code", return_value=None):
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

        # Coordinator should NOT receive any update
        mock_coordinator.push_update.assert_not_called()

    zwave_js_lock.unsubscribe_push_updates()
    await zwave_js_lock.async_unload(False)


# Integration tests for _resolve_masked_code (without mocking)


async def test_resolve_masked_code_returns_pin_when_active(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test _resolve_masked_code returns PIN when slot is active with valid PIN.

    This integration test exercises the actual resolution logic without mocking
    to verify entity lookup and state checking works correctly.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"3": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Create the active (switch) and PIN (text) entities with proper unique IDs
    ent_reg = er.async_get(hass)
    base_unique_id = f"{lcm_entry.entry_id}|3"

    active_entry = ent_reg.async_get_or_create(
        "switch",
        DOMAIN,
        f"{base_unique_id}|enabled",
        config_entry=lcm_entry,
    )
    pin_entry = ent_reg.async_get_or_create(
        "text",
        DOMAIN,
        f"{base_unique_id}|pin",
        config_entry=lcm_entry,
    )

    # Set states: active=ON, pin="5678"
    hass.states.async_set(active_entry.entity_id, "on")
    hass.states.async_set(pin_entry.entity_id, "5678")
    await hass.async_block_till_done()

    # _resolve_masked_code should return the PIN value
    result = zwave_js_lock._resolve_masked_code(3)
    assert result == "5678"

    await zwave_js_lock.async_unload(False)


async def test_resolve_masked_code_returns_none_when_inactive(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test _resolve_masked_code returns None when slot is inactive."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"3": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Create the active (switch) and PIN (text) entities
    ent_reg = er.async_get(hass)
    base_unique_id = f"{lcm_entry.entry_id}|3"

    active_entry = ent_reg.async_get_or_create(
        "switch",
        DOMAIN,
        f"{base_unique_id}|enabled",
        config_entry=lcm_entry,
    )
    pin_entry = ent_reg.async_get_or_create(
        "text",
        DOMAIN,
        f"{base_unique_id}|pin",
        config_entry=lcm_entry,
    )

    # Set states: active=OFF, pin="5678"
    hass.states.async_set(active_entry.entity_id, "off")
    hass.states.async_set(pin_entry.entity_id, "5678")
    await hass.async_block_till_done()

    # _resolve_masked_code should return None (slot is inactive)
    result = zwave_js_lock._resolve_masked_code(3)
    assert result is None

    await zwave_js_lock.async_unload(False)


async def test_resolve_masked_code_returns_none_when_pin_not_numeric(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test _resolve_masked_code returns None when PIN is not numeric."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"3": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Create the active (switch) and PIN (text) entities
    ent_reg = er.async_get(hass)
    base_unique_id = f"{lcm_entry.entry_id}|3"

    active_entry = ent_reg.async_get_or_create(
        "switch",
        DOMAIN,
        f"{base_unique_id}|enabled",
        config_entry=lcm_entry,
    )
    pin_entry = ent_reg.async_get_or_create(
        "text",
        DOMAIN,
        f"{base_unique_id}|pin",
        config_entry=lcm_entry,
    )

    # Set states: active=ON, pin="not_numeric"
    hass.states.async_set(active_entry.entity_id, "on")
    hass.states.async_set(pin_entry.entity_id, "unknown")
    await hass.async_block_till_done()

    # _resolve_masked_code should return None (PIN not numeric)
    result = zwave_js_lock._resolve_masked_code(3)
    assert result is None

    await zwave_js_lock.async_unload(False)


async def test_resolve_masked_code_returns_none_for_unmanaged_slot(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test _resolve_masked_code returns None for slots not managed by LCM."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"1": {}},  # Only slot 1 is managed
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # _resolve_masked_code should return None for slot 99 (not managed)
    result = zwave_js_lock._resolve_masked_code(99)
    assert result is None

    await zwave_js_lock.async_unload(False)


async def test_resolve_masked_code_returns_none_when_entities_missing(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test _resolve_masked_code returns None when entities are not registered."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"3": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Don't create any entities - they're "missing"

    # _resolve_masked_code should return None (entities not found)
    result = zwave_js_lock._resolve_masked_code(3)
    assert result is None

    await zwave_js_lock.async_unload(False)


async def test_resolve_masked_code_returns_none_when_states_missing(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test _resolve_masked_code returns None when entity states are not available."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"3": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Create entities but don't set their states
    ent_reg = er.async_get(hass)
    base_unique_id = f"{lcm_entry.entry_id}|3"

    ent_reg.async_get_or_create(
        "switch",
        DOMAIN,
        f"{base_unique_id}|enabled",
        config_entry=lcm_entry,
    )
    ent_reg.async_get_or_create(
        "text",
        DOMAIN,
        f"{base_unique_id}|pin",
        config_entry=lcm_entry,
    )
    # Don't set states - they'll be None

    # _resolve_masked_code should return None (states not found)
    result = zwave_js_lock._resolve_masked_code(3)
    assert result is None

    await zwave_js_lock.async_unload(False)


# Client state tests


async def test_get_client_state_client_not_ready(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test _get_client_state returns False when client is not ready."""
    # Mock runtime_data to have no client
    with patch.object(zwave_integration, "runtime_data", None):
        ready, reason = zwave_js_lock._get_client_state()
        assert ready is False
        assert "client not ready" in reason


async def test_get_client_state_client_not_connected(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test _get_client_state returns False when client is not connected."""
    # Mock client.connected to be False
    with patch.object(lock_schlage_be469.client, "connected", False):
        ready, reason = zwave_js_lock._get_client_state()
        assert ready is False
        assert "not connected" in reason


async def test_get_client_state_driver_not_ready(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test _get_client_state returns False when driver is not ready."""
    # Mock client.driver to be None
    with patch.object(lock_schlage_be469.client, "driver", None):
        ready, reason = zwave_js_lock._get_client_state()
        assert ready is False
        assert "driver not ready" in reason


# Push subscription retry tests


async def test_subscribe_push_deferred_when_client_not_ready(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that push subscription is deferred when client isn't ready."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)

    # Ensure not already subscribed
    zwave_js_lock._value_update_unsub = None
    zwave_js_lock._push_retry_cancel = None

    # Mock client not ready
    with patch.object(
        zwave_js_lock, "_get_client_state", return_value=(False, "test reason")
    ):
        zwave_js_lock.subscribe_push_updates()

        # Should have scheduled a retry, not subscribed
        assert zwave_js_lock._value_update_unsub is None
        assert zwave_js_lock._push_retry_cancel is not None

    # Clean up retry
    if zwave_js_lock._push_retry_cancel:
        zwave_js_lock._push_retry_cancel()
        zwave_js_lock._push_retry_cancel = None


async def test_subscribe_push_retry_handler(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that push retry handler retries subscription."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Set up a pending retry
    zwave_js_lock._push_retry_cancel = MagicMock()

    # Call retry handler directly
    with patch.object(zwave_js_lock, "subscribe_push_updates") as mock_sub:
        zwave_js_lock._handle_push_retry(None)

        # Should have cleared retry and called subscribe
        assert zwave_js_lock._push_retry_cancel is None
        mock_sub.assert_called_once()

    await zwave_js_lock.async_unload(False)


async def test_unsubscribe_clears_pending_retry(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that unsubscribe clears any pending retry."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Set up a pending retry
    retry_cancel = MagicMock()
    zwave_js_lock._push_retry_cancel = retry_cancel

    zwave_js_lock.unsubscribe_push_updates()

    # Should have called the cancel function
    retry_cancel.assert_called_once()
    assert zwave_js_lock._push_retry_cancel is None

    await zwave_js_lock.async_unload(False)


async def test_subscribe_push_node_value_error(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that ValueError from node.on() schedules a retry."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)

    zwave_js_lock._value_update_unsub = None
    zwave_js_lock._push_retry_cancel = None

    # Mock node.on to raise ValueError
    with patch.object(
        lock_schlage_be469, "on", side_effect=ValueError("node not ready")
    ):
        zwave_js_lock.subscribe_push_updates()

        # Should have scheduled retry
        assert zwave_js_lock._value_update_unsub is None
        assert zwave_js_lock._push_retry_cancel is not None

    # Clean up
    if zwave_js_lock._push_retry_cancel:
        zwave_js_lock._push_retry_cancel()
        zwave_js_lock._push_retry_cancel = None


# Value update event filter tests


async def test_push_update_filters_non_usercode_events(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that push updates filter out non-User Code CC events."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {}
    zwave_js_lock.coordinator = mock_coordinator

    zwave_js_lock.subscribe_push_updates()

    # Send a non-User Code CC event (e.g., Door Lock CC)
    event = ZwaveEvent(
        type="value updated",
        data={
            "source": "node",
            "event": "value updated",
            "nodeId": lock_schlage_be469.node_id,
            "args": {
                "commandClass": 98,  # Door Lock CC, not User Code
                "property": "lockState",
                "propertyKey": 0,
                "newValue": "locked",
            },
        },
    )
    lock_schlage_be469.receive_event(event)
    await hass.async_block_till_done()

    # Coordinator should NOT receive any update
    mock_coordinator.push_update.assert_not_called()

    zwave_js_lock.unsubscribe_push_updates()
    await zwave_js_lock.async_unload(False)


async def test_push_update_filters_slot_zero(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that push updates filter out slot 0 (metadata slot)."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {}
    zwave_js_lock.coordinator = mock_coordinator

    zwave_js_lock.subscribe_push_updates()

    # Send a User Code CC event for slot 0
    event = ZwaveEvent(
        type="value updated",
        data={
            "source": "node",
            "event": "value updated",
            "nodeId": lock_schlage_be469.node_id,
            "args": {
                "commandClass": CommandClass.USER_CODE,
                "property": "userCode",
                "propertyKey": 0,  # Slot 0
                "newValue": "1234",
            },
        },
    )
    lock_schlage_be469.receive_event(event)
    await hass.async_block_till_done()

    # Coordinator should NOT receive any update
    mock_coordinator.push_update.assert_not_called()

    zwave_js_lock.unsubscribe_push_updates()
    await zwave_js_lock.async_unload(False)


async def test_push_update_handles_empty_usercode(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that push updates handle empty/None usercodes as cleared."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {}
    zwave_js_lock.coordinator = mock_coordinator

    zwave_js_lock.subscribe_push_updates()

    # Send event with empty newValue (code cleared)
    event = ZwaveEvent(
        type="value updated",
        data={
            "source": "node",
            "event": "value updated",
            "nodeId": lock_schlage_be469.node_id,
            "args": {
                "commandClass": CommandClass.USER_CODE,
                "property": "userCode",
                "propertyKey": 5,
                "newValue": None,
            },
        },
    )
    lock_schlage_be469.receive_event(event)
    await hass.async_block_till_done()

    # Coordinator should receive empty string for cleared slot
    mock_coordinator.push_update.assert_called_once_with({5: ""})

    zwave_js_lock.unsubscribe_push_updates()
    await zwave_js_lock.async_unload(False)


async def test_push_update_handles_zeros_usercode(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that push updates treat all-zeros usercode as cleared."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {}
    zwave_js_lock.coordinator = mock_coordinator

    zwave_js_lock.subscribe_push_updates()

    # Send event with all-zeros value (some locks report this for cleared)
    event = ZwaveEvent(
        type="value updated",
        data={
            "source": "node",
            "event": "value updated",
            "nodeId": lock_schlage_be469.node_id,
            "args": {
                "commandClass": CommandClass.USER_CODE,
                "property": "userCode",
                "propertyKey": 5,
                "newValue": "0000",
            },
        },
    )
    lock_schlage_be469.receive_event(event)
    await hass.async_block_till_done()

    # Coordinator should receive empty string for cleared slot
    mock_coordinator.push_update.assert_called_once_with({5: ""})

    zwave_js_lock.unsubscribe_push_updates()
    await zwave_js_lock.async_unload(False)


async def test_push_update_skips_unchanged_value(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that push updates skip when value hasn't changed."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {5: "1234"}  # Coordinator already has this value
    zwave_js_lock.coordinator = mock_coordinator

    zwave_js_lock.subscribe_push_updates()

    # Send event with same value as coordinator already has
    event = ZwaveEvent(
        type="value updated",
        data={
            "source": "node",
            "event": "value updated",
            "nodeId": lock_schlage_be469.node_id,
            "args": {
                "commandClass": CommandClass.USER_CODE,
                "property": "userCode",
                "propertyKey": 5,
                "newValue": "1234",  # Same as coordinator.data[5]
            },
        },
    )
    lock_schlage_be469.receive_event(event)
    await hass.async_block_till_done()

    # Coordinator should NOT receive duplicate update
    mock_coordinator.push_update.assert_not_called()

    zwave_js_lock.unsubscribe_push_updates()
    await zwave_js_lock.async_unload(False)


# Notification event tests


async def test_notification_event_non_access_control_logged(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that non-Access Control notifications are logged but don't fire events."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    events = async_capture_events(hass, EVENT_LOCK_STATE_CHANGED)

    # Send a non-Access Control notification (e.g., Power Management type=8)
    event = ZwaveEvent(
        type="notification",
        data={
            "source": "node",
            "event": "notification",
            "nodeId": lock_schlage_be469.node_id,
            "endpointIndex": 0,
            "ccId": 113,
            "args": {
                "type": 8,  # POWER_MANAGEMENT, not ACCESS_CONTROL (6)
                "event": 1,
                "label": "Power Management",
                "eventLabel": "Power has been applied",
                "parameters": {},
            },
        },
    )
    lock_schlage_be469.receive_event(event)
    await hass.async_block_till_done()

    # No LCM event should be fired for non-access control notifications
    assert len(events) == 0

    await zwave_js_lock.async_unload(False)


# Exception handling tests


async def test_set_usercode_continues_on_cache_exception(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that set_usercode proceeds when cache check raises exception."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Mock get_usercode to raise an exception
    with (
        patch(
            "custom_components.lock_code_manager.providers.zwave_js.get_usercode",
            side_effect=Exception("Cache unavailable"),
        ),
        patch.object(
            zwave_js_lock, "async_call_service", new_callable=AsyncMock
        ) as mock_service,
    ):
        # Should proceed with the set despite cache exception
        result = await zwave_js_lock.async_set_usercode(10, "9999", "Test")

        assert result is True
        mock_service.assert_called_once()

    await zwave_js_lock.async_unload(False)


async def test_clear_usercode_continues_on_cache_exception(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that clear_usercode proceeds when cache check raises exception."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Mock get_usercode to raise an exception
    with (
        patch(
            "custom_components.lock_code_manager.providers.zwave_js.get_usercode",
            side_effect=Exception("Cache unavailable"),
        ),
        patch.object(
            zwave_js_lock, "async_call_service", new_callable=AsyncMock
        ) as mock_service,
    ):
        # Should proceed with the clear despite cache exception
        result = await zwave_js_lock.async_clear_usercode(10)

        assert result is True
        mock_service.assert_called_once()

    await zwave_js_lock.async_unload(False)


async def test_get_usercodes_from_cache_raises_lock_disconnected(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that _get_usercodes_from_cache raises LockDisconnected on error."""

    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Mock get_usercodes to raise an exception
    with (
        patch(
            "custom_components.lock_code_manager.providers.zwave_js.get_usercodes",
            side_effect=Exception("Node unavailable"),
        ),
        pytest.raises(LockDisconnected),
    ):
        zwave_js_lock._get_usercodes_from_cache()

    await zwave_js_lock.async_unload(False)


async def test_refresh_usercode_cache_raises_lock_disconnected(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that _async_refresh_usercode_cache raises LockDisconnected on error."""

    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Mock async_refresh_cc_values to raise an exception
    with (
        patch.object(
            lock_schlage_be469,
            "async_refresh_cc_values",
            side_effect=Exception("Refresh failed"),
        ),
        pytest.raises(LockDisconnected),
    ):
        await zwave_js_lock._async_refresh_usercode_cache()

    await zwave_js_lock.async_unload(False)


async def test_get_usercodes_raises_when_disconnected(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that async_get_usercodes raises LockDisconnected when not connected."""

    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"1": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # Mock connection to be down
    with (
        patch.object(zwave_js_lock, "async_is_connection_up", return_value=False),
        pytest.raises(LockDisconnected),
    ):
        await zwave_js_lock.async_get_usercodes()

    await zwave_js_lock.async_unload(False)


async def test_get_usercodes_triggers_hard_refresh_for_missing_slots(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that async_get_usercodes triggers hard refresh when slots are missing."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"1": {}, "99": {}},  # Slot 99 won't be in cache
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # First call returns incomplete data, second returns complete
    call_count = 0

    def mock_cache():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call: missing slot 99
            return [{"code_slot": 1, "usercode": "1234", "in_use": True}]
        # After refresh: includes slot 99
        return [
            {"code_slot": 1, "usercode": "1234", "in_use": True},
            {"code_slot": 99, "usercode": "", "in_use": False},
        ]

    with (
        patch.object(
            zwave_js_lock, "_get_usercodes_from_cache", side_effect=mock_cache
        ),
        patch.object(
            zwave_js_lock,
            "_async_refresh_usercode_cache",
            new_callable=AsyncMock,
        ) as mock_refresh,
    ):
        codes = await zwave_js_lock.async_get_usercodes()

        # Should have triggered a hard refresh because slot 99 was missing
        mock_refresh.assert_called_once()
        assert codes[1] == "1234"
        assert codes[99] == ""

    await zwave_js_lock.async_unload(False)


async def test_get_usercodes_triggers_hard_refresh_for_unknown_state(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """Test that async_get_usercodes triggers hard refresh when in_use is None."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"1": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup(lcm_entry)

    # First call returns unknown state, second returns complete
    call_count = 0

    def mock_cache():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            # First call: slot with unknown in_use state
            return [{"code_slot": 1, "usercode": None, "in_use": None}]
        # After refresh: known state
        return [{"code_slot": 1, "usercode": "5678", "in_use": True}]

    with (
        patch.object(
            zwave_js_lock, "_get_usercodes_from_cache", side_effect=mock_cache
        ),
        patch.object(
            zwave_js_lock,
            "_async_refresh_usercode_cache",
            new_callable=AsyncMock,
        ) as mock_refresh,
    ):
        codes = await zwave_js_lock.async_get_usercodes()

        # Should have triggered a hard refresh because in_use was None
        mock_refresh.assert_called_once()
        assert codes[1] == "5678"

    await zwave_js_lock.async_unload(False)
