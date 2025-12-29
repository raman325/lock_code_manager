"""Test sensor platform."""

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_component import async_update_entity

_LOGGER = logging.getLogger(__name__)


async def test_sensor_entity(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test sensor entity."""
    # Trigger coordinator-backed sensor updates (mirrors polling behavior)
    await async_update_entity(hass, "sensor.test_1_code_slot_2")
    await async_update_entity(hass, "sensor.test_2_code_slot_2")
    await hass.async_block_till_done()

    for code_slot, pin in ((1, "1234"), (2, "5678")):
        state = hass.states.get(f"sensor.test_1_code_slot_{code_slot}")
        assert state
        assert state.state == pin
        state = hass.states.get(f"sensor.test_2_code_slot_{code_slot}")
        assert state
        assert state.state == pin
