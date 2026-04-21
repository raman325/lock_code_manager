"""Akuvox provider test fixtures."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
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
from custom_components.lock_code_manager.providers.akuvox import (
    AKUVOX_DOMAIN,
    AkuvoxLock,
)
from tests.providers.helpers import register_mock_service

LOCK_ENTITY_ID = "lock.local_akuvox_test_relay_a"


@pytest.fixture
async def akuvox_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create a local_akuvox config entry."""
    entry = MockConfigEntry(domain=AKUVOX_DOMAIN)
    entry.add_to_hass(hass)
    entry._async_set_state(hass, entry.state, None)
    return entry


@pytest.fixture
async def akuvox_lock(
    hass: HomeAssistant, akuvox_config_entry: MockConfigEntry
) -> AkuvoxLock:
    """Create an AkuvoxLock instance with a registered lock entity."""
    entity_reg = er.async_get(hass)
    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "local_akuvox",
        "test_relay_a",
        config_entry=akuvox_config_entry,
    )
    return AkuvoxLock(
        hass,
        dr.async_get(hass),
        entity_reg,
        akuvox_config_entry,
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
    entry = MockConfigEntry(domain=DOMAIN, data=config, unique_id="test_akuvox_lcm")
    entry.add_to_hass(hass)
    return entry


# --- Alias fixtures for shared test mixins ---


@pytest.fixture
def provider_lock(akuvox_lock: AkuvoxLock) -> AkuvoxLock:
    """Alias akuvox_lock for shared test mixins."""
    return akuvox_lock


@pytest.fixture
def provider_config_entry(akuvox_config_entry: MockConfigEntry) -> MockConfigEntry:
    """Alias akuvox_config_entry for shared test mixins."""
    return akuvox_config_entry


@pytest.fixture
def provider_domain() -> str:
    """Return the provider integration domain."""
    return AKUVOX_DOMAIN


@pytest.fixture
def provider_lock_class() -> type[AkuvoxLock]:
    """Return the provider lock class."""
    return AkuvoxLock


def make_user(
    device_id: str,
    name: str,
    private_pin: str = "",
    source_type: str | None = "1",
    user_type: str = "0",
) -> dict[str, Any]:
    """Create a user dict matching list_users response format."""
    return {
        "id": device_id,
        "name": name,
        "private_pin": private_pin,
        "source_type": source_type,
        "user_type": user_type,
    }


# ---------------------------------------------------------------------------
# E2E fixtures -- set up a full LCM config entry on top of a mock Akuvox
# integration so the provider is discovered and initialised through the
# real async_setup_entry path.
# ---------------------------------------------------------------------------

AKUVOX_LCM_CONFIG_SLOTS = {
    1: {CONF_NAME: "slot1", CONF_PIN: "1234", CONF_ENABLED: True},
    2: {CONF_NAME: "slot2", CONF_PIN: "5678", CONF_ENABLED: True},
}


@pytest.fixture
async def akuvox_lock_entity(hass: HomeAssistant) -> er.RegistryEntry:
    """Create an Akuvox config entry (LOADED), device, and lock entity.

    Sets the config entry to LOADED state so the provider's
    async_is_integration_connected check passes.
    """
    entry = MockConfigEntry(domain=AKUVOX_DOMAIN)
    entry.add_to_hass(hass)
    entry._async_set_state(hass, ConfigEntryState.LOADED, None)

    dev_reg = dr.async_get(hass)
    ent_reg = er.async_get(hass)

    device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        connections=set(),
        identifiers={(AKUVOX_DOMAIN, "test_relay_a")},
        name="Test Relay A",
    )

    lock_entity = ent_reg.async_get_or_create(
        "lock",
        "local_akuvox",
        "test_relay_a",
        config_entry=entry,
        device_id=device.id,
    )

    hass.states.async_set(lock_entity.entity_id, "locked")

    return lock_entity


@pytest.fixture
def akuvox_mock_services(
    hass: HomeAssistant,
    akuvox_lock_entity: er.RegistryEntry,
) -> dict[str, AsyncMock]:
    """Register mock Akuvox services needed for the full LCM setup path.

    Returns a dict of service name to handler so E2E tests can inspect
    call counts or swap side effects.
    """
    entity_id = akuvox_lock_entity.entity_id
    handlers: dict[str, AsyncMock] = {}

    # list_users: called by async_get_usercodes (coordinator refresh)
    # and by async_setup -> _async_tag_unmanaged_users
    handlers["list_users"] = AsyncMock(
        return_value={entity_id: {"users": []}},
    )

    # add_user: called by async_set_usercode for new users
    handlers["add_user"] = AsyncMock(return_value=None)

    # modify_user: called by async_set_usercode for existing users,
    # and by _async_tag_unmanaged_users
    handlers["modify_user"] = AsyncMock(return_value=None)

    # delete_user: called by async_clear_usercode
    handlers["delete_user"] = AsyncMock(return_value=None)

    for service_name, handler in handlers.items():
        register_mock_service(hass, AKUVOX_DOMAIN, service_name, handler)

    return handlers


@pytest.fixture
async def e2e_lcm_config_entry(
    hass: HomeAssistant,
    akuvox_lock_entity: er.RegistryEntry,
    akuvox_mock_services: dict[str, AsyncMock],
) -> AsyncGenerator[MockConfigEntry]:
    """Set up a full LCM config entry managing the Akuvox lock.

    This goes through the real async_setup_entry path: LCM discovers the
    lock entity is from the local_akuvox platform and instantiates AkuvoxLock.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [akuvox_lock_entity.entity_id],
            CONF_SLOTS: AKUVOX_LCM_CONFIG_SLOTS,
        },
        unique_id="test_akuvox_e2e",
    )
    lcm_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(lcm_entry.entry_id)
    await hass.async_block_till_done()

    yield lcm_entry

    await hass.config_entries.async_unload(lcm_entry.entry_id)


def get_akuvox_lock(
    hass: HomeAssistant,
    lcm_entry: MockConfigEntry,
    lock_entity: er.RegistryEntry,
) -> AkuvoxLock:
    """Extract the AkuvoxLock from a loaded LCM config entry."""
    lock = lcm_entry.runtime_data.locks.get(lock_entity.entity_id)
    assert lock is not None, f"Lock {lock_entity.entity_id} not found in runtime data"
    assert isinstance(lock, AkuvoxLock)
    return lock


@pytest.fixture
def e2e_akuvox_lock(
    hass: HomeAssistant,
    e2e_lcm_config_entry: MockConfigEntry,
    akuvox_lock_entity: er.RegistryEntry,
) -> AkuvoxLock:
    """Extract the AkuvoxLock from the LCM config entry."""
    return get_akuvox_lock(hass, e2e_lcm_config_entry, akuvox_lock_entity)
