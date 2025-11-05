"""Test binary sensor platform."""

import copy
from datetime import timedelta
import logging

from pytest_homeassistant_custom_component.common import async_fire_time_changed

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
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.lock_code_manager.const import (
    CONF_CALENDAR,
    CONF_SLOTS,
    COORDINATORS,
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
    PIN_SYNCED_ENTITY,
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


async def test_handles_disconnected_lock_on_set(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    caplog,
):
    """Test that binary sensor handles LockDisconnected exception when setting usercode."""
    # Verify initial state - slot should be active and in sync
    active_state = hass.states.get(ACTIVE_ENTITY)
    assert active_state.state == STATE_ON

    synced_state = hass.states.get(PIN_SYNCED_ENTITY)
    assert synced_state.state == STATE_ON

    # Change PIN to trigger sync
    await hass.services.async_call(
        TEXT_DOMAIN,
        TEXT_SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "9999"},
        target={ATTR_ENTITY_ID: PIN_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()

    # Synced state should now be off (out of sync)
    synced_state = hass.states.get(PIN_SYNCED_ENTITY)
    assert synced_state.state == STATE_OFF

    # Get the lock provider instance and disconnect it
    coordinators = hass.data[DOMAIN][lock_code_manager_config_entry.entry_id][
        COORDINATORS
    ]
    lock_provider = coordinators[LOCK_1_ENTITY_ID]._lock
    lock_provider.set_connected(False)

    # Trigger coordinator refresh to attempt sync
    await coordinators[LOCK_1_ENTITY_ID].async_refresh()
    await hass.async_block_till_done()

    # Fire time changed to trigger binary sensor update (which calls async_update)
    async_fire_time_changed(
        hass, dt_util.utcnow() + coordinators[LOCK_1_ENTITY_ID].update_interval
    )
    await hass.async_block_till_done()

    # Binary sensor should remain off due to failed sync
    synced_state = hass.states.get(PIN_SYNCED_ENTITY)
    assert synced_state.state == STATE_OFF

    # Verify debug log was created
    assert "Unable to set usercode" in caplog.text
    assert "lock not connected" in caplog.text

    # Verify the code wasn't actually changed (still old value)
    assert hass.data[LOCK_DATA][LOCK_1_ENTITY_ID]["codes"][2] == "5678"


async def test_handles_disconnected_lock_on_clear(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    caplog,
):
    """Test that binary sensor handles LockDisconnected exception when clearing usercode."""
    # Verify initial state - slot should be active and in sync
    active_state = hass.states.get(ACTIVE_ENTITY)
    assert active_state.state == STATE_ON

    synced_state = hass.states.get(PIN_SYNCED_ENTITY)
    assert synced_state.state == STATE_ON

    # Disable the slot to trigger clear
    hass.states.async_set(ACTIVE_ENTITY, STATE_OFF)
    await hass.async_block_till_done()

    # Get the lock provider instance and disconnect it
    coordinators = hass.data[DOMAIN][lock_code_manager_config_entry.entry_id][
        COORDINATORS
    ]
    lock_provider = coordinators[LOCK_1_ENTITY_ID]._lock
    lock_provider.set_connected(False)

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
    assert hass.data[LOCK_DATA][LOCK_1_ENTITY_ID]["codes"].get(2) == "5678"


async def test_syncs_when_lock_reconnects(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that binary sensor successfully syncs once lock reconnects."""
    # Change PIN to trigger sync
    await hass.services.async_call(
        TEXT_DOMAIN,
        TEXT_SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "9999"},
        target={ATTR_ENTITY_ID: PIN_ENTITY},
        blocking=True,
    )
    await hass.async_block_till_done()

    # Synced state should be off
    synced_state = hass.states.get(PIN_SYNCED_ENTITY)
    assert synced_state.state == STATE_OFF

    # Get the lock provider and disconnect it
    coordinators = hass.data[DOMAIN][lock_code_manager_config_entry.entry_id][
        COORDINATORS
    ]
    lock_provider = coordinators[LOCK_1_ENTITY_ID]._lock
    lock_provider.set_connected(False)

    # Attempt sync while disconnected - should fail silently
    await coordinators[LOCK_1_ENTITY_ID].async_refresh()
    await hass.async_block_till_done()

    # Fire time changed to trigger update
    async_fire_time_changed(
        hass, dt_util.utcnow() + coordinators[LOCK_1_ENTITY_ID].update_interval
    )
    await hass.async_block_till_done()

    # Should still be out of sync
    synced_state = hass.states.get(PIN_SYNCED_ENTITY)
    assert synced_state.state == STATE_OFF

    # Reconnect the lock
    lock_provider.set_connected(True)

    # Trigger another sync
    await coordinators[LOCK_1_ENTITY_ID].async_refresh()
    await hass.async_block_till_done()

    # Fire time changed again
    async_fire_time_changed(
        hass, dt_util.utcnow() + coordinators[LOCK_1_ENTITY_ID].update_interval
    )
    await hass.async_block_till_done()

    # Now it should sync successfully
    synced_state = hass.states.get(PIN_SYNCED_ENTITY)
    assert synced_state.state == STATE_ON

    # Verify the code was actually changed
    assert hass.data[LOCK_DATA][LOCK_1_ENTITY_ID]["codes"][2] == "9999"
