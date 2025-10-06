"""Test sensor platform."""

import logging

from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_component import async_update_entity

_LOGGER = logging.getLogger(__name__)


async def test_sensor_entity(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test sensor entity."""
    # Trigger update for slot 2 to clear the code since it's inactive
    # (slot 2 has calendar configured but no active event)
    await async_update_entity(hass, "binary_sensor.test_1_code_slot_2_in_sync")
    await async_update_entity(hass, "binary_sensor.test_2_code_slot_2_in_sync")
    await hass.async_block_till_done()

    for code_slot, pin in ((1, "1234"), (2, STATE_UNAVAILABLE)):
        state = hass.states.get(f"sensor.test_1_code_slot_{code_slot}")
        assert state
        assert state.state == pin
        state = hass.states.get(f"sensor.test_2_code_slot_{code_slot}")
        assert state
        assert state.state == pin
