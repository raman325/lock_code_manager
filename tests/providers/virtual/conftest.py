"""Virtual provider test fixtures."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import (
    CONF_ENABLED,
    CONF_LOCKS,
    CONF_NAME,
    CONF_PIN,
    CONF_SLOTS,
    DOMAIN,
)
from custom_components.lock_code_manager.providers.virtual import VirtualLock

VIRTUAL_LOCK_ENTITY_ID = "lock.virtual_test_virtual"

# LCM config: one virtual lock, two slots
VIRTUAL_LCM_CONFIG = {
    CONF_LOCKS: [VIRTUAL_LOCK_ENTITY_ID],
    CONF_SLOTS: {
        1: {CONF_NAME: "slot1", CONF_PIN: "1234", CONF_ENABLED: True},
        2: {CONF_NAME: "slot2", CONF_PIN: "5678", CONF_ENABLED: True},
    },
}


@pytest.fixture
async def virtual_lock(hass: HomeAssistant) -> VirtualLock:
    """Create a VirtualLock instance with a registered lock entity."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock",
        config_entry=config_entry,
    )

    lock = VirtualLock(
        hass,
        dr.async_get(hass),
        entity_reg,
        None,
        lock_entity,
    )
    await lock.async_setup_internal(config_entry)
    return lock


@pytest.fixture
async def virtual_lock_with_slots(hass: HomeAssistant) -> VirtualLock:
    """Create a VirtualLock with configured slots for testing get_usercodes."""
    entity_reg = er.async_get(hass)
    lock_entity_id = "lock.test_test_lock_usercodes"

    config = {
        CONF_LOCKS: [lock_entity_id],
        CONF_SLOTS: {
            1: {CONF_NAME: "slot1", CONF_PIN: "1234", CONF_ENABLED: True},
            2: {CONF_NAME: "slot2", CONF_PIN: "5678", CONF_ENABLED: True},
        },
    }
    config_entry = MockConfigEntry(
        domain=DOMAIN, data=config, unique_id="test_virtual_usercodes"
    )
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock_usercodes",
        config_entry=config_entry,
    )

    lock = VirtualLock(
        hass,
        dr.async_get(hass),
        entity_reg,
        None,
        lock_entity,
    )
    await lock.async_setup_internal(config_entry)
    return lock


@pytest.fixture
async def virtual_lock_entity(hass: HomeAssistant) -> er.RegistryEntry:
    """Create a virtual config entry and lock entity.

    The lock entity is registered under the "virtual" platform so that
    LCM's INTEGRATIONS_CLASS_MAP lookup finds it as a VirtualLock.
    """
    virtual_entry = MockConfigEntry(domain="virtual")
    virtual_entry.add_to_hass(hass)

    ent_reg = er.async_get(hass)
    lock_entity = ent_reg.async_get_or_create(
        "lock",
        "virtual",
        "test_virtual",
        config_entry=virtual_entry,
    )

    hass.states.async_set(lock_entity.entity_id, "locked")

    return lock_entity


@pytest.fixture
async def lcm_config_entry(
    hass: HomeAssistant,
    virtual_lock_entity: er.RegistryEntry,
) -> MockConfigEntry:
    """Set up a full LCM config entry managing the virtual lock.

    This goes through the real async_setup_entry path: LCM discovers the
    lock entity is from the virtual platform and instantiates VirtualLock.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN, data=VIRTUAL_LCM_CONFIG, unique_id="test_virtual_e2e"
    )
    lcm_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(lcm_entry.entry_id)
    await hass.async_block_till_done()

    yield lcm_entry

    await hass.config_entries.async_unload(lcm_entry.entry_id)


def get_virtual_lock(hass: HomeAssistant, lcm_entry: MockConfigEntry) -> VirtualLock:
    """Extract the VirtualLock from a loaded LCM config entry."""
    lock = lcm_entry.runtime_data.locks.get(VIRTUAL_LOCK_ENTITY_ID)
    assert lock is not None, f"Lock {VIRTUAL_LOCK_ENTITY_ID} not found in runtime data"
    assert isinstance(lock, VirtualLock)
    return lock


@pytest.fixture
def e2e_virtual_lock(hass: HomeAssistant, lcm_config_entry) -> VirtualLock:
    """Extract the VirtualLock from the LCM config entry."""
    return get_virtual_lock(hass, lcm_config_entry)
