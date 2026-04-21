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
    CONF_ENTITY_ID,
    CONF_NAME,
    CONF_PIN,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
    MAX_SYNC_ATTEMPTS,
    SYNC_ATTEMPT_WINDOW,
    TICK_INTERVAL,
)
from custom_components.lock_code_manager.coordinator import (
    LockUsercodeUpdateCoordinator,
)
from custom_components.lock_code_manager.exceptions import (
    DuplicateCodeError,
    LockCodeManagerError,
)
from custom_components.lock_code_manager.models import SyncState

from .common import (
    BASE_CONFIG,
    LOCK_1_ENTITY_ID,
    LOCK_2_ENTITY_ID,
    SLOT_1_ACTIVE_ENTITY,
    SLOT_1_ENABLED_ENTITY,
    SLOT_1_IN_SYNC_ENTITY,
    SLOT_1_PIN_ENTITY,
    SLOT_2_ACTIVE_ENTITY,
    SLOT_2_ENABLED_ENTITY,
    SLOT_2_NUMBER_OF_USES_ENTITY,
    SLOT_2_PIN_ENTITY,
    MockLCMLock,
)
from .conftest import (
    async_initial_tick,
    async_trigger_sync_tick,
    async_trigger_sync_tick_for_manager,
    get_in_sync_entity_obj,
)

_LOGGER = logging.getLogger(__name__)


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
    mock_calendars,
    lock_code_manager_config_entry,
):
    """Walk through calendar, usage, enable/disable, and PIN updates for slot 2."""
    # Initial calendar/active state should be off
    calendar_1, _ = mock_calendars
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
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    service_calls = lock_provider.service_calls
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
    new_config[CONF_SLOTS][2][CONF_ENTITY_ID] = "calendar.test_2"

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
    mock_calendars,
    lock_code_manager_config_entry,
):
    """Test that codes aren't unnecessarily cleared/set on startup when already synced."""
    # Create a calendar event to make slot 2 active
    # (slot 2 has calendar.test_1 configured in BASE_CONFIG)
    calendar_1, _ = mock_calendars
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

    # Trigger a tick so the sync manager processes the calendar state change
    await async_trigger_sync_tick(hass, in_sync_entity, set_dirty=False)

    # The lock already has code "5678" in slot 2 (from BASE_CONFIG setup)
    # and the PIN entity is also configured with "5678"
    # So they should be in sync without any service calls
    state = hass.states.get(in_sync_entity)
    assert state.state == STATE_ON, "Codes should be in sync on startup"

    # Check that no set_usercode or clear_usercode calls were made during startup
    # We allow the initial coordinator refresh call, but no actual modifications
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    service_calls = lock_provider.service_calls

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

    # Verify the entity exists
    state = hass.states.get(in_sync_entity)
    assert state, f"Entity {in_sync_entity} not found"

    in_sync_entity_obj = get_in_sync_entity_obj(hass, in_sync_entity)

    lock_provider = config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    service_calls = lock_provider.service_calls

    # Clear any calls from initial startup (initial tick may or may not
    # have performed sync depending on entity readiness timing)
    service_calls.get("set_usercode", []).clear()

    # Force out-of-sync state: reset code on the lock to mismatch
    lock_provider.codes[1] = "1234"
    in_sync_entity_obj._sync_manager._state = SyncState.OUT_OF_SYNC

    # Trigger a tick to perform the sync operation
    await async_trigger_sync_tick(hass, in_sync_entity)

    # Verify that set_usercode WAS called to sync the code
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

    # After sync + coordinator refresh, trigger another tick to detect "back in sync"
    await async_trigger_sync_tick(hass, in_sync_entity, set_dirty=False)
    state = hass.states.get(in_sync_entity)
    assert state.state == STATE_ON, "Codes should be in sync after automatic correction"

    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_startup_out_of_sync_slots_sync_once(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    Ensure out-of-sync slots sync once each without extra operations.

    With coordinator-triggered syncs, out-of-sync slots are detected and synced
    automatically during startup via coordinator update callbacks.
    """
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

    lock_provider = config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    service_calls = lock_provider.service_calls

    # Initial tick (in async_start) loads state but doesn't perform sync.
    # Trigger a second tick to perform sync operations.
    in_sync_entity_obj_1 = get_in_sync_entity_obj(hass, in_sync_slot_1)
    in_sync_entity_obj_2 = get_in_sync_entity_obj(hass, in_sync_slot_2)

    # Trigger ticks to perform sync operations.
    # Each slot needs up to two ticks: one to load initial state (if not already
    # loaded during async_start), and another to perform the actual sync.
    for mgr in (in_sync_entity_obj_1._sync_manager, in_sync_entity_obj_2._sync_manager):
        await async_trigger_sync_tick_for_manager(hass, mgr)
        # Second tick needed if first tick only did initial state load
        await async_trigger_sync_tick_for_manager(hass, mgr)

    # Both slots should have synced exactly once
    set_calls = service_calls["set_usercode"]
    assert len(set_calls) == 2
    assert (1, "9999", "test1") in set_calls
    assert (2, "0000", "test2") in set_calls

    # Further ticks should not issue extra operations once in sync
    for mgr in (in_sync_entity_obj_1._sync_manager, in_sync_entity_obj_2._sync_manager):
        await async_trigger_sync_tick_for_manager(hass, mgr)

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
    in_sync_entity_obj = get_in_sync_entity_obj(hass, in_sync_entity)

    # Reset to simulate pre-initial-load state
    in_sync_entity_obj._sync_manager._state = SyncState.LOADING

    # Set the active entity to unavailable
    hass.states.async_set(active_entity_id, STATE_UNAVAILABLE)
    await hass.async_block_till_done()

    # Trigger tick with unavailable active state
    await async_trigger_sync_tick_for_manager(
        hass, in_sync_entity_obj._sync_manager, set_dirty=False
    )

    # Verify initial state is still not loaded (still LOADING)
    assert in_sync_entity_obj._sync_manager._state is SyncState.LOADING

    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_in_sync_waits_for_missing_pin_state(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that in-sync entity waits for dependent entities to report state."""
    in_sync_entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)

    # Simulate pre-initialization state
    in_sync_entity_obj._sync_manager._state = SyncState.LOADING

    # Remove the PIN entity state so _ensure_entities_ready() fails
    hass.states.async_remove(SLOT_1_PIN_ENTITY)
    await hass.async_block_till_done()

    await async_trigger_sync_tick_for_manager(
        hass, in_sync_entity_obj._sync_manager, set_dirty=False
    )

    # Entity should still be waiting on initial state
    assert in_sync_entity_obj._sync_manager._state is SyncState.LOADING, (
        "In-sync sensor should not initialize when PIN state is missing"
    )

    # Restore the PIN entity state and verify initialization completes
    hass.states.async_set(SLOT_1_PIN_ENTITY, "1234")
    await hass.async_block_till_done()

    await async_trigger_sync_tick_for_manager(
        hass, in_sync_entity_obj._sync_manager, set_dirty=False
    )

    assert in_sync_entity_obj._sync_manager._state is SyncState.IN_SYNC, (
        "In-sync sensor should initialize once dependent states are available"
    )


