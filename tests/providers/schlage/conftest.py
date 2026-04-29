"""Schlage provider test fixtures."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

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
from tests.providers.helpers import register_mock_service

LOCK_ENTITY_ID = "lock.schlage_test_schlage_lock"

# LCM config: one Schlage lock, two slots
SCHLAGE_LCM_CONFIG_SLOTS = {
    1: {CONF_NAME: "slot1", CONF_PIN: "1234", CONF_ENABLED: True},
    2: {CONF_NAME: "slot2", CONF_PIN: "5678", CONF_ENABLED: True},
}


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
async def simple_lcm_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """
    Create a Lock Code Manager config entry that manages slots 1 and 2.

    This is a lightweight entry for unit tests that don't need the full LCM
    setup path. It only adds slot configuration data so that managed_slots
    is populated on the provider.
    """
    config = {
        CONF_LOCKS: [LOCK_ENTITY_ID],
        CONF_SLOTS: SCHLAGE_LCM_CONFIG_SLOTS,
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


# ---------------------------------------------------------------------------
# E2E fixtures -- set up a full LCM config entry on top of a mock Schlage
# integration so the provider is discovered and initialised through the real
# async_setup_entry path.
# ---------------------------------------------------------------------------


@pytest.fixture
async def schlage_lock_entity(hass: HomeAssistant) -> er.RegistryEntry:
    """
    Create a Schlage config entry (LOADED state) and lock entity.

    The lock entity is registered under the "schlage" platform so that
    LCM's INTEGRATIONS_CLASS_MAP lookup finds it as a SchlageLock.
    """
    schlage_entry = MockConfigEntry(domain=SCHLAGE_DOMAIN)
    schlage_entry.add_to_hass(hass)

    ent_reg = er.async_get(hass)
    lock_entity = ent_reg.async_get_or_create(
        "lock",
        "schlage",
        "test_schlage_lock",
        config_entry=schlage_entry,
    )

    hass.states.async_set(lock_entity.entity_id, "locked")

    return lock_entity


@pytest.fixture
def schlage_mock_services(
    hass: HomeAssistant,
    schlage_lock_entity: er.RegistryEntry,
) -> dict[str, AsyncMock]:
    """
    Register mock Schlage services needed for the full LCM setup path.

    Returns a dict of service name to handler so E2E tests can inspect
    call counts or swap side effects.
    """
    entity_id = schlage_lock_entity.entity_id
    handlers: dict[str, AsyncMock] = {}

    # get_codes: called by async_get_usercodes and async_setup (tagging)
    handlers["get_codes"] = AsyncMock(return_value={entity_id: {}})

    # add_code: called by async_set_usercode
    handlers["add_code"] = AsyncMock(return_value=None)

    # delete_code: called by async_clear_usercode
    handlers["delete_code"] = AsyncMock(return_value=None)

    for service_name, handler in handlers.items():
        register_mock_service(hass, SCHLAGE_DOMAIN, service_name, handler)

    return handlers


@pytest.fixture
async def lcm_config_entry(
    hass: HomeAssistant,
    schlage_lock_entity: er.RegistryEntry,
    schlage_mock_services: dict[str, AsyncMock],
) -> MockConfigEntry:
    """
    Set up a full LCM config entry managing the Schlage lock.

    This goes through the real async_setup_entry path: LCM discovers the
    lock entity is from the schlage platform and instantiates SchlageLock.
    The Schlage config entry is not truly LOADED (no real integration), so
    we patch the connection check to always return True.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [schlage_lock_entity.entity_id],
            CONF_SLOTS: SCHLAGE_LCM_CONFIG_SLOTS,
        },
        unique_id="test_schlage_e2e",
    )
    lcm_entry.add_to_hass(hass)
    with patch.object(SchlageLock, "async_is_integration_connected", return_value=True):
        assert await hass.config_entries.async_setup(lcm_entry.entry_id)
        await hass.async_block_till_done()

        yield lcm_entry

        await hass.config_entries.async_unload(lcm_entry.entry_id)


def get_schlage_lock(
    hass: HomeAssistant,
    lcm_entry: MockConfigEntry,
    lock_entity: er.RegistryEntry,
) -> SchlageLock:
    """Extract the SchlageLock from a loaded LCM config entry."""
    lock = lcm_entry.runtime_data.locks.get(lock_entity.entity_id)
    assert lock is not None, f"Lock {lock_entity.entity_id} not found in runtime data"
    assert isinstance(lock, SchlageLock)
    return lock


@pytest.fixture
def e2e_schlage_lock(
    hass: HomeAssistant,
    lcm_config_entry: MockConfigEntry,
    schlage_lock_entity: er.RegistryEntry,
) -> SchlageLock:
    """Extract the SchlageLock from the LCM config entry."""
    return get_schlage_lock(hass, lcm_config_entry, schlage_lock_entity)
