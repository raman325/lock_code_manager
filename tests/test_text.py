"""Test text platform."""

import logging

from homeassistant.components.persistent_notification import (
    _async_get_or_create_notifications,
)
from homeassistant.components.text import (
    ATTR_VALUE,
    DOMAIN as TEXT_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant

from .common import SLOT_2_NAME_ENTITY, SLOT_2_PIN_ENTITY

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

    # Test that notification gets created and state doesn't change when setting an empty PIN
    await hass.services.async_call(
        TEXT_DOMAIN,
        SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: ""},
        target={ATTR_ENTITY_ID: SLOT_2_PIN_ENTITY},
        blocking=True,
    )

    assert len(_async_get_or_create_notifications(hass)) == 1

    state = hass.states.get(SLOT_2_PIN_ENTITY)
    assert state
    assert state.state == "0987"
