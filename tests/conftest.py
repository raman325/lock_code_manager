"""Fixtures for lock_code_manager tests."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    MockPlatform,
    mock_config_flow,
    mock_integration,
    mock_platform,
)

from homeassistant.components.calendar import DOMAIN as CALENDAR_DOMAIN
from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN
from homeassistant.components.lovelace import DOMAIN as LL_DOMAIN
from homeassistant.config_entries import ConfigEntry, ConfigFlow
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.setup import async_setup_component

from custom_components.lock_code_manager.const import DOMAIN
from custom_components.lock_code_manager.providers import INTEGRATIONS_CLASS_MAP
from custom_components.lock_code_manager.providers._base import BaseLock

from .common import BASE_CONFIG, MockCalendarEntity, MockLCMLock, MockLockEntity

pytest_plugins = ["pytest_homeassistant_custom_component"]

TEST_DOMAIN = "test"


@pytest.fixture
def aiohttp_client(aiohttp_client, socket_enabled):
    """Return aiohttp_client and allow opening sockets."""
    return aiohttp_client


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations."""
    yield


@pytest.fixture(autouse=True)
def auto_setup_mock_lock():
    """Automatically set up MockLCMLock for all tests."""
    with patch.dict(
        "custom_components.lock_code_manager.helpers.INTEGRATIONS_CLASS_MAP",
        {"test": MockLCMLock, **INTEGRATIONS_CLASS_MAP},
    ):
        yield


@pytest.fixture(autouse=True)
def disable_rate_limiting(request: pytest.FixtureRequest):
    """
    Disable BaseLock rate limiting for most tests to speed them up.

    We patch __post_init__ to set _min_operation_delay=0.0 on new instances.
    """
    original_post_init = getattr(BaseLock, "__post_init__", None)

    def patched_post_init(self):
        if original_post_init:
            original_post_init(self)
        self._min_operation_delay = 0.0

    with patch(
        "custom_components.lock_code_manager.providers._base.BaseLock.__post_init__",
        patched_post_init,
    ):
        yield


class MockFlow(ConfigFlow):
    """Test flow."""


@pytest.fixture(name="mock_config_flow")
def config_flow_fixture(hass: HomeAssistant) -> Generator[None]:
    """Mock config flow."""
    mock_platform(hass, f"{TEST_DOMAIN}.config_flow")

    with mock_config_flow(TEST_DOMAIN, MockFlow):
        yield


@pytest.fixture(name="setup_lovelace_ui")
async def setup_lovelace_ui_fixture(hass: HomeAssistant, config: dict[str, Any]):
    """Set up Lovelace in UI mode."""
    assert await async_setup_component(hass, LL_DOMAIN, {"lovelace": config})
    yield


@pytest.fixture(name="mock_lock_config_entry")
async def mock_lock_config_entry_fixture(hass: HomeAssistant, mock_config_flow):
    """Set up lock entities using an entity platform."""

    async def async_setup_entry_init(
        hass: HomeAssistant, config_entry: ConfigEntry
    ) -> bool:
        """Set up test config entry."""
        await hass.config_entries.async_forward_entry_setups(
            config_entry, [Platform.CALENDAR, Platform.LOCK]
        )
        return True

    async def async_unload_entry_init(
        hass: HomeAssistant, config_entry: ConfigEntry
    ) -> bool:
        """Unload test config entry."""
        for platform in (Platform.CALENDAR, Platform.LOCK):
            await hass.config_entries.async_forward_entry_unload(config_entry, platform)
        return True

    mock_platform(hass, f"{TEST_DOMAIN}.config_flow")
    mock_integration(
        hass,
        MockModule(
            TEST_DOMAIN,
            async_setup_entry=async_setup_entry_init,
            async_unload_entry=async_unload_entry_init,
        ),
    )

    config_entry = MockConfigEntry(domain=TEST_DOMAIN)

    async def async_setup_entry_lock_platform(
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback,
    ) -> None:
        """Set up test lock platform via config entry."""
        async_add_entities([MockLockEntity("test_1"), MockLockEntity("test_2")])

    mock_platform(
        hass,
        f"{TEST_DOMAIN}.{LOCK_DOMAIN}",
        MockPlatform(async_setup_entry=async_setup_entry_lock_platform),
    )

    calendars = hass.data["lock_code_manager_calendars"] = [
        MockCalendarEntity("test_1"),
        MockCalendarEntity("test_2"),
    ]

    async def async_setup_entry_calendar_platform(
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback,
    ) -> None:
        """Set up test calendar platform via config entry."""
        async_add_entities(calendars)

    mock_platform(
        hass,
        f"{TEST_DOMAIN}.{CALENDAR_DOMAIN}",
        MockPlatform(async_setup_entry=async_setup_entry_calendar_platform),
    )

    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    yield config_entry

    await hass.config_entries.async_unload(config_entry.entry_id)


@pytest.fixture(name="lock_code_manager_config_entry")
async def lock_code_manager_config_entry_fixture(hass: HomeAssistant):
    """Set up the config entry for lock code manager."""
    config_entry = MockConfigEntry(
        domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title"
    )
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    yield config_entry

    await hass.config_entries.async_unload(config_entry.entry_id)


def get_in_sync_entity_obj(hass: HomeAssistant, entity_id: str):
    """Get the in-sync entity object for a given entity ID.

    Returns the entity object from the entity component registry.
    """
    entity_component = hass.data["entity_components"]["binary_sensor"]
    entity_obj = entity_component.get_entity(entity_id)
    assert entity_obj is not None
    return entity_obj


async def async_trigger_sync_tick(
    hass: HomeAssistant, entity_id: str, set_dirty: bool = True
) -> None:
    """Manually trigger a sync tick for an in-sync entity.

    Encapsulates the pattern of marking an entity dirty and triggering an
    immediate tick, useful for testing tick-based sync behavior without
    waiting for the natural 5-second tick interval.
    """
    entity_obj = get_in_sync_entity_obj(hass, entity_id)
    if set_dirty:
        entity_obj._sync_manager._dirty = True
    await entity_obj._sync_manager._async_tick()
    await hass.async_block_till_done()


async def async_initial_tick(hass: HomeAssistant, entity_id: str) -> None:
    """Trigger initial tick for entity setup.

    During entity setup, the initial tick in async_start may fail if dependent
    entities (active, code sensor) are not yet registered. This helper triggers
    a tick to complete initial state loading, but only if the entity hasn't
    been initialized yet (_in_sync is None).
    """
    entity_obj = get_in_sync_entity_obj(hass, entity_id)
    if entity_obj._sync_manager._in_sync is None:
        entity_obj._sync_manager._dirty = True
        await entity_obj._sync_manager._async_tick()
        await hass.async_block_till_done()


async def async_trigger_sync_tick_for_manager(
    hass: HomeAssistant, sync_manager, set_dirty: bool = True
) -> None:
    """Manually trigger a sync tick for a sync manager object.

    Useful when you already have the entity object or need to trigger
    ticks on multiple managers in a loop.
    """
    if set_dirty:
        sync_manager._dirty = True
    await sync_manager._async_tick()
    await hass.async_block_till_done()
