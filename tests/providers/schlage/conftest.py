"""Schlage provider test fixtures."""

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
from custom_components.lock_code_manager.providers.schlage import (
    SCHLAGE_DOMAIN,
    SchlageLock,
)

LOCK_ENTITY_ID = "lock.schlage_test_schlage_lock"


@pytest.fixture
async def schlage_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create a Schlage config entry."""
    entry = MockConfigEntry(domain=SCHLAGE_DOMAIN)
    entry.add_to_hass(hass)
    entry._async_set_state(hass, entry.state, None)
    return entry


@pytest.fixture
async def schlage_lock(
    hass: HomeAssistant, schlage_config_entry: MockConfigEntry
) -> SchlageLock:
    """Create a SchlageLock instance with a registered lock entity."""
    entity_reg = er.async_get(hass)
    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "schlage",
        "test_schlage_lock",
        config_entry=schlage_config_entry,
    )
    return SchlageLock(
        hass,
        dr.async_get(hass),
        entity_reg,
        schlage_config_entry,
        lock_entity,
    )


@pytest.fixture
async def lcm_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create a Lock Code Manager config entry that manages slots 1 and 2."""
    config = {
        CONF_LOCKS: [LOCK_ENTITY_ID],
        CONF_SLOTS: {
            1: {CONF_NAME: "slot1", CONF_PIN: "1234", CONF_ENABLED: True},
            2: {CONF_NAME: "slot2", CONF_PIN: "5678", CONF_ENABLED: True},
        },
    }
    entry = MockConfigEntry(domain=DOMAIN, data=config, unique_id="test_schlage_lcm")
    entry.add_to_hass(hass)
    return entry


# --- Alias fixtures for shared test mixins ---


@pytest.fixture
def provider_lock(schlage_lock: SchlageLock) -> SchlageLock:
    """Alias schlage_lock for shared test mixins."""
    return schlage_lock


@pytest.fixture
def provider_config_entry(schlage_config_entry: MockConfigEntry) -> MockConfigEntry:
    """Alias schlage_config_entry for shared test mixins."""
    return schlage_config_entry


@pytest.fixture
def provider_domain() -> str:
    """Return the provider integration domain."""
    return SCHLAGE_DOMAIN


@pytest.fixture
def provider_lock_class() -> type[SchlageLock]:
    """Return the provider lock class."""
    return SchlageLock
