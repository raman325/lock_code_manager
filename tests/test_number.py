"""Test number platform."""

import logging

from homeassistant.const import ATTR_ENTITY_ID, ATTR_STATE, STATE_LOCKED, STATE_UNLOCKED
from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.const import (
    ATTR_ACTION_TEXT,
    ATTR_CODE_SLOT,
    ATTR_CODE_SLOT_NAME,
    ATTR_FROM,
    ATTR_NOTIFICATION_SOURCE,
    ATTR_TO,
    EVENT_LOCK_USERCODE_USED,
)

from .common import LOCK_1_ENTITY_ID

_LOGGER = logging.getLogger(__name__)


async def test_number_entity(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test number entity."""
    state = hass.states.get("number.code_slot_2_number_of_uses")
    assert state
    assert state.state == "5"

    hass.bus.async_fire(
        EVENT_LOCK_USERCODE_USED,
        {
            ATTR_NOTIFICATION_SOURCE: "event",
            ATTR_ENTITY_ID: LOCK_1_ENTITY_ID,
            ATTR_STATE: STATE_UNLOCKED,
            ATTR_ACTION_TEXT: "test",
            ATTR_CODE_SLOT: 2,
            ATTR_CODE_SLOT_NAME: "test2",
            ATTR_FROM: STATE_LOCKED,
            ATTR_TO: STATE_UNLOCKED,
        },
    )

    await hass.async_block_till_done()

    state = hass.states.get("number.code_slot_2_number_of_uses")
    assert state
    assert state.state == "4"
