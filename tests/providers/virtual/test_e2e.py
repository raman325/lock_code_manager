"""Full lifecycle E2E tests for Virtual lock provider."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.models import SlotCode
from custom_components.lock_code_manager.providers.virtual import VirtualLock

from .conftest import VIRTUAL_LOCK_ENTITY_ID


class TestFullSetupLifecycle:
    """Verify LCM correctly discovers and sets up the Virtual provider."""

    async def test_provider_discovered_as_virtual(
        self,
        hass: HomeAssistant,
        lcm_config_entry,
    ) -> None:
        """Verify LCM discovers the virtual lock and creates a VirtualLock."""
        lock = lcm_config_entry.runtime_data.locks.get(VIRTUAL_LOCK_ENTITY_ID)
        assert lock is not None
        assert isinstance(lock, VirtualLock)

    async def test_always_connected(
        self,
        hass: HomeAssistant,
        e2e_virtual_lock: VirtualLock,
    ) -> None:
        """Virtual locks are always connected."""
        assert await e2e_virtual_lock.async_is_integration_connected()


class TestSetAndGetUsercodes:
    """Verify set/get/clear operations through the E2E-initialized provider."""

    async def test_set_and_get_usercode(
        self,
        hass: HomeAssistant,
        e2e_virtual_lock: VirtualLock,
    ) -> None:
        """Set a code, then get_usercodes and verify it is present."""
        await e2e_virtual_lock.async_set_usercode(1, "1111", "test_user")

        codes = await e2e_virtual_lock.async_get_usercodes()
        assert codes[1] == "1111"

    async def test_clear_usercode(
        self,
        hass: HomeAssistant,
        e2e_virtual_lock: VirtualLock,
    ) -> None:
        """Set a code, clear it, and verify it is gone."""
        await e2e_virtual_lock.async_set_usercode(1, "1111", "test_user")
        await e2e_virtual_lock.async_clear_usercode(1)

        codes = await e2e_virtual_lock.async_get_usercodes()
        assert codes[1] is SlotCode.EMPTY

    async def test_codes_persist_across_unload_and_reload(
        self,
        hass: HomeAssistant,
        e2e_virtual_lock: VirtualLock,
    ) -> None:
        """Set codes, unload (persists to store), reload, verify codes survive."""
        await e2e_virtual_lock.async_set_usercode(1, "1111", "user1")
        await e2e_virtual_lock.async_set_usercode(2, "2222", "user2")

        # Unload without removing permanently saves data to the store
        config_entry = e2e_virtual_lock._lcm_config_entry
        await e2e_virtual_lock.async_unload(False)

        # Re-setup reloads from the store via hard_refresh_codes
        await e2e_virtual_lock.async_setup_internal(config_entry)
        codes = await e2e_virtual_lock.async_get_usercodes()

        assert codes[1] == "1111"
        assert codes[2] == "2222"

    async def test_hard_refresh_reloads_from_store(
        self,
        hass: HomeAssistant,
        e2e_virtual_lock: VirtualLock,
    ) -> None:
        """Hard refresh reloads from the store, so unsaved codes are lost."""
        await e2e_virtual_lock.async_set_usercode(1, "1111", "user1")

        # Hard refresh reloads from the store (which has not been saved to)
        codes = await e2e_virtual_lock.async_hard_refresh_codes()

        assert codes[1] is SlotCode.EMPTY
