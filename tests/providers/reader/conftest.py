"""Reader provider test fixtures."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import (
    CONF_ENABLED,
    CONF_LOCKS,
    CONF_NAME,
    CONF_PIN,
    CONF_SLOTS,
    DOMAIN,
)

READER_ENTITY_ID = "sensor.keypad_code"

# LCM config: one reader anchor, one enabled slot and one disabled slot
READER_LCM_CONFIG = {
    CONF_LOCKS: [READER_ENTITY_ID],
    CONF_SLOTS: {
        1: {CONF_NAME: "alice", CONF_PIN: "1234", CONF_ENABLED: True},
        2: {CONF_NAME: "bob", CONF_PIN: "5678", CONF_ENABLED: False},
    },
}


@pytest.fixture
async def esphome_config_entry(
    hass: HomeAssistant,
) -> AsyncGenerator[MockConfigEntry]:
    """Create the anchor entity's provider config entry."""
    esphome_entry = MockConfigEntry(domain="esphome")
    esphome_entry.add_to_hass(hass)

    yield esphome_entry

    # A test that drove this mock entry to LOADED must not leave it there:
    # hass teardown would then genuinely try to unload esphome, which was
    # never set up, and fail on its imports.
    if esphome_entry.state is not ConfigEntryState.NOT_LOADED:
        esphome_entry.mock_state(hass, ConfigEntryState.NOT_LOADED)


@pytest.fixture
async def reader_entity(
    hass: HomeAssistant, esphome_config_entry: MockConfigEntry
) -> er.RegistryEntry:
    """Register a sensor-domain anchor entity under the esphome platform."""
    entity = er.async_get(hass).async_get_or_create(
        "sensor",
        "esphome",
        "keypad_code",
        suggested_object_id="keypad_code",
        config_entry=esphome_config_entry,
    )
    assert entity.entity_id == READER_ENTITY_ID
    # A cleared keypad idles on an empty state; set it before LCM subscribes
    # so no test starts with a phantom submission.
    hass.states.async_set(READER_ENTITY_ID, "")
    return entity


@pytest.fixture
async def lcm_config_entry(
    hass: HomeAssistant,
    reader_entity: er.RegistryEntry,
) -> AsyncGenerator[MockConfigEntry]:
    """
    Set up a full LCM config entry managing the reader.

    This goes through the real async_setup_entry path: LCM sees the
    sensor-domain anchor and instantiates ReaderLock.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN, data=READER_LCM_CONFIG, unique_id="test_reader"
    )
    lcm_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(lcm_entry.entry_id)
    await hass.async_block_till_done()

    yield lcm_entry

    # A test exercising unload leaves the entry already torn down.
    if lcm_entry.state is ConfigEntryState.LOADED:
        await hass.config_entries.async_unload(lcm_entry.entry_id)
