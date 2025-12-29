"""Fixtures for lock_code_manager tests."""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

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

import custom_components.lock_code_manager as lcm_init
from custom_components.lock_code_manager import helpers
from custom_components.lock_code_manager.const import CONF_LOCKS, DOMAIN
from custom_components.lock_code_manager.providers import _base as base_lock

from .common import BASE_CONFIG, MockCalendarEntity, MockLCMLock, MockLockEntity

pytest_plugins = ["pytest_homeassistant_custom_component"]

TEST_DOMAIN = "test"


@pytest.fixture(autouse=True)
def aiohttp_client(aiohttp_client, socket_enabled):
    """Return aiohttp_client and allow opening sockets."""
    return aiohttp_client


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Enable custom integrations."""
    yield


@pytest.fixture(autouse=True)
def auto_setup_mock_lock(monkeypatch: pytest.MonkeyPatch):
    """Automatically set up MockLCMLock for all tests."""
    monkeypatch.setattr(
        "custom_components.lock_code_manager.helpers.INTEGRATIONS_CLASS_MAP",
        {"test": MockLCMLock},
    )
    yield


@pytest.fixture(autouse=True)
def auto_disable_lock_rate_limit(monkeypatch: pytest.MonkeyPatch):
    """Disable per-lock rate limiting during test setup."""
    original_create = helpers.async_create_lock_instance

    def _create_lock_instance(*args, **kwargs):
        lock = original_create(*args, **kwargs)
        lock._min_operation_delay = 0.0
        return lock

    monkeypatch.setattr(helpers, "async_create_lock_instance", _create_lock_instance)
    monkeypatch.setattr(lcm_init, "async_create_lock_instance", _create_lock_instance)
    yield


@pytest.fixture(autouse=True)
def disable_rate_limiting(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
):
    """Disable BaseLock rate limiting for most tests to speed them up."""
    if request.fspath and "tests/_base/test_provider.py" in str(request.fspath):
        yield
        return
    monkeypatch.setattr(base_lock, "MIN_OPERATION_DELAY", 0.0)
    yield


class MockFlow(ConfigFlow):
    """Test flow."""


@pytest.fixture(name="mock_config_flow")
def config_flow_fixture(hass: HomeAssistant) -> Generator[None, None, None]:
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
    entry_data = hass.data.get(DOMAIN, {}).get(config_entry.entry_id)
    if entry_data:
        for lock in entry_data.get(CONF_LOCKS, {}).values():
            lock._min_operation_delay = 0.0

    yield config_entry

    await hass.config_entries.async_unload(config_entry.entry_id)
