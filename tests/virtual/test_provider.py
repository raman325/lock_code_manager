"""Test the Virtual lock platform."""

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.providers.virtual import VirtualLock


async def test_door_lock(hass: HomeAssistant):
    """Test a lock entity."""
    lock = VirtualLock(
        hass,
        dr.async_get(hass),
        er.async_get(hass),
        MockConfigEntry(),
        er.RegistryEntry("lock.test", "blah", "blah"),
    )
    assert await lock.async_setup() is None
    assert lock.usercode_scan_interval == timedelta(minutes=1)
    assert lock.domain == "virtual"
    assert await lock.async_internal_is_connection_up()
    assert lock._data == {}
    await lock.async_internal_hard_refresh_codes()
    assert lock._data == {}
    # we should not be able to clear a usercode that does not exist
    with pytest.raises(HomeAssistantError):
        await lock.async_internal_clear_usercode(1)

    # we should be able to set a usercode and see it in the data
    await lock.async_internal_set_usercode(1, 1, "test")
    assert lock._data["1"] == {"code": 1, "name": "test"}
    await lock.async_internal_get_usercodes()
    assert lock._data["1"] == {"code": 1, "name": "test"}

    # if we unload without removing permanently, the data should be saved
    assert await lock.async_unload(False) is None
    assert await lock.async_setup() is None
    assert lock._data["1"] == {"code": 1, "name": "test"}

    # we can clear a valid usercode
    await lock.async_internal_set_usercode(2, 2, "test2")
    assert lock._data["2"] == {"code": 2, "name": "test2"}
    await lock.async_internal_clear_usercode(2)
    assert "2" not in lock._data

    # if we unload with removing permanently, the data should be removed
    assert await lock.async_unload(True) is None
    assert await lock.async_setup() is None
    assert not lock._data
