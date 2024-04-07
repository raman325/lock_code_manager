"""Test base class."""

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.providers._base import BaseLock


async def test_base(hass: HomeAssistant):
    """Test base class."""
    lock = BaseLock(
        hass,
        dr.async_get(hass),
        er.async_get(hass),
        MockConfigEntry(),
        er.RegistryEntry("lock.test", "blah", "blah"),
    )
    assert await lock.async_setup() is None
    assert await lock.async_unload(False) is None
    assert lock.usercode_scan_interval == timedelta(minutes=1)
    with pytest.raises(NotImplementedError):
        lock.domain
    with pytest.raises(NotImplementedError):
        await lock.async_internal_is_connection_up()
    with pytest.raises(HomeAssistantError):
        await lock.async_internal_hard_refresh_codes()
    with pytest.raises(HomeAssistantError):
        await lock.async_internal_clear_usercode(1)
    with pytest.raises(HomeAssistantError):
        await lock.async_internal_set_usercode(1, 1)
    with pytest.raises(NotImplementedError):
        await lock.async_internal_get_usercodes()
