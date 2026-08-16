"""Test sensor platform."""

import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.domain.credentials import pin_address
from custom_components.lock_code_manager.domain.locks import async_create_lock_instance
from custom_components.lock_code_manager.domain.models import SlotCredential
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
    coordinator.async_set_updated_data({pin_address(1): SlotCredential.empty()})
    await hass.async_block_till_done()
    state = hass.states.get("sensor.test_1_code_slot_1")
    assert state is not None
    assert state.state == ""

    # Unreadable credential -> sensor resolves to expected PIN from config
    coordinator.async_set_updated_data({pin_address(1): SlotCredential.unreadable()})
    await hass.async_block_till_done()
    state = hass.states.get("sensor.test_1_code_slot_1")
    assert state is not None
    assert state.state == "1234"

    # Known credential -> sensor shows the code
    coordinator.async_set_updated_data({pin_address(1): SlotCredential.known("5678")})
    await hass.async_block_till_done()
    state = hass.states.get("sensor.test_1_code_slot_1")
    assert state is not None
    assert state.state == "5678"


async def test_add_code_slot_entity_skipped_when_lock_has_no_coordinator(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """A lock whose provider setup has not finished (no coordinator yet) is skipped.

    ``add_code_slot_entities`` is invoked via the lock-slot-adder callback
    registry once a lock has been set up. This exercises the defensive
    ``if coordinator is None: return`` for a lock instance that has not
    completed ``async_setup_internal`` -- the code sensor is simply not
    created rather than crashing on a ``None`` coordinator.
    """
    entry = lock_code_manager_config_entry
    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)
    fresh_lock = async_create_lock_instance(
        hass, dev_reg, ent_reg, entry, LOCK_1_ENTITY_ID
    )
    assert fresh_lock.coordinator is None

    entities_before = set(hass.states.async_entity_ids("sensor"))
    entry.runtime_data.callbacks.invoke_lock_slot_adders(fresh_lock, 1, ent_reg)
    await hass.async_block_till_done()

    assert set(hass.states.async_entity_ids("sensor")) == entities_before
