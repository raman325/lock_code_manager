"""Codeless provider test fixtures."""

from __future__ import annotations

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.const import CONF_ENABLED, CONF_PIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import (
    CONF_CODELESS,
    CONF_LOCKS,
    CONF_MEMBERS,
    CONF_USERS,
    DOMAIN,
)
from custom_components.lock_code_manager.domain.slot_assignment import (
    CONF_SLOT_ASSIGNMENT,
)
from custom_components.lock_code_manager.providers.codeless import CodelessLock

from ...common import register_codeless_lock

USER_NAME = "Raman"
USER_PIN = "1234"


@pytest.fixture
def codeless_lock_entity(hass: HomeAssistant) -> er.RegistryEntry:
    """Register the real lock entity that no provider claims."""
    return register_codeless_lock(hass)


@pytest.fixture
async def lcm_config_entry(
    hass: HomeAssistant, codeless_lock_entity: er.RegistryEntry
) -> MockConfigEntry:
    """
    Set up an entry whose one member is declared codeless.

    Through the real setup path, so what picks the provider is the
    declaration in the entry -- keyed by registry id, exactly as the config
    flow writes it -- rather than anything the test hands the factory.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [codeless_lock_entity.entity_id],
            CONF_MEMBERS: {codeless_lock_entity.id: {CONF_CODELESS: True}},
            CONF_USERS: {USER_NAME: {CONF_PIN: USER_PIN, CONF_ENABLED: True}},
            CONF_SLOT_ASSIGNMENT: {USER_NAME.casefold(): 1},
        },
        unique_id="test_codeless_e2e",
    )
    lcm_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(lcm_entry.entry_id)
    await hass.async_block_till_done()

    yield lcm_entry

    await hass.config_entries.async_unload(lcm_entry.entry_id)


@pytest.fixture
def codeless_lock(
    hass: HomeAssistant,
    lcm_config_entry: MockConfigEntry,
    codeless_lock_entity: er.RegistryEntry,
) -> CodelessLock:
    """Return the provider the loaded entry built for its declared member."""
    lock = lcm_config_entry.runtime_data.locks.get(codeless_lock_entity.entity_id)
    assert isinstance(lock, CodelessLock)
    return lock
