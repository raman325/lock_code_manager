"""Test sensor platform."""

from datetime import timedelta
import logging

from homeassistant.components.number import (
    ATTR_VALUE,
    DOMAIN as NUMBER_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.components.switch import (
    DOMAIN as SWITCH_DOMAIN,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


async def test_binary_sensor_entity(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test sensor entity."""
    state = hass.states.get("calendar.test")
    assert state
    assert state.state == STATE_OFF

    state = hass.states.get("binary_sensor.code_slot_2_pin_synced_to_locks")
    assert state
    assert state.state == STATE_OFF

    now = dt_util.utcnow()
    start = now - timedelta(hours=1)
    end = now + timedelta(hours=1)

    cal_event = hass.data["lock_code_manager_calendar"].create_event(
        dtstart=start, dtend=end, summary="test"
    )
    await hass.async_block_till_done()

    state = hass.states.get("binary_sensor.code_slot_2_pin_synced_to_locks")
    assert state
    assert state.state == STATE_ON

    hass.data["lock_code_manager_calendar"].delete_event(cal_event.uid)
    await hass.async_block_till_done()

    state = hass.states.get("binary_sensor.code_slot_2_pin_synced_to_locks")
    assert state
    assert state.state == STATE_OFF

    hass.data["lock_code_manager_calendar"].create_event(
        dtstart=start, dtend=end, summary="test"
    )
    await hass.async_block_till_done()

    state = hass.states.get("binary_sensor.code_slot_2_pin_synced_to_locks")
    assert state
    assert state.state == STATE_ON

    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "0"},
        target={ATTR_ENTITY_ID: "number.code_slot_2_number_of_uses"},
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get("binary_sensor.code_slot_2_pin_synced_to_locks")
    assert state
    assert state.state == STATE_OFF

    await hass.services.async_call(
        NUMBER_DOMAIN,
        SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "5"},
        target={ATTR_ENTITY_ID: "number.code_slot_2_number_of_uses"},
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get("binary_sensor.code_slot_2_pin_synced_to_locks")
    assert state
    assert state.state == STATE_ON

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        target={ATTR_ENTITY_ID: "switch.code_slot_2_enabled"},
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get("binary_sensor.code_slot_2_pin_synced_to_locks")
    assert state
    assert state.state == STATE_OFF

    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_ON,
        target={ATTR_ENTITY_ID: "switch.code_slot_2_enabled"},
        blocking=True,
    )
    await hass.async_block_till_done()

    state = hass.states.get("binary_sensor.code_slot_2_pin_synced_to_locks")
    assert state
    assert state.state == STATE_ON
