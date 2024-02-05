"""Fixtures for lock_code_manager tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from custom_components.lock_code_manager.providers._base import BaseLock
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

pytest_plugins = ["pytest_homeassistant_custom_component"]


@dataclass(repr=False)
class MockLock(BaseLock):
    """Mock class for lock instance."""

    is_connection_up = MagicMock()
    hard_refresh_codes = MagicMock()
    set_usercode = MagicMock()
    clear_usercode = MagicMock()
    get_usercodes = MagicMock()

    async_is_connection_up = AsyncMock()
    async_hard_refresh_codes = AsyncMock()
    async_set_usercode = AsyncMock()
    async_clear_usercode = AsyncMock()
    async_get_usercodes = AsyncMock()

    domain = PropertyMock(spec=str)
    device_entry = PropertyMock(spec=dr.DeviceEntry)
    usercode_scan_interval = PropertyMock(spec=timedelta)

    # @property
    # def domain(self) -> str:
    #     """Return integration domain."""
    #     raise NotImplementedError()

    # @property
    # def device_entry(self) -> dr.DeviceEntry | None:
    #     """Return device registry entry for the lock."""
    #     return None

    # @property
    # def usercode_scan_interval(self) -> timedelta:
    #     """Return scan interval for usercodes."""
    #     return timedelta(minutes=1)


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


async def setup_lock_code_manager(hass: HomeAssistant):
    """Set up the lock code manager."""
    lock_class = AsyncMock(spec=BaseLock)
    with patch.dict(
        "custom_components.lock_code_manager.providers.INTEGRATIONS",
        {"base": lock_class},
    ):
        yield
