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
        config_entry,
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
        config_entry,
        lock_entity,
    )
    await lock.async_setup_internal(config_entry)
    return lock
