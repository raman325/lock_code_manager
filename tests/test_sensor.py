"""Test sensor platform."""

import logging

from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.models import SlotCode
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
    """Test sensor native_value handles SlotCode.EMPTY and SlotCode.UNREADABLE_CODE."""
    lock: BaseLock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    coordinator = lock.coordinator
    assert coordinator is not None

    # Test SlotCode.EMPTY -> sensor shows empty string
    coordinator.async_set_updated_data({1: SlotCode.EMPTY})
    await hass.async_block_till_done()
    state = hass.states.get("sensor.test_1_code_slot_1")
    assert state is not None
    assert state.state == ""

    # Test SlotCode.UNREADABLE_CODE -> sensor resolves to expected PIN from config
    coordinator.async_set_updated_data({1: SlotCode.UNREADABLE_CODE})
    await hass.async_block_till_done()
    state = hass.states.get("sensor.test_1_code_slot_1")
    assert state is not None
    assert state.state == "1234"

    # Test regular code -> sensor shows the code
    coordinator.async_set_updated_data({1: "5678"})
    await hass.async_block_till_done()
    state = hass.states.get("sensor.test_1_code_slot_1")
    assert state is not None
    assert state.state == "5678"
