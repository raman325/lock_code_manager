"""Test number platform."""

import logging

from homeassistant.components.lock import LockState
from homeassistant.const import ATTR_ENTITY_ID, ATTR_STATE
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

from .common import LOCK_1_ENTITY_ID, SLOT_2_NUMBER_OF_USES_ENTITY

_LOGGER = logging.getLogger(__name__)


async def test_number_entity(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test number entity."""
    state = hass.states.get(SLOT_2_NUMBER_OF_USES_ENTITY)
    assert state
    assert state.state == "5"

    hass.bus.async_fire(
        EVENT_LOCK_STATE_CHANGED,
        {
            ATTR_NOTIFICATION_SOURCE: "event",
            ATTR_ENTITY_ID: LOCK_1_ENTITY_ID,
            ATTR_STATE: LockState.UNLOCKED,
            ATTR_ACTION_TEXT: "test",
            ATTR_CODE_SLOT: 2,
            ATTR_CODE_SLOT_NAME: "test2",
            ATTR_FROM: LockState.LOCKED,
            ATTR_TO: LockState.UNLOCKED,
        },
    )

    await hass.async_block_till_done()

    state = hass.states.get(SLOT_2_NUMBER_OF_USES_ENTITY)
    assert state
    assert state.state == "4"
