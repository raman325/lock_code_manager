"""Test sensor platform."""

import logging

from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.models import SlotCredential
from custom_components.lock_code_manager.providers import BaseLock

from .common import LOCK_1_ENTITY_ID

_LOGGER = logging.getLogger(__name__)


async def test_sensor_entity(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test sensor entity shows lock code values."""
    for code_slot, pin in ((1, "1234"), (2, "5678")):
        state = hass.states.get(f"sensor.test_1_code_slot_{code_slot}")
        assert state
        assert state.state == pin
        state = hass.states.get(f"sensor.test_2_code_slot_{code_slot}")
        assert state
        assert state.state == pin


async def test_sensor_native_value_with_slot_code(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test sensor native_value handles empty and unreadable credentials."""
    lock: BaseLock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    coordinator = lock.coordinator
    assert coordinator is not None

    # Empty credential -> sensor shows empty string
    coordinator.async_set_updated_data({1: SlotCredential.empty()})
    await hass.async_block_till_done()
    state = hass.states.get("sensor.test_1_code_slot_1")
    assert state is not None
    assert state.state == ""

    # Unreadable credential -> sensor resolves to expected PIN from config
    coordinator.async_set_updated_data({1: SlotCredential.unreadable()})
    await hass.async_block_till_done()
    state = hass.states.get("sensor.test_1_code_slot_1")
    assert state is not None
    assert state.state == "1234"

    # Known credential -> sensor shows the code
    coordinator.async_set_updated_data({1: SlotCredential.known("5678")})
    await hass.async_block_till_done()
    state = hass.states.get("sensor.test_1_code_slot_1")
    assert state is not None
    assert state.state == "5678"
