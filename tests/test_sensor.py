"""Test sensor platform."""

import logging

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


async def test_sensor_entity(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test sensor entity."""
    for code_slot, pin in ((1, "1234"), (2, STATE_UNAVAILABLE)):
        state = hass.states.get(f"sensor.test_1_code_slot_{code_slot}")
        assert state
        assert state.state == pin
        state = hass.states.get(f"sensor.test_2_code_slot_{code_slot}")
        assert state
        assert state.state == pin
