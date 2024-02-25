"""Test switch platform."""

import logging

from homeassistant.components.persistent_notification import (
    _async_get_or_create_notifications,
)
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN, SERVICE_TOGGLE
from homeassistant.components.text import (
    ATTR_VALUE,
    DOMAIN as TEXT_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


async def test_switch_entity(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test switch entity."""
    state = hass.states.get("switch.code_slot_1_enabled")
    assert state
    assert state.state == STATE_ON

    # Toggle switch off
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TOGGLE,
        target={ATTR_ENTITY_ID: "switch.code_slot_1_enabled"},
        blocking=True,
    )

    state = hass.states.get("switch.code_slot_1_enabled")
    assert state
    assert state.state == STATE_OFF

    await hass.services.async_call(
        TEXT_DOMAIN,
        SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: ""},
        target={ATTR_ENTITY_ID: "text.code_slot_1_pin"},
        blocking=True,
    )

    # Attempt to toggle switch on. This should fail because the PIN value is empty
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TOGGLE,
        target={ATTR_ENTITY_ID: "switch.code_slot_1_enabled"},
        blocking=True,
    )

    assert len(_async_get_or_create_notifications(hass)) == 1

    state = hass.states.get("switch.code_slot_1_enabled")
    assert state
    assert state.state == STATE_OFF

    # Set PIN value so we can toggle switch on
    await hass.services.async_call(
        TEXT_DOMAIN,
        SERVICE_SET_VALUE,
        service_data={ATTR_VALUE: "1234"},
        target={ATTR_ENTITY_ID: "text.code_slot_1_pin"},
        blocking=True,
    )
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TOGGLE,
        target={ATTR_ENTITY_ID: "switch.code_slot_1_enabled"},
        blocking=True,
    )

    state = hass.states.get("switch.code_slot_1_enabled")
    assert state
    assert state.state == STATE_ON
