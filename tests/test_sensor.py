"""Test sensor platform."""

import logging

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import CONF_LOCKS, CONF_SLOTS, DOMAIN
from custom_components.lock_code_manager.domain.credentials import pin_address
from custom_components.lock_code_manager.domain.locks import async_create_lock_instance
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers import BaseLock

from .common import LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID, code_entity_id

_LOGGER = logging.getLogger(__name__)


async def test_sensor_entity(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test sensor entity shows lock code values."""
    for code_slot, pin in ((1, "1234"), (2, "5678")):
        state = hass.states.get(
            code_entity_id(hass, lock_code_manager_config_entry, code_slot)
        )
        assert state
        assert state.state == pin
        state = hass.states.get(
            code_entity_id(
                hass, lock_code_manager_config_entry, code_slot, LOCK_2_ENTITY_ID
            )
        )
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
    state = hass.states.get(code_entity_id(hass, lock_code_manager_config_entry, 1))
    assert state is not None
    assert state.state == ""

    # Unreadable credential -> sensor resolves to expected PIN from config
    coordinator.async_set_updated_data({pin_address(1): SlotCredential.unreadable()})
    await hass.async_block_till_done()
    state = hass.states.get(code_entity_id(hass, lock_code_manager_config_entry, 1))
    assert state is not None
    assert state.state == "1234"

    # Known credential -> sensor shows the code
    coordinator.async_set_updated_data({pin_address(1): SlotCredential.known("5678")})
    await hass.async_block_till_done()
    state = hass.states.get(code_entity_id(hass, lock_code_manager_config_entry, 1))
    assert state is not None
    assert state.state == "5678"


async def test_sensor_unreadable_code_on_a_shared_lock_shows_its_entrys_pin(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    An unreadable code falls back to the PIN of the entry that owns the slot.

    The second entry shares the first entry's lock, and with it the lock's
    coordinator, which was created for the first entry.
    """
    second_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [LOCK_1_ENTITY_ID],
            CONF_SLOTS: {
                3: {CONF_NAME: "shared3", CONF_PIN: "2468", CONF_ENABLED: True}
            },
        },
        unique_id="Shared Sensor",
    )
    second_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(second_entry.entry_id)
    await hass.async_block_till_done()
    coordinator = second_entry.runtime_data.locks[LOCK_1_ENTITY_ID].coordinator
    assert coordinator is not None
    assert coordinator.config_entry is lock_code_manager_config_entry

    coordinator.async_set_updated_data({pin_address(3): SlotCredential.unreadable()})
    await hass.async_block_till_done()

    state = hass.states.get(code_entity_id(hass, second_entry, 3))
    assert state is not None
    assert state.state == "2468"

    await hass.config_entries.async_unload(second_entry.entry_id)


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
