"""Test event platform."""

import logging

from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_STATE,
    STATE_LOCKED,
    STATE_UNKNOWN,
    STATE_UNLOCKED,
)
from homeassistant.core import Event, HomeAssistant

from custom_components.lock_code_manager.const import (
    ATTR_ACTION_TEXT,
    ATTR_CODE_SLOT,
    ATTR_CODE_SLOT_NAME,
    ATTR_FROM,
    ATTR_NOTIFICATION_SOURCE,
    ATTR_TO,
    CONF_LOCKS,
    DOMAIN,
)
from custom_components.lock_code_manager.providers import BaseLock

from .common import EVENT_ENTITY, LOCK_1_ENTITY_ID

_LOGGER = logging.getLogger(__name__)


async def test_event_entity(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test event entity."""
    state = hass.states.get(EVENT_ENTITY)
    assert state
    assert state.state == STATE_UNKNOWN

    lock: BaseLock = hass.data[DOMAIN][lock_code_manager_config_entry.entry_id][
        CONF_LOCKS
    ][LOCK_1_ENTITY_ID]

    lock.async_fire_code_slot_event(2, False, "test", Event("zwave_js_notification"))

    await hass.async_block_till_done()

    state = hass.states.get(EVENT_ENTITY)
    assert state
    assert state.state != STATE_UNKNOWN

    assert state.attributes[ATTR_NOTIFICATION_SOURCE] == "event"
    assert state.attributes[ATTR_ENTITY_ID] == LOCK_1_ENTITY_ID
    assert state.attributes[ATTR_STATE] == STATE_UNLOCKED
    assert state.attributes[ATTR_ACTION_TEXT] == "test"
    assert state.attributes[ATTR_CODE_SLOT] == 2
    assert state.attributes[ATTR_CODE_SLOT_NAME] == "test2"
    assert state.attributes[ATTR_FROM] == STATE_LOCKED
    assert state.attributes[ATTR_TO] == STATE_UNLOCKED
