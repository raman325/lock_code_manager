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
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry()
    config_entry.add_to_hass(hass)

    # Create a proper registry entry
    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock",
        config_entry=config_entry,
    )

    lock = BaseLock(
        hass,
        dr.async_get(hass),
        entity_reg,
        config_entry,
        lock_entity,
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
