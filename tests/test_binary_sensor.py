"""Test binary sensor platform."""

import copy
from datetime import timedelta
import logging
from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE as NUMBER_SERVICE_SET_VALUE,
)
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.text import (
    DOMAIN as TEXT_DOMAIN,
    SERVICE_SET_VALUE as TEXT_SERVICE_SET_VALUE,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_component import async_update_entity
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from custom_components.lock_code_manager.const import (
    CONF_CALENDAR,
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
)
from custom_components.lock_code_manager.coordinator import (
    LockUsercodeUpdateCoordinator,
)
from custom_components.lock_code_manager.data import LockCodeManagerConfigEntry

from .common import (
    BASE_CONFIG,
    LOCK_1_ENTITY_ID,
    LOCK_2_ENTITY_ID,
    LOCK_DATA,
    SLOT_1_ACTIVE_ENTITY,
    SLOT_1_IN_SYNC_ENTITY,
    SLOT_1_PIN_ENTITY,
    SLOT_2_ACTIVE_ENTITY,
    SLOT_2_ENABLED_ENTITY,
    SLOT_2_NUMBER_OF_USES_ENTITY,
    SLOT_2_PIN_ENTITY,
)

_LOGGER = logging.getLogger(__name__)


def _get_lock_context(hass: HomeAssistant, config_entry: LockCodeManagerConfigEntry):
    """Return coordinator and provider for lock_1."""
    coordinator = config_entry.runtime_data.coordinators[LOCK_1_ENTITY_ID]
    return coordinator, coordinator.lock


async def _async_force_sync_cycle(
    hass: HomeAssistant, coordinator: LockUsercodeUpdateCoordinator
):
    """Trigger a coordinator refresh and follow-up entity update."""
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + coordinator.update_interval)
    await hass.async_block_till_done()


