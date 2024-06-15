"""Test binary sensor platform."""

import copy
from datetime import timedelta
import logging

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

from custom_components.lock_code_manager.const import CONF_CALENDAR, CONF_SLOTS

from .common import (
    ACTIVE_ENTITY,
    BASE_CONFIG,
    ENABLED_ENTITY,
    LOCK_1_ENTITY_ID,
    LOCK_DATA,
    NUMBER_OF_USES_ENTITY,
    PIN_ENTITY,
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
