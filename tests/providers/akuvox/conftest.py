"""Akuvox provider test fixtures."""

from __future__ import annotations

from typing import Any

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
from custom_components.lock_code_manager.providers.akuvox import (
    AKUVOX_DOMAIN,
    AkuvoxLock,
)

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