async def test_binary_sensor_entity(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Walk through calendar, usage, enable/disable, and PIN updates for slot 2."""
    # Initial calendar/active state should be off
    calendar_1, _ = hass.data["lock_code_manager_calendars"]
    state = hass.states.get("calendar.test_1")
    assert state
    assert state.state == STATE_OFF

    state = hass.states.get(SLOT_2_ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_OFF

    # Create an event to activate the slot and verify it toggles on
    now = dt_util.utcnow()
    start = now - timedelta(hours=1)
    end = now + timedelta(hours=1)

    cal_event = calendar_1.create_event(dtstart=start, dtend=end, summary="test")
    await hass.async_block_till_done()

    state = hass.states.get(SLOT_2_ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_ON

    # Removing the event should turn the slot off
    calendar_1.delete_event(cal_event.uid)
    await hass.async_block_till_done()

    state = hass.states.get(SLOT_2_ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_OFF

    # Adding another event turns it back on
    calendar_1.create_event(dtstart=start, dtend=end, summary="test")
    await hass.async_block_till_done()

    state = hass.states.get(SLOT_2_ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_ON

    # Exhaust uses and then restore them
    await hass.services.async_call(
        NUMBER_DOMAIN,
        NUMBER_SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: 0},
        target={ATTR_ENTITY_ID: SLOT_2_NUMBER_OF_USES_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get(SLOT_2_ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_OFF

    await hass.services.async_call(
        NUMBER_DOMAIN,
        NUMBER_SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: 5},
        target={ATTR_ENTITY_ID: SLOT_2_NUMBER_OF_USES_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get(SLOT_2_ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_ON

    # Disable/enable the slot via switch
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        target={ATTR_ENTITY_ID: SLOT_2_ENABLED_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get(SLOT_2_ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_OFF

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON,
        target={ATTR_ENTITY_ID: SLOT_2_ENABLED_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get(SLOT_2_ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_ON

    # Change PIN and ensure provider receives the update
    service_calls = hass.data[LOCK_DATA][LOCK_1_ENTITY_ID]["service_calls"]
    initial_set_calls = list(service_calls.get("set_usercode", []))

    await hass.services.async_call(
        TEXT_DOMAIN,
        TEXT_SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "0987"},
        target={ATTR_ENTITY_ID: SLOT_2_PIN_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert service_calls.get("set_usercode", []) == initial_set_calls

    new_config = copy.deepcopy(BASE_CONFIG)
    new_config[CONF_SLOTS][2][CONF_CALENDAR] = "calendar.test_2"

    hass.config_entries.async_update_entry(
        lock_code_manager_config_entry, options=new_config
    )
    await hass.async_block_till_done()

    # Changing to a different calendar should deactivate the slot
    state = hass.states.get(SLOT_2_ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_OFF


async def test_startup_no_code_flapping_when_synced(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that codes aren't unnecessarily cleared/set on startup when already synced."""
    # Create a calendar event to make slot 2 active
    # (slot 2 has calendar.test_1 configured in BASE_CONFIG)
    calendar_1, _ = hass.data["lock_code_manager_calendars"]
    now = dt_util.utcnow()
    start = now - timedelta(hours=1)
    end = now + timedelta(hours=1)
    calendar_1.create_event(dtstart=start, dtend=end, summary="test")
    await hass.async_block_till_done()

    # Get the in-sync binary sensor for lock 1, slot 2
    in_sync_entity = "binary_sensor.test_1_code_slot_2_in_sync"

    # Verify the entity exists
    state = hass.states.get(in_sync_entity)
    assert state, f"Entity {in_sync_entity} not found"

    # The lock already has code "5678" in slot 2 (from BASE_CONFIG setup)
    # and the PIN entity is also configured with "5678"
    # So they should be in sync without any service calls
    assert state.state == STATE_ON, "Codes should be in sync on startup"

    # Check that no set_usercode or clear_usercode calls were made during startup
    # We allow the initial coordinator refresh call, but no actual modifications
    service_calls = hass.data[LOCK_DATA][LOCK_1_ENTITY_ID]["service_calls"]

    # There should be no set_usercode calls during initial load
    set_calls = service_calls.get("set_usercode", [])
    assert len(set_calls) == 0, (
        f"Expected no set_usercode calls during startup when codes are synced, "
        f"but found: {set_calls}"
    )

    # There should be no clear_usercode calls during initial load
    clear_calls = service_calls.get("clear_usercode", [])
    assert len(clear_calls) == 0, (
        f"Expected no clear_usercode calls during startup when codes are synced, "
        f"but found: {clear_calls}"
    )


async def test_startup_detects_out_of_sync_code(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Test that out-of-sync codes are detected on startup and automatically corrected."""
    # Create config with a different PIN than what's on the lock
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {CONF_NAME: "test1", CONF_PIN: "9999", CONF_ENABLED: True},
        },
    }

    # Set up the lock with a different code than configured
    # Lock has "1234" but config wants "9999"
    config_entry = MockConfigEntry(
        domain=DOMAIN, data=config, unique_id="Test Out of Sync"
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    # Get the in-sync binary sensor
    in_sync_entity = "binary_sensor.test_1_code_slot_1_in_sync"

    # Verify the entity exists and detects out-of-sync state
    state = hass.states.get(in_sync_entity)
    assert state, f"Entity {in_sync_entity} not found"

    # Initially should be out of sync because lock has "1234" but config wants "9999"
    assert state.state == STATE_OFF, (
        "Codes should be detected as out of sync on startup"
    )

    # Verify that NO set_usercode was called during initial startup
    # (the fix prevents operations on first load)
    service_calls = hass.data[LOCK_DATA][LOCK_1_ENTITY_ID]["service_calls"]
    set_calls = service_calls.get("set_usercode", [])
    assert len(set_calls) == 0, (
        f"Expected no set_usercode calls during initial startup, but found: {set_calls}"
    )

    # Now trigger the async_update method which should detect the out-of-sync state
    # and correct it (this simulates the polling behavior)
    # Wait for the next update cycle
    await async_update_entity(hass, in_sync_entity)
    await hass.async_block_till_done()
    await async_update_entity(hass, in_sync_entity)
    await hass.async_block_till_done()

    # Verify that set_usercode WAS called to sync the code after initial load
    set_calls = service_calls.get("set_usercode", [])
    assert len(set_calls) == 1, (
        f"Expected exactly 1 set_usercode call after detecting out-of-sync, "
        f"but found {len(set_calls)}: {set_calls}"
    )
    assert set_calls[0] == (
        1,
        "9999",
        "test1",
    ), f"set_usercode should be called with correct values, got {set_calls[0]}"

    # After sync, the in-sync sensor should be ON
    await hass.async_block_till_done()
    state = hass.states.get(in_sync_entity)
    assert state.state == STATE_ON, "Codes should be in sync after automatic correction"

    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_startup_out_of_sync_slots_sync_once(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Ensure out-of-sync slots sync once each without extra operations."""
    # Arrange two slots that need syncing on startup
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {CONF_NAME: "test1", CONF_PIN: "9999", CONF_ENABLED: True},
            2: {CONF_NAME: "test2", CONF_PIN: "0000", CONF_ENABLED: True},
        },
    }

    config_entry = MockConfigEntry(
        domain=DOMAIN, data=config, unique_id="Test Multi Sync", title="Test LCM"
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    in_sync_slot_1 = "binary_sensor.test_1_code_slot_1_in_sync"
    in_sync_slot_2 = "binary_sensor.test_1_code_slot_2_in_sync"

    assert hass.states.get(in_sync_slot_1)
    assert hass.states.get(in_sync_slot_2)

    service_calls = hass.data[LOCK_DATA][LOCK_1_ENTITY_ID]["service_calls"]
    # No set calls should have happened before we trigger updates
    assert service_calls["set_usercode"] == []

    # Trigger sync for both slots (first update is skipped after initial load)
    await async_update_entity(hass, in_sync_slot_1)
    await async_update_entity(hass, in_sync_slot_2)
    await hass.async_block_till_done()
    await async_update_entity(hass, in_sync_slot_1)
    await async_update_entity(hass, in_sync_slot_2)
    await hass.async_block_till_done()

    set_calls = service_calls["set_usercode"]
    assert len(set_calls) == 2
    assert (1, "9999", "test1") in set_calls
    assert (2, "0000", "test2") in set_calls

    # Further updates should not issue extra operations once in sync
    await async_update_entity(hass, in_sync_slot_1)
    await async_update_entity(hass, in_sync_slot_2)
    await hass.async_block_till_done()

    assert len(service_calls["set_usercode"]) == 2

    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_startup_waits_for_valid_active_state(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Test that in-sync entity doesn't load initial state when active entity has invalid state."""
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True},
        },
    }

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=config,
        unique_id="Test Invalid Active State",
        title="Test LCM",
    )
    config_entry.add_to_hass(hass)

    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    # Get the entity IDs
    active_entity_id = "binary_sensor.test_lcm_code_slot_1_active"
    in_sync_entity = "binary_sensor.test_1_code_slot_1_in_sync"

    # Verify entities exist
    assert hass.states.get(active_entity_id), (
        f"Active entity {active_entity_id} not found"
    )
    assert hass.states.get(in_sync_entity), f"In-sync entity {in_sync_entity} not found"

    # Get the entity object
    entity_component = hass.data["entity_components"]["binary_sensor"]
    in_sync_entity_obj = entity_component.get_entity(in_sync_entity)
    assert in_sync_entity_obj

    # Reset to simulate pre-initial-load state
    in_sync_entity_obj._attr_is_on = None

    # Set the active entity to unavailable
    hass.states.async_set(active_entity_id, STATE_UNAVAILABLE)
    await hass.async_block_till_done()

    # Trigger update with unavailable active state
    await async_update_entity(hass, in_sync_entity)
    await hass.async_block_till_done()

    # Verify initial state is still not loaded (is_on still None)
    assert in_sync_entity_obj._attr_is_on is None

    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_in_sync_waits_for_missing_pin_state(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that in-sync entity waits for dependent entities to report state."""
    entity_component = hass.data["entity_components"]["binary_sensor"]
    in_sync_entity_obj = entity_component.get_entity(SLOT_1_IN_SYNC_ENTITY)
    assert in_sync_entity_obj is not None

    # Simulate pre-initialization state
    in_sync_entity_obj._attr_is_on = None

    # Remove the PIN entity state so _ensure_entities_ready() fails
    hass.states.async_remove(SLOT_1_PIN_ENTITY)
    await hass.async_block_till_done()

    await async_update_entity(hass, SLOT_1_IN_SYNC_ENTITY)
    await hass.async_block_till_done()

    # Entity should still be waiting on initial state
    assert in_sync_entity_obj._attr_is_on is None, (
        "In-sync sensor should not initialize when PIN state is missing"
    )

    # Restore the PIN entity state and verify initialization completes
    hass.states.async_set(SLOT_1_PIN_ENTITY, "1234")
    await hass.async_block_till_done()

    await async_update_entity(hass, SLOT_1_IN_SYNC_ENTITY)
    await hass.async_block_till_done()

    assert in_sync_entity_obj._attr_is_on is True, (
        "In-sync sensor should initialize once dependent states are available"
    )


async def test_entities_track_availability(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that entities react to lock availability and coordinator data."""
    entity_component = hass.data["entity_components"]["binary_sensor"]
    active_entity_obj = entity_component.get_entity(SLOT_1_ACTIVE_ENTITY)
    in_sync_entity_obj = entity_component.get_entity(SLOT_1_IN_SYNC_ENTITY)
    assert active_entity_obj is not None
    assert in_sync_entity_obj is not None

    coordinator, _ = _get_lock_context(hass, lock_code_manager_config_entry)

    assert active_entity_obj.available
    assert in_sync_entity_obj.available

    # Make lock 1 unavailable and verify per-lock entity follows
    hass.states.async_set(LOCK_1_ENTITY_ID, STATE_UNAVAILABLE)
    await hass.async_block_till_done()

    assert active_entity_obj.available
    assert not in_sync_entity_obj.available

    # When all locks are unavailable, the shared entity also becomes unavailable
    hass.states.async_set(LOCK_2_ENTITY_ID, STATE_UNAVAILABLE)
    await hass.async_block_till_done()

    assert not active_entity_obj.available

    # Restoring the lock state should re-enable availability
    hass.states.async_set(LOCK_1_ENTITY_ID, "locked")
    hass.states.async_set(LOCK_2_ENTITY_ID, "locked")
    await hass.async_block_till_done()

    assert active_entity_obj.available
    assert in_sync_entity_obj.available

    # The per-lock entity should also reflect missing coordinator data
    coordinator.data.pop(1)
    assert not in_sync_entity_obj.available

    coordinator.data[1] = "1234"
    assert in_sync_entity_obj.available


async def test_handles_disconnected_lock_on_set(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that binary sensor handles LockDisconnected exception when setting usercode."""
    # Verify initial state - slot should be active and in sync
    active_state = hass.states.get(SLOT_1_ACTIVE_ENTITY)
    assert active_state.state == STATE_ON

    synced_state = hass.states.get(SLOT_1_IN_SYNC_ENTITY)
    assert synced_state.state == STATE_ON

    coordinator, lock_provider = _get_lock_context(hass, lock_code_manager_config_entry)
    lock_provider.set_connected(False)

    # Change PIN to trigger sync
    await hass.services.async_call(
        TEXT_DOMAIN,
        TEXT_SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "9999"},
        target={ATTR_ENTITY_ID: SLOT_1_PIN_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()

    # Synced state should now be off (out of sync)
    synced_state = hass.states.get(SLOT_1_IN_SYNC_ENTITY)
    assert synced_state.state == STATE_OFF

    # Trigger coordinator refresh to attempt sync
    await _async_force_sync_cycle(hass, coordinator)

    # Binary sensor should remain out of sync due to failed sync
    synced_state = hass.states.get(SLOT_1_IN_SYNC_ENTITY)
    assert synced_state.state in (STATE_OFF, STATE_UNAVAILABLE)

    # Verify the code wasn't actually changed (still old value)
    assert hass.data[LOCK_DATA][LOCK_1_ENTITY_ID]["codes"][1] == "1234"

    # Reconnect the lock and refresh coordinator to restore availability
    lock_provider.set_connected(True)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    entity_component = hass.data["entity_components"]["binary_sensor"]
    in_sync_entity_obj = entity_component.get_entity(SLOT_1_IN_SYNC_ENTITY)

    # Directly trigger the update state method to perform sync
    await in_sync_entity_obj._async_update_state()
    await hass.async_block_till_done()

    assert hass.data[LOCK_DATA][LOCK_1_ENTITY_ID]["codes"][1] == "9999"
    assert hass.states.get(SLOT_1_IN_SYNC_ENTITY).state == STATE_ON


async def test_handles_disconnected_lock_on_clear(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that binary sensor handles LockDisconnected exception when clearing usercode."""
    # Verify initial state - slot should be active and in sync
    active_state = hass.states.get(SLOT_1_ACTIVE_ENTITY)
    assert active_state.state == STATE_ON

    synced_state = hass.states.get(SLOT_1_IN_SYNC_ENTITY)
    assert synced_state.state == STATE_ON

    coordinator, lock_provider = _get_lock_context(hass, lock_code_manager_config_entry)
    lock_provider.set_connected(False)

    # Disable the slot to trigger clear
    hass.states.async_set(SLOT_1_ACTIVE_ENTITY, STATE_OFF)
    await hass.async_block_till_done()

    # Trigger coordinator refresh
    await _async_force_sync_cycle(hass, coordinator)

    # Verify the code wasn't actually cleared (still has value)
    assert hass.data[LOCK_DATA][LOCK_1_ENTITY_ID]["codes"].get(1) == "1234"

    # Reconnect the lock and refresh coordinator to restore availability
    lock_provider.set_connected(True)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    entity_component = hass.data["entity_components"]["binary_sensor"]
    in_sync_entity_obj = entity_component.get_entity(SLOT_1_IN_SYNC_ENTITY)

    # Directly trigger the update state method to perform sync
    await in_sync_entity_obj._async_update_state()
    await hass.async_block_till_done()

    assert hass.data[LOCK_DATA][LOCK_1_ENTITY_ID]["codes"].get(1) is None


async def test_coordinator_refresh_failure_schedules_retry(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that coordinator refresh failure after sync schedules a retry."""
    # Verify initial state - slot should be active and in sync
    synced_state = hass.states.get(SLOT_1_IN_SYNC_ENTITY)
    assert synced_state.state == STATE_ON

    coordinator, _ = _get_lock_context(hass, lock_code_manager_config_entry)

    entity_component = hass.data["entity_components"]["binary_sensor"]
    in_sync_entity_obj = entity_component.get_entity(SLOT_1_IN_SYNC_ENTITY)

    # Verify no retry is scheduled initially
    assert in_sync_entity_obj._retry_unsub is None

    # Patch coordinator refresh to fail BEFORE changing PIN
    # This way the failure happens during the sync triggered by the PIN change
    with patch.object(
        coordinator,
        "async_refresh",
        new=AsyncMock(side_effect=UpdateFailed("Connection failed")),
    ):
        # Change PIN to trigger sync - coordinator refresh will fail
        await hass.services.async_call(
            TEXT_DOMAIN,
            TEXT_SERVICE_SET_VALUE,
            service_data={ATTR_VALUE: "9999"},
            target={ATTR_ENTITY_ID: SLOT_1_PIN_ENTITY},
            blocking=True,
        )
        await hass.async_block_till_done()

    # Retry should be scheduled due to coordinator refresh failure
    assert in_sync_entity_obj._retry_unsub is not None, (
        "Retry should be scheduled when coordinator refresh fails after sync"
    )

    # Clean up - cancel the retry
    in_sync_entity_obj._cancel_retry()
