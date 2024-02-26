"""Test event platform."""

import logging

from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_STATE,
    STATE_LOCKED,
    STATE_UNKNOWN,
    STATE_UNLOCKED,
)
from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.const import (
    ATTR_ACTION_TEXT,
    ATTR_CODE_SLOT,
    ATTR_CODE_SLOT_NAME,
    ATTR_FROM,
    ATTR_NOTIFICATION_SOURCE,
    ATTR_TO,
    EVENT_LOCK_STATE_CHANGED,
)

from .common import LOCK_1_ENTITY_ID

_LOGGER = logging.getLogger(__name__)


async def test_event_entity(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test event entity."""
    state = hass.states.get("event.code_slot_2")
    assert state
    assert state.state == STATE_UNKNOWN

    event_data = {
        ATTR_NOTIFICATION_SOURCE: "event",
        ATTR_ENTITY_ID: LOCK_1_ENTITY_ID,
        ATTR_STATE: STATE_UNLOCKED,
        ATTR_ACTION_TEXT: "test",
        ATTR_CODE_SLOT: 2,
        ATTR_CODE_SLOT_NAME: "test2",
        ATTR_FROM: STATE_LOCKED,
        ATTR_TO: STATE_UNLOCKED,
    }

    hass.bus.async_fire(EVENT_LOCK_STATE_CHANGED, event_data)

    await hass.async_block_till_done()

    state = hass.states.get("event.code_slot_2")
    assert state
    assert state.state != STATE_UNKNOWN

    assert all(state.attributes[key] == val for key, val in event_data.items())
