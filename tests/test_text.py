"""Test text platform."""

import logging

from homeassistant.components.text import (
    ATTR_VALUE,
    DOMAIN as TEXT_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF
from homeassistant.core import HomeAssistant

from .common import SLOT_2_ENABLED_ENTITY, SLOT_2_NAME_ENTITY, SLOT_2_PIN_ENTITY

_LOGGER = logging.getLogger(__name__)


async def test_text_entities(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test text entities."""
    state = hass.states.get(SLOT_2_NAME_ENTITY)
    assert state
    assert state.state == "test2"

    state = hass.states.get(SLOT_2_PIN_ENTITY)
    assert state
    assert state.state == "5678"

    await hass.services.async_call(
        TEXT_DOMAIN,
        SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "0987"},
        target={ATTR_ENTITY_ID: SLOT_2_PIN_ENTITY},
        blocking=True,
    )

    state = hass.states.get(SLOT_2_PIN_ENTITY)
    assert state
    assert state.state == "0987"

    # Clearing a PIN on an enabled slot should auto-disable the slot and clear the PIN
    await hass.services.async_call(
        TEXT_DOMAIN,
        SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: ""},
        target={ATTR_ENTITY_ID: SLOT_2_PIN_ENTITY},
        blocking=True,
    )

    state = hass.states.get(SLOT_2_PIN_ENTITY)
    assert state
    assert state.state == ""

    state = hass.states.get(SLOT_2_ENABLED_ENTITY)
    assert state
    assert state.state == STATE_OFF


async def test_whitespace_pin_normalized_to_empty(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that a whitespace-only PIN is normalized to empty and auto-disables the slot."""
    # First verify the slot is enabled and has a PIN
    state = hass.states.get(SLOT_2_PIN_ENTITY)
    assert state
    assert state.state == "5678"

    # Set a whitespace-only PIN — should normalize to "" and auto-disable
    await hass.services.async_call(
        TEXT_DOMAIN,
        SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "   "},
        target={ATTR_ENTITY_ID: SLOT_2_PIN_ENTITY},
        blocking=True,
    )

    state = hass.states.get(SLOT_2_PIN_ENTITY)
    assert state
    assert state.state == ""

    state = hass.states.get(SLOT_2_ENABLED_ENTITY)
    assert state
    assert state.state == STATE_OFF
