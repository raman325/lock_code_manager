"""Test text platform."""

import logging

from homeassistant.components.persistent_notification import (
    _async_get_or_create_notifications,
)
from homeassistant.components.text import ATTR_VALUE
from homeassistant.components.text import DOMAIN as TEXT_DOMAIN
from homeassistant.components.text import SERVICE_SET_VALUE
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


async def test_text_entities(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test text entities."""
    state = hass.states.get("text.code_slot_1_name")
    assert state
    assert state.state == "test1"

    state = hass.states.get("text.code_slot_1_pin")
    assert state
    assert state.state == "1234"

    await hass.services.async_call(
        TEXT_DOMAIN,
        SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "0987"},
        target={ATTR_ENTITY_ID: "text.code_slot_1_pin"},
        blocking=True,
    )

    state = hass.states.get("text.code_slot_1_pin")
    assert state
    assert state.state == "0987"

    # Test that notification gets created and state doesn't change when setting an empty PIN
    await hass.services.async_call(
        TEXT_DOMAIN,
        SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: ""},
        target={ATTR_ENTITY_ID: "text.code_slot_1_pin"},
        blocking=True,
    )

    assert len(_async_get_or_create_notifications(hass)) == 1

    state = hass.states.get("text.code_slot_1_pin")
    assert state
    assert state.state == "0987"
