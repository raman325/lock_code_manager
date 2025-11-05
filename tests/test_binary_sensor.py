"""Test binary sensor platform."""

import copy
from datetime import timedelta
import logging

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE as NUMBER_SERVICE_SET_VALUE,
)
from homeassistant.components.switch import (
    DOMAIN as SWITCH_DOMAIN,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
)
from homeassistant.components.text import (
    DOMAIN as TEXT_DOMAIN,
    SERVICE_SET_VALUE as TEXT_SERVICE_SET_VALUE,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_ENABLED,
    CONF_NAME,
    CONF_PIN,
    STATE_OFF,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_component import async_update_entity
from homeassistant.util import dt as dt_util

from custom_components.lock_code_manager.const import (
    CONF_CALENDAR,
    CONF_LOCKS,
    CONF_SLOTS,
    COORDINATORS,
    DOMAIN,
)

from .common import (
    BASE_CONFIG,
    LOCK_1_ENTITY_ID,
    LOCK_DATA,
    SLOT_1_ACTIVE_ENTITY,
    SLOT_1_IN_SYNC_ENTITY,
    SLOT_1_PIN_ENTITY,
    SLOT_2_ACTIVE_ENTITY,
    SLOT_2_ENABLED_ENTITY,
    SLOT_2_NUMBER_OF_USES_ENTITY,
    SLOT_2_PIN_ENTITY,
    MockLCMLock,
)

_LOGGER = logging.getLogger(__name__)


async def test_binary_sensor_entity(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test sensor entity."""
    calendar_1, _ = hass.data["lock_code_manager_calendars"]
    state = hass.states.get("calendar.test_1")
    assert state
    assert state.state == STATE_OFF

    state = hass.states.get(SLOT_2_ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_OFF

    now = dt_util.utcnow()
    start = now - timedelta(hours=1)
    end = now + timedelta(hours=1)

    cal_event = calendar_1.create_event(dtstart=start, dtend=end, summary="test")
    await hass.async_block_till_done()

    state = hass.states.get(SLOT_2_ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_ON

    calendar_1.delete_event(cal_event.uid)
    await hass.async_block_till_done()

    state = hass.states.get(SLOT_2_ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_OFF

    calendar_1.create_event(dtstart=start, dtend=end, summary="test")
    await hass.async_block_till_done()

    state = hass.states.get(SLOT_2_ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_ON

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

    await hass.services.async_call(
        TEXT_DOMAIN,
        TEXT_SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "0987"},
        target={ATTR_ENTITY_ID: SLOT_2_PIN_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()

    assert hass.data[LOCK_DATA][LOCK_1_ENTITY_ID]["service_calls"]["set_usercode"][
        -1
    ] == (2, "0987", "test2")

    new_config = copy.deepcopy(BASE_CONFIG)
    new_config[CONF_SLOTS][2][CONF_CALENDAR] = "calendar.test_2"

    hass.config_entries.async_update_entry(
        lock_code_manager_config_entry, options=new_config
    )
    await hass.async_block_till_done()

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
    monkeypatch: pytest.MonkeyPatch,
):
    """Test that out-of-sync codes are detected on startup and automatically corrected."""
    # Monkeypatch the helper to use our mock lock
    monkeypatch.setattr(
        "custom_components.lock_code_manager.helpers.INTEGRATIONS_CLASS_MAP",
        {"test": MockLCMLock},
    )

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


async def test_startup_waits_for_valid_active_state(
    hass: HomeAssistant,
    mock_lock_config_entry,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """Test that in-sync entity doesn't load initial state when active entity has invalid state."""
    # Monkeypatch the helper to use our mock lock
    monkeypatch.setattr(
        "custom_components.lock_code_manager.helpers.INTEGRATIONS_CLASS_MAP",
        {"test": MockLCMLock},
    )

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

    caplog.set_level(logging.DEBUG)
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

    # Reset flags to simulate pre-initial-load state
    in_sync_entity_obj._initial_state_loaded = False
    in_sync_entity_obj._attr_is_on = False

    # Set the active entity to unavailable
    hass.states.async_set(active_entity_id, STATE_UNAVAILABLE)
    await hass.async_block_till_done()

    caplog.clear()

    # Trigger update with unavailable active state
    await async_update_entity(hass, in_sync_entity)
    await hass.async_block_till_done()

    # Verify it logged the validation message
    assert any(
        "has invalid state" in record.message
        and "waiting for valid state" in record.message
        for record in caplog.records
    ), "Should log that it's waiting for valid active state"

    # Verify initial state is still not loaded
    assert not in_sync_entity_obj._initial_state_loaded

    await hass.config_entries.async_unload(config_entry.entry_id)


async def test_handles_disconnected_lock_on_set(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    caplog,
):
    """Test that binary sensor handles LockDisconnected exception when setting usercode."""
    # Verify initial state - slot should be active and in sync
    active_state = hass.states.get(SLOT_1_ACTIVE_ENTITY)
    assert active_state.state == STATE_ON

    synced_state = hass.states.get(SLOT_1_IN_SYNC_ENTITY)
    assert synced_state.state == STATE_ON

    # Get the lock provider instance and disconnect it
    coordinators = hass.data[DOMAIN][lock_code_manager_config_entry.entry_id][
        COORDINATORS
    ]
    lock_provider = coordinators[LOCK_1_ENTITY_ID].lock
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
    await coordinators[LOCK_1_ENTITY_ID].async_refresh()
    await hass.async_block_till_done()

    # Fire time changed to trigger binary sensor update (which calls async_update)
    async_fire_time_changed(
        hass, dt_util.utcnow() + coordinators[LOCK_1_ENTITY_ID].update_interval
    )
    await hass.async_block_till_done()

    # Binary sensor should remain off due to failed sync
    synced_state = hass.states.get(SLOT_1_IN_SYNC_ENTITY)
    assert synced_state.state == STATE_OFF

    # Verify debug log was created
    assert "Unable to set usercode" in caplog.text
    assert "lock not connected" in caplog.text

    # Verify the code wasn't actually changed (still old value)
    assert hass.data[LOCK_DATA][LOCK_1_ENTITY_ID]["codes"][1] == "1234"


async def test_handles_disconnected_lock_on_clear(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    caplog,
):
    """Test that binary sensor handles LockDisconnected exception when clearing usercode."""
    # Verify initial state - slot should be active and in sync
    active_state = hass.states.get(SLOT_1_ACTIVE_ENTITY)
    assert active_state.state == STATE_ON

    synced_state = hass.states.get(SLOT_1_IN_SYNC_ENTITY)
    assert synced_state.state == STATE_ON

    # Get the lock provider instance and disconnect it
    coordinators = hass.data[DOMAIN][lock_code_manager_config_entry.entry_id][
        COORDINATORS
    ]
    lock_provider = coordinators[LOCK_1_ENTITY_ID].lock
    lock_provider.set_connected(False)

    # Disable the slot to trigger clear
    hass.states.async_set(SLOT_1_ACTIVE_ENTITY, STATE_OFF)
    await hass.async_block_till_done()

    # Trigger coordinator refresh
    await coordinators[LOCK_1_ENTITY_ID].async_refresh()
    await hass.async_block_till_done()

    # Fire time changed to trigger binary sensor update
    async_fire_time_changed(
        hass, dt_util.utcnow() + coordinators[LOCK_1_ENTITY_ID].update_interval
    )
    await hass.async_block_till_done()

    # Verify debug log was created
    assert "Unable to clear usercode" in caplog.text
    assert "lock not connected" in caplog.text

    # Verify the code wasn't actually cleared (still has value)
    assert hass.data[LOCK_DATA][LOCK_1_ENTITY_ID]["codes"].get(1) == "1234"