async def test_entities_track_availability(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that entities react to lock availability and coordinator data."""
    active_entity_obj = get_in_sync_entity_obj(hass, SLOT_1_ACTIVE_ENTITY)
    in_sync_entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)

    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    coordinator = lock_provider.coordinator
    assert coordinator is not None

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
    await async_initial_tick(hass, SLOT_1_IN_SYNC_ENTITY)

    # Verify initial state - slot should be active and in sync
    active_state = hass.states.get(SLOT_1_ACTIVE_ENTITY)
    assert active_state.state == STATE_ON

    synced_state = hass.states.get(SLOT_1_IN_SYNC_ENTITY)
    assert synced_state.state == STATE_ON

    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    coordinator = lock_provider.coordinator
    assert coordinator is not None
    lock_provider.set_connected(False)

    in_sync_entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)

    # Change PIN to trigger sync
    await hass.services.async_call(
        TEXT_DOMAIN,
        TEXT_SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "9999"},
        target={ATTR_ENTITY_ID: SLOT_1_PIN_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()

    # Trigger a tick - sync will fail due to disconnected lock
    await async_trigger_sync_tick_for_manager(
        hass, in_sync_entity_obj._sync_manager, set_dirty=False
    )

    # Synced state should now be off (out of sync)
    synced_state = hass.states.get(SLOT_1_IN_SYNC_ENTITY)
    assert synced_state.state == STATE_OFF

    # Verify the code wasn't actually changed (still old value)
    assert lock_provider.codes[1] == "1234"

    # Reconnect the lock and refresh coordinator to restore availability
    lock_provider.set_connected(True)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # Directly trigger the tick to perform sync
    await in_sync_entity_obj._sync_manager._async_tick()
    await hass.async_block_till_done()

    assert lock_provider.codes[1] == "9999"

    # Trigger another tick to detect "back in sync"
    await in_sync_entity_obj._sync_manager._async_tick()
    await hass.async_block_till_done()
    assert hass.states.get(SLOT_1_IN_SYNC_ENTITY).state == STATE_ON


async def test_handles_disconnected_lock_on_clear(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that binary sensor handles LockDisconnected exception when clearing usercode."""
    await async_initial_tick(hass, SLOT_1_IN_SYNC_ENTITY)

    # Verify initial state - slot should be active and in sync
    active_state = hass.states.get(SLOT_1_ACTIVE_ENTITY)
    assert active_state.state == STATE_ON

    synced_state = hass.states.get(SLOT_1_IN_SYNC_ENTITY)
    assert synced_state.state == STATE_ON

    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    coordinator = lock_provider.coordinator
    assert coordinator is not None
    lock_provider.set_connected(False)

    # Disable the slot to trigger clear
    hass.states.async_set(SLOT_1_ACTIVE_ENTITY, STATE_OFF)
    await hass.async_block_till_done()

    # Trigger coordinator refresh
    await _async_force_sync_cycle(hass, coordinator)

    # Verify the code wasn't actually cleared (still has value)
    assert lock_provider.codes.get(1) == "1234"

    # Reconnect the lock and refresh coordinator to restore availability
    lock_provider.set_connected(True)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    in_sync_entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)

    # Directly trigger the update state method to perform sync
    await async_trigger_sync_tick_for_manager(
        hass, in_sync_entity_obj._sync_manager, set_dirty=False
    )

    assert lock_provider.codes.get(1) is None


