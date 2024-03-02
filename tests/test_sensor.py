"""Test sensor platform."""

import logging

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


async def test_sensor_entity(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test sensor entity."""
    for code_slot, pin in ((1, "1234"), (2, "5678")):
        state = hass.states.get(f"sensor.mock_title_code_slot_{code_slot}_code")
        assert state
        assert state.state == pin
        state = hass.states.get(f"sensor.mock_title_code_slot_{code_slot}_code_2")
        assert state
        assert state.state == pin
