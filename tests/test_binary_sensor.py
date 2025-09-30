"""Test binary sensor platform."""

import copy
from datetime import timedelta
import logging

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

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
)
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.lock_code_manager.const import (
    CONF_CALENDAR,
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
)

from .common import (
    ACTIVE_ENTITY,
    BASE_CONFIG,
    ENABLED_ENTITY,
    LOCK_1_ENTITY_ID,
    LOCK_DATA,
    NUMBER_OF_USES_ENTITY,
    PIN_ENTITY,
    MockLCMLock,
)

_LOGGER = logging.getLogger(__name__)


async def test_binary_sensor_entity(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test sensor entity."""
    calendar_1, calendar_2 = hass.data["lock_code_manager_calendars"]
    state = hass.states.get("calendar.test_1")
    assert state
    assert state.state == STATE_OFF

    state = hass.states.get(ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_OFF

    now = dt_util.utcnow()
    start = now - timedelta(hours=1)
    end = now + timedelta(hours=1)

    cal_event = calendar_1.create_event(dtstart=start, dtend=end, summary="test")
    await hass.async_block_till_done()

    state = hass.states.get(ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_ON

    calendar_1.delete_event(cal_event.uid)
    await hass.async_block_till_done()

    state = hass.states.get(ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_OFF

    calendar_1.create_event(dtstart=start, dtend=end, summary="test")
    await hass.async_block_till_done()

    state = hass.states.get(ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_ON

    await hass.services.async_call(
        NUMBER_DOMAIN,
        NUMBER_SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: 0},
        target={ATTR_ENTITY_ID: NUMBER_OF_USES_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get(ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_OFF

    await hass.services.async_call(
        NUMBER_DOMAIN,
        NUMBER_SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: 5},
        target={ATTR_ENTITY_ID: NUMBER_OF_USES_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get(ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_ON

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        target={ATTR_ENTITY_ID: ENABLED_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get(ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_OFF

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON,
        target={ATTR_ENTITY_ID: ENABLED_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get(ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_ON

    await hass.services.async_call(
        TEXT_DOMAIN,
        TEXT_SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "0987"},
        target={ATTR_ENTITY_ID: PIN_ENTITY},
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

    state = hass.states.get(ACTIVE_ENTITY)
    assert state
    assert state.state == STATE_OFF


async def test_startup_no_code_flapping_when_synced(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that codes aren't unnecessarily cleared/set on startup when already synced."""
    # Get the in-sync binary sensor for lock 1, slot 2
    in_sync_entity = "binary_sensor.test_1_code_slot_2_pin_synced"

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
    in_sync_entity = "binary_sensor.test_1_code_slot_1_pin_synced"

    # Verify the entity exists and detects out-of-sync state
    state = hass.states.get(in_sync_entity)
    assert state, f"Entity {in_sync_entity} not found"

    # Initially should be out of sync because lock has "1234" but config wants "9999"
    assert (
        state.state == STATE_OFF
    ), "Codes should be detected as out of sync on startup"

    # Verify that NO set_usercode was called during initial startup
    # (the fix prevents operations on first load)
    service_calls = hass.data[LOCK_DATA][LOCK_1_ENTITY_ID]["service_calls"]
    set_calls = service_calls.get("set_usercode", [])
    assert len(set_calls) == 0, (
        f"Expected no set_usercode calls during initial startup, "
        f"but found: {set_calls}"
    )

    # Now trigger the async_update method which should detect the out-of-sync state
    # and correct it (this simulates the polling behavior)
    in_sync_binary_sensor = hass.states.get(in_sync_entity)
    assert in_sync_binary_sensor

    # Wait for the next update cycle
    await hass.helpers.entity_component.async_update_entity(in_sync_entity)
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