async def test_coordinator_refresh_failure_schedules_retry(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that coordinator refresh failure after sync sets dirty flag."""
    await async_initial_tick(hass, SLOT_1_IN_SYNC_ENTITY)

    # Verify initial state - slot should be active and in sync
    synced_state = hass.states.get(SLOT_1_IN_SYNC_ENTITY)
    assert synced_state.state == STATE_ON

    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    coordinator = lock_provider.coordinator
    assert coordinator is not None

    in_sync_entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)

    # Ensure manager is in a tickable state
    in_sync_entity_obj._sync_manager._state = SyncState.OUT_OF_SYNC

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

        # Trigger tick to attempt sync - coordinator refresh will fail
        await in_sync_entity_obj._sync_manager._async_tick()
        await hass.async_block_till_done()

    # State should be OUT_OF_SYNC due to coordinator refresh failure
    assert in_sync_entity_obj._sync_manager._state is SyncState.OUT_OF_SYNC, (
        "State should be OUT_OF_SYNC when coordinator refresh fails after sync"
    )


async def test_coordinator_update_triggers_sync_on_external_change(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """
    Test that coordinator updates trigger sync when lock code changes externally.

    This test replicates the issue where someone manually changes a code on the
    lock (or the lock reports a different code), and the integration should
    automatically sync to restore the configured code.

    With the tick-based design:
    1. Coordinator updates trigger _request_sync_check() via the coordinator listener
    2. The next periodic tick performs reconciliation and syncs if needed

    This test verifies that coordinator updates transition the state to
    OUT_OF_SYNC and the subsequent tick detects the mismatch and performs
    the sync operation.
    """
    # Use config without calendar so both slots are active
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True},
        },
    }

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=config,
        unique_id="Test Coordinator Sync",
        title="Test LCM",
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    in_sync_entity = "binary_sensor.test_1_code_slot_1_in_sync"
    await async_initial_tick(hass, in_sync_entity)

    # Verify initial state - should be in sync
    state = hass.states.get(in_sync_entity)
    assert state.state == STATE_ON, "Slot should be in sync initially"

    # Get the lock provider and coordinator
    lock_provider = config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    coordinator = lock_provider.coordinator
    service_calls = lock_provider.service_calls

    # Clear any service calls from initial setup
    service_calls.get("set_usercode", []).clear()

    # Simulate external change: someone changed the code on the lock to "9999"
    lock_provider.codes[1] = "9999"

    # Trigger coordinator refresh - marks dirty via coordinator listener
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # Trigger tick to detect mismatch and perform sync
    await async_trigger_sync_tick(hass, in_sync_entity, set_dirty=False)

    # Coordinator update + tick should have triggered sync to restore "1234"
    assert len(service_calls["set_usercode"]) == 1, (
        "Tick should trigger sync when lock code differs from config"
    )
    assert service_calls["set_usercode"][0] == (1, "1234", "test1"), (
        "Sync should restore the configured PIN"
    )

    # Trigger another tick to detect "back in sync"
    await async_trigger_sync_tick(hass, in_sync_entity, set_dirty=False)
    state = hass.states.get(in_sync_entity)
    assert state.state == STATE_ON, "Slot should be in sync after tick-triggered sync"

    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_condition_entity_subscription_updates_on_config_change(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Test that condition entity subscription updates when config entry changes."""
    # Create two input_booleans to use as condition entities
    hass.states.async_set("input_boolean.access_1", STATE_ON)
    hass.states.async_set("input_boolean.access_2", STATE_OFF)
    await hass.async_block_till_done()

    # Set up a slot with the first input_boolean as condition
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {
                CONF_NAME: "test1",
                CONF_PIN: "1234",
                CONF_ENABLED: True,
                CONF_ENTITY_ID: "input_boolean.access_1",
            },
        },
    }

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=config,
        unique_id="Test Condition Subscription",
        title="Test LCM",
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    active_entity = "binary_sensor.test_lcm_code_slot_1_active"

    # Initial state: access_1 is ON, so slot should be active
    state = hass.states.get(active_entity)
    assert state.state == STATE_ON, "Slot should be active when condition entity is ON"

    # Turn off access_1 - slot should become inactive
    hass.states.async_set("input_boolean.access_1", STATE_OFF)
    await hass.async_block_till_done()

    state = hass.states.get(active_entity)
    assert state.state == STATE_OFF, (
        "Slot should be inactive when condition entity is OFF"
    )

    # Turn it back on
    hass.states.async_set("input_boolean.access_1", STATE_ON)
    await hass.async_block_till_done()

    state = hass.states.get(active_entity)
    assert state.state == STATE_ON, "Slot should be active when condition entity is ON"

    # Now update the config entry to use a different condition entity
    new_config = copy.deepcopy(config)
    new_config[CONF_SLOTS][1][CONF_ENTITY_ID] = "input_boolean.access_2"

    hass.config_entries.async_update_entry(config_entry, data=new_config)
    await hass.async_block_till_done()

    # Now the slot should be inactive because access_2 is OFF
    state = hass.states.get(active_entity)
    assert state.state == STATE_OFF, (
        "Slot should be inactive after switching to condition entity that is OFF"
    )

    # The slot should now react to access_2, NOT access_1
    # Turn on access_2 - slot should become active
    hass.states.async_set("input_boolean.access_2", STATE_ON)
    await hass.async_block_till_done()

    state = hass.states.get(active_entity)
    assert state.state == STATE_ON, "Slot should react to new condition entity"

    # Turn off access_2
    hass.states.async_set("input_boolean.access_2", STATE_OFF)
    await hass.async_block_till_done()

    state = hass.states.get(active_entity)
    assert state.state == STATE_OFF, (
        "Slot should react to new condition entity being OFF"
    )

    # Verify the slot does NOT react to the old condition entity anymore
    # Turn on access_1 - slot should stay inactive
    hass.states.async_set("input_boolean.access_1", STATE_ON)
    await hass.async_block_till_done()

    state = hass.states.get(active_entity)
    assert state.state == STATE_OFF, (
        "Slot should NOT react to old condition entity after config change"
    )

    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_sync_disables_slot_on_duplicate_code(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that sync disables slot and notifies when duplicate code is detected."""
    await async_initial_tick(hass, SLOT_1_IN_SYNC_ENTITY)

    # Verify initial state - slot 1 should be active and in sync
    active_state = hass.states.get(SLOT_1_ACTIVE_ENTITY)
    assert active_state.state == STATE_ON

    synced_state = hass.states.get(SLOT_1_IN_SYNC_ENTITY)
    assert synced_state.state == STATE_ON

    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    coordinator = lock_provider.coordinator
    assert coordinator is not None

    in_sync_entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)

    # Change PIN to trigger sync, and mock the provider to raise DuplicateCodeError
    with patch.object(
        lock_provider,
        "async_internal_set_usercode",
        AsyncMock(
            side_effect=DuplicateCodeError(
                code_slot=1,
                conflicting_slot=5,
                conflicting_slot_managed=False,
                lock_entity_id=LOCK_1_ENTITY_ID,
            )
        ),
    ):
        await hass.services.async_call(
            TEXT_DOMAIN,
            TEXT_SERVICE_SET_VALUE,
            service_data={ATTR_VALUE: "9999"},
            target={ATTR_ENTITY_ID: SLOT_1_PIN_ENTITY},
            blocking=True,
        )
        await hass.async_block_till_done()

        # Trigger tick to attempt sync (will fail with DuplicateCodeError)
        await in_sync_entity_obj._sync_manager._async_tick()
        await hass.async_block_till_done()

    # Slot should be disabled (enabled switch turned off)
    enabled_state = hass.states.get(SLOT_1_ENABLED_ENTITY)
    assert enabled_state.state == STATE_OFF

    # Verify the "9999" code was never set — it should have been blocked
    # (The slot may have been cleared as a result of disabling, that's expected)
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    assert lock_provider.codes.get(1) != "9999"


async def test_sync_attempts_exceeded_disables_slot(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that exceeding sync attempts disables slot and notifies."""
    await async_initial_tick(hass, SLOT_1_IN_SYNC_ENTITY)

    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    coordinator = lock_provider.coordinator
    assert coordinator is not None

    in_sync_entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)

    # Change PIN to trigger sync
    await hass.services.async_call(
        TEXT_DOMAIN,
        TEXT_SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "9999"},
        target={ATTR_ENTITY_ID: SLOT_1_PIN_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()

    sync_mgr = in_sync_entity_obj._sync_manager

    # Pre-load the tracker to simulate repeated failures on the same PIN
    now = dt_util.utcnow()
    sync_mgr._sync_attempt_count = MAX_SYNC_ATTEMPTS
    sync_mgr._sync_attempt_first = now

    # Trigger tick — circuit breaker should fire before attempting sync
    sync_mgr._state = SyncState.OUT_OF_SYNC
    await sync_mgr._async_tick()
    await hass.async_block_till_done()

    # Lock should be suspended (not slot disabled)
    assert sync_mgr._state is SyncState.SUSPENDED
    assert coordinator.slot_sync_mgrs_suspended is True

    # The "9999" code should never have been sent to the lock — the tracker
    # check fires BEFORE the sync operation
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    assert lock_provider.codes.get(1) != "9999"


async def test_sync_tracker_resets_when_back_in_sync(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that sync attempt tracker resets when slot comes back in sync."""
    await async_initial_tick(hass, SLOT_1_IN_SYNC_ENTITY)

    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    coordinator = lock_provider.coordinator
    assert coordinator is not None

    in_sync_entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)

    # Verify currently in sync
    synced_state = hass.states.get(SLOT_1_IN_SYNC_ENTITY)
    assert synced_state.state == STATE_ON

    # Change the PIN, which triggers a sync cycle. The mock lock will
    # accept the code and coordinator will reflect it, bringing it back in sync.
    await hass.services.async_call(
        TEXT_DOMAIN,
        TEXT_SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "9999"},
        target={ATTR_ENTITY_ID: SLOT_1_PIN_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()
    await _async_force_sync_cycle(hass, coordinator)

    # Tick performs sync → coordinator refreshes → detects back in sync
    # and resets tracker immediately
    in_sync_entity_obj._sync_manager._state = SyncState.OUT_OF_SYNC
    await in_sync_entity_obj._sync_manager._async_tick()
    await hass.async_block_till_done()

    # Slot should be back in sync after the sync operation, and the
    # tracker should not be at the circuit breaker threshold
    synced_state = hass.states.get(SLOT_1_IN_SYNC_ENTITY)
    assert synced_state.state == STATE_ON
    assert not in_sync_entity_obj._sync_manager._sync_attempts_exceeded()


async def test_sync_tracker_does_not_fire_breaker_with_expired_window(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that stale sync attempts outside the window do not trigger the circuit breaker.

    When the attempt window expires, _record_sync_attempt resets the counter,
    preventing stale counts from triggering the breaker on a new sync cycle.
    """
    await async_initial_tick(hass, SLOT_1_IN_SYNC_ENTITY)

    in_sync_entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)
    sync_mgr = in_sync_entity_obj._sync_manager

    # Verify in sync
    assert hass.states.get(SLOT_1_IN_SYNC_ENTITY).state == STATE_ON

    # Simulate prior sync attempts from an EXPIRED window
    sync_mgr._sync_attempt_count = MAX_SYNC_ATTEMPTS - 1
    sync_mgr._sync_attempt_first = dt_util.utcnow() - SYNC_ATTEMPT_WINDOW * 2

    # Change PIN to trigger out-of-sync
    await hass.services.async_call(
        TEXT_DOMAIN,
        TEXT_SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "9999"},
        target={ATTR_ENTITY_ID: SLOT_1_PIN_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()

    # _request_sync_check transitions IN_SYNC -> OUT_OF_SYNC
    assert sync_mgr._state is SyncState.OUT_OF_SYNC

    # Tick fires — expired window causes _record_sync_attempt to reset
    # the counter, so the breaker does not fire. Sync succeeds and
    # resets the tracker.
    await sync_mgr._async_tick()
    await hass.async_block_till_done()

    # Sync succeeded — lock should be IN_SYNC, not SUSPENDED
    assert sync_mgr._state is SyncState.IN_SYNC
    # Tracker reset after successful sync
    assert sync_mgr._sync_attempt_count == 0


async def test_sync_tracker_expired_window_resets(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that sync attempt tracker resets when the time window expires."""
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    coordinator = lock_provider.coordinator
    assert coordinator is not None

    in_sync_entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)

    # Set up tracker with max attempts but with an expired window
    in_sync_entity_obj._sync_manager._sync_attempt_count = MAX_SYNC_ATTEMPTS
    in_sync_entity_obj._sync_manager._sync_attempt_first = (
        dt_util.utcnow() - SYNC_ATTEMPT_WINDOW * 2
    )

    # The _sync_attempts_exceeded check should return False (window expired)
    assert not in_sync_entity_obj._sync_manager._sync_attempts_exceeded()


async def test_clear_operation_does_not_increment_tracker(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that clear sync operations do not increment the sync attempt tracker."""
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    coordinator = lock_provider.coordinator
    assert coordinator is not None

    in_sync_entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)

    # Verify starting at zero
    assert in_sync_entity_obj._sync_manager._sync_attempt_count == 0

    # Disable slot to trigger a clear sync cycle
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        target={ATTR_ENTITY_ID: SLOT_1_ENABLED_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()
    await _async_force_sync_cycle(hass, coordinator)

    # Clear operation should NOT have incremented the tracker
    assert in_sync_entity_obj._sync_manager._sync_attempt_count == 0


async def test_invalid_active_state_during_initial_load(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that invalid active state during initial load prevents sync and logs once."""
    in_sync_entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)

    # Reset to simulate pre-initial-load state
    in_sync_entity_obj._sync_manager._state = SyncState.LOADING
    in_sync_entity_obj._sync_manager._logged_invalid_state = False

    # Set active entity to an invalid state (not ON or OFF)
    hass.states.async_set(SLOT_1_ACTIVE_ENTITY, "unknown_invalid_state")
    await hass.async_block_till_done()

    # Trigger multiple ticks — should keep retrying without crashing
    for _ in range(5):
        await in_sync_entity_obj._sync_manager._async_tick()
        await hass.async_block_till_done()

    # Warning logged once
    assert in_sync_entity_obj._sync_manager._logged_invalid_state is True

    # Initial state is still not loaded due to invalid state
    assert in_sync_entity_obj._sync_manager._state is SyncState.LOADING


async def test_unexpected_error_during_sync_suspends_lock(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that unexpected errors during sync operation suspend the lock."""
    in_sync_entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    coordinator = lock_provider.coordinator

    # Mock _perform_sync to raise an unexpected exception
    async def mock_perform_sync_unexpected_error(*args, **kwargs):
        # Raise a generic exception (not CodeRejectedError or LockDisconnected)
        raise ValueError("Unexpected programming error")

    with patch.object(
        in_sync_entity_obj._sync_manager,
        "_perform_sync",
        new=mock_perform_sync_unexpected_error,
    ):
        # Force out-of-sync state
        in_sync_entity_obj._sync_manager._state = SyncState.OUT_OF_SYNC
        in_sync_entity_obj._sync_manager._coordinator.data[1] = "wrong_code"

        # Trigger tick to attempt sync (which will fail with unexpected error)
        await in_sync_entity_obj._sync_manager._async_tick()
        await hass.async_block_till_done()

        # Verify that the lock was suspended
        assert in_sync_entity_obj._sync_manager._state is SyncState.SUSPENDED
        assert coordinator.slot_sync_mgrs_suspended is True


async def test_sync_manager_handles_string_slot_num(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test sync manager normalizes string slot keys to int before coordinator lookup."""
    in_sync_entity_obj = get_in_sync_entity_obj(hass, SLOT_1_IN_SYNC_ENTITY)
    manager = in_sync_entity_obj._sync_manager

    assert isinstance(manager._slot_num, int)
    assert manager._slot_num in manager._coordinator.data


# ---------------------------------------------------------------------------
# Adversarial integration tests — exercise real component boundaries
# ---------------------------------------------------------------------------


async def test_coordinator_poll_detects_external_change_and_syncs(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Full round-trip: external code change, coordinator poll, sync manager detects mismatch, sets code."""
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True},
        },
    }

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=config,
        unique_id="Test External Change Poll",
        title="Test LCM",
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    in_sync_entity = "binary_sensor.test_1_code_slot_1_in_sync"
    await async_initial_tick(hass, in_sync_entity)

    # Verify initial state is in sync
    state = hass.states.get(in_sync_entity)
    assert state.state == STATE_ON, "Should start in sync"

    lock_provider = config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    coordinator = lock_provider.coordinator
    service_calls = lock_provider.service_calls
    service_calls.get("set_usercode", []).clear()

    # Simulate external change: someone changed the code on the lock
    lock_provider.codes[1] = "9999"

    # Trigger a real coordinator refresh (polls the mock lock)
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # Coordinator data should now reflect the external change
    assert coordinator.data[1] == "9999"

    # Fire the tick timer to let the sync manager detect the mismatch
    async_fire_time_changed(hass, dt_util.utcnow() + TICK_INTERVAL * 2)
    await hass.async_block_till_done()

    # Sync manager should have called set_usercode to restore "1234"
    assert len(service_calls.get("set_usercode", [])) == 1
    assert service_calls["set_usercode"][0] == (1, "1234", "test1")

    # The mock lock hardware should now have the correct code
    assert lock_provider.codes[1] == "1234"

    # Fire another tick for the sync manager to verify it is back in sync
    async_fire_time_changed(hass, dt_util.utcnow() + TICK_INTERVAL * 3)
    await hass.async_block_till_done()

    state = hass.states.get(in_sync_entity)
    assert state.state == STATE_ON, "Should be back in sync after correction"

    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_push_update_triggers_sync_state_change_on_binary_sensor(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Push update with changed code triggers coordinator listeners, sync manager updates, entity state changes."""
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True},
        },
    }

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=config,
        unique_id="Test Push Update",
        title="Test LCM",
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    in_sync_entity = "binary_sensor.test_1_code_slot_1_in_sync"
    await async_initial_tick(hass, in_sync_entity)

    state = hass.states.get(in_sync_entity)
    assert state.state == STATE_ON, "Should start in sync"

    lock_provider = config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    coordinator = lock_provider.coordinator
    service_calls = lock_provider.service_calls
    service_calls.get("set_usercode", []).clear()

    # Push an update with a wrong code — this simulates a push-based lock
    # reporting that someone changed the code externally
    coordinator.push_update({1: "wrong"})
    await hass.async_block_till_done()

    # The coordinator listener fires _request_sync_check synchronously,
    # which should transition IN_SYNC -> OUT_OF_SYNC immediately
    state = hass.states.get(in_sync_entity)
    assert state.state == STATE_OFF, (
        "In-sync binary sensor should be off after push update with wrong code"
    )

    sync_status = state.attributes.get("sync_status")
    assert sync_status == "out_of_sync", (
        f"Expected sync_status 'out_of_sync', got '{sync_status}'"
    )

    # Fire a tick to let the sync manager correct it
    # Also update the mock lock codes so set_usercode can work
    lock_provider.codes[1] = "wrong"
    async_fire_time_changed(hass, dt_util.utcnow() + TICK_INTERVAL * 2)
    await hass.async_block_till_done()

    # Sync manager should have restored the configured PIN
    assert len(service_calls.get("set_usercode", [])) == 1
    assert service_calls["set_usercode"][0] == (1, "1234", "test1")
    assert lock_provider.codes[1] == "1234"

    # Fire another tick to verify back in sync
    async_fire_time_changed(hass, dt_util.utcnow() + TICK_INTERVAL * 3)
    await hass.async_block_till_done()

    state = hass.states.get(in_sync_entity)
    assert state.state == STATE_ON, "Should be back in sync after tick"

    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_suspension_propagates_to_out_of_sync_managers_on_next_tick(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """When one slot suspends the lock, other out-of-sync slots transition to SUSPENDED on next tick."""
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True},
            2: {CONF_NAME: "test2", CONF_PIN: "5678", CONF_ENABLED: True},
        },
    }

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=config,
        unique_id="Test Suspension Propagation",
        title="Test LCM",
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    in_sync_slot_1 = "binary_sensor.test_1_code_slot_1_in_sync"
    in_sync_slot_2 = "binary_sensor.test_1_code_slot_2_in_sync"
    await async_initial_tick(hass, in_sync_slot_1)
    await async_initial_tick(hass, in_sync_slot_2)

    lock_provider = config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    coordinator = lock_provider.coordinator

    entity_obj_1 = get_in_sync_entity_obj(hass, in_sync_slot_1)
    entity_obj_2 = get_in_sync_entity_obj(hass, in_sync_slot_2)

    mgr_1 = entity_obj_1._sync_manager
    mgr_2 = entity_obj_2._sync_manager

    # Make slot 1 out of sync by changing the lock code externally
    lock_provider.codes[1] = "9999"
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # Make slot 2's sync fail with an unexpected error
    with patch.object(
        lock_provider,
        "async_internal_set_usercode",
        AsyncMock(side_effect=ValueError("Unexpected hardware error")),
    ):
        # Also make slot 2 out of sync
        lock_provider.codes[2] = "0000"
        await coordinator.async_refresh()
        await hass.async_block_till_done()

        # Fire tick — slot 2 tries to sync, hits generic exception, suspends lock
        async_fire_time_changed(hass, dt_util.utcnow() + TICK_INTERVAL * 2)
        await hass.async_block_till_done()

    # Verify the lock is suspended
    assert coordinator.slot_sync_mgrs_suspended is True

    # One of the managers should be SUSPENDED (the one that hit the error)
    # The other should transition to SUSPENDED on the next tick
    suspended_count = sum(1 for m in (mgr_1, mgr_2) if m._state is SyncState.SUSPENDED)
    assert suspended_count >= 1, "At least one manager should be SUSPENDED"

    # Fire another tick so the other slot transitions to SUSPENDED too
    async_fire_time_changed(hass, dt_util.utcnow() + TICK_INTERVAL * 3)
    await hass.async_block_till_done()

    # Both managers should now be SUSPENDED
    assert mgr_1._state is SyncState.SUSPENDED, (
        f"Expected slot 1 SUSPENDED, got {mgr_1._state}"
    )
    assert mgr_2._state is SyncState.SUSPENDED, (
        f"Expected slot 2 SUSPENDED, got {mgr_2._state}"
    )

    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_drift_check_detects_external_change_and_triggers_sync(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Hard refresh detects out-of-band code change, coordinator updates, sync manager re-syncs."""
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True},
        },
    }

    config_entry = MockConfigEntry(
        domain=DOMAIN,
        data=config,
        unique_id="Test Drift Check",
        title="Test LCM",
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    in_sync_entity = "binary_sensor.test_1_code_slot_1_in_sync"
    await async_initial_tick(hass, in_sync_entity)

    state = hass.states.get(in_sync_entity)
    assert state.state == STATE_ON, "Should start in sync"

    lock_provider = config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    coordinator = lock_provider.coordinator
    service_calls = lock_provider.service_calls
    service_calls.get("set_usercode", []).clear()

    # Simulate external change (keypad programming)
    lock_provider.codes[1] = "5555"

    # Call the drift check directly — this is what the periodic timer calls
    await coordinator._async_drift_check(dt_util.utcnow())
    await hass.async_block_till_done()

    # Coordinator data should now have the drifted value
    assert coordinator.data[1] == "5555"

    # Fire tick to let the sync manager detect and correct
    async_fire_time_changed(hass, dt_util.utcnow() + TICK_INTERVAL * 2)
    await hass.async_block_till_done()

    # Sync manager should have restored the configured PIN
    assert len(service_calls.get("set_usercode", [])) == 1
    assert service_calls["set_usercode"][0] == (1, "1234", "test1")
    assert lock_provider.codes[1] == "1234"

    # Verify back in sync
    async_fire_time_changed(hass, dt_util.utcnow() + TICK_INTERVAL * 3)
    await hass.async_block_till_done()

    state = hass.states.get(in_sync_entity)
    assert state.state == STATE_ON, "Should be in sync after drift correction"

    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_sync_manager_handles_code_sensor_unknown_state_on_startup(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Sync manager stays in LOADING when code sensor is STATE_UNKNOWN and resolves after first poll."""
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True},
        },
    }

    # Use a mutable container to track call count (avoiding nonlocal)
    state = {"call_count": 0, "original": MockLCMLock.async_get_usercodes}

    async def failing_then_succeeding_get_usercodes(self_lock):
        """Fail on first calls, succeed on subsequent calls."""
        state["call_count"] += 1
        if state["call_count"] <= 2:
            raise LockCodeManagerError("Connection failed")
        return await state["original"](self_lock)

    with patch.object(
        MockLCMLock,
        "async_get_usercodes",
        failing_then_succeeding_get_usercodes,
    ):
        config_entry = MockConfigEntry(
            domain=DOMAIN,
            data=config,
            unique_id="Test Unknown State",
            title="Test LCM",
        )
        config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    in_sync_entity = "binary_sensor.test_1_code_slot_1_in_sync"
    entity_obj = get_in_sync_entity_obj(hass, in_sync_entity)
    mgr = entity_obj._sync_manager

    # Sync manager should still be in LOADING since code sensor has no data
    assert mgr._state is SyncState.LOADING, f"Expected LOADING state, got {mgr._state}"

    # Now let the coordinator succeed on the next poll (patch is no longer active)
    lock_provider = config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    coordinator = lock_provider.coordinator
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    # Trigger a tick so the sync manager can process the new data
    async_fire_time_changed(hass, dt_util.utcnow() + TICK_INTERVAL * 2)
    await hass.async_block_till_done()

    # Sync manager should have transitioned out of LOADING
    assert mgr._state is not SyncState.LOADING, (
        f"Expected sync manager to leave LOADING after successful poll, "
        f"but state is still {mgr._state}"
    )
    # Should be IN_SYNC since the lock already has the right code
    assert mgr._state is SyncState.IN_SYNC, f"Expected IN_SYNC, got {mgr._state}"

    await hass.config_entries.async_unload(config_entry.entry_id)
