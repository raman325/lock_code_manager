"""Full lifecycle E2E tests for Virtual lock provider."""

from __future__ import annotations

from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.domain.credentials import (
    CredentialRef,
    CredentialType,
    credential_from_slot,
)
from custom_components.lock_code_manager.domain.models import SlotCredential
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
        """Set a credential, then get_usercodes via base projection and verify it is present."""
        await e2e_virtual_lock.async_set_credential(
            1,
            credential_from_slot(1, SlotCredential.known("1111")),
            "1111",
            name="test_user",
            source="direct",
        )

        codes = await e2e_virtual_lock.async_get_usercodes()
        assert codes[1] == SlotCredential.known("1111")

    async def test_clear_usercode(
        self,
        hass: HomeAssistant,
        e2e_virtual_lock: VirtualLock,
    ) -> None:
        """Set a credential, clear it, and verify it is gone."""
        await e2e_virtual_lock.async_set_credential(
            1,
            credential_from_slot(1, SlotCredential.known("1111")),
            "1111",
            name="test_user",
            source="direct",
        )
        await e2e_virtual_lock.async_delete_credential(
            CredentialRef(user_id=1, type=CredentialType.PIN, slot=1)
        )

        codes = await e2e_virtual_lock.async_get_usercodes()
        assert codes[1] is SlotCredential.empty()

    async def test_base_orchestration_set_and_get(
        self,
        hass: HomeAssistant,
        e2e_virtual_lock: VirtualLock,
    ) -> None:
        """Set via async_internal_set_usercode (base orchestration → primitive) and verify via projection."""
        await e2e_virtual_lock.async_internal_set_usercode(1, "2222", "orchestrated")

        codes = await e2e_virtual_lock.async_get_usercodes()
        assert codes[1] == SlotCredential.known("2222")

    async def test_base_orchestration_clear(
        self,
        hass: HomeAssistant,
        e2e_virtual_lock: VirtualLock,
    ) -> None:
        """Set then clear via base orchestration; verify projection shows empty."""
        await e2e_virtual_lock.async_internal_set_usercode(1, "3333", "user")
        await e2e_virtual_lock.async_internal_clear_usercode(1)

        codes = await e2e_virtual_lock.async_get_usercodes()
        assert codes[1] is SlotCredential.empty()

    async def test_codes_persist_across_unload_and_reload(
        self,
        hass: HomeAssistant,
        e2e_virtual_lock: VirtualLock,
    ) -> None:
        """Set codes, unload (persists to store), reload, verify codes survive."""
        await e2e_virtual_lock.async_set_credential(
            1,
            credential_from_slot(1, SlotCredential.known("1111")),
            "1111",
            name="user1",
            source="direct",
        )
        await e2e_virtual_lock.async_set_credential(
            2,
            credential_from_slot(2, SlotCredential.known("2222")),
            "2222",
            name="user2",
            source="direct",
        )

        # Unload without removing permanently saves data to the store
        config_entry = e2e_virtual_lock._lcm_config_entry
        await e2e_virtual_lock.async_unload(False)

        # Re-setup reloads from the store via hard_refresh_codes
        await e2e_virtual_lock.async_setup_internal(config_entry)
        codes = await e2e_virtual_lock.async_get_usercodes()

        assert codes[1] == SlotCredential.known("1111")
        assert codes[2] == SlotCredential.known("2222")

    async def test_hard_refresh_reloads_from_store(
        self,
        hass: HomeAssistant,
        e2e_virtual_lock: VirtualLock,
    ) -> None:
        """Hard refresh reloads from the store, so unsaved codes are lost."""
        await e2e_virtual_lock.async_set_credential(
            1,
            credential_from_slot(1, SlotCredential.known("1111")),
            "1111",
            name="user1",
            source="direct",
        )

        # Hard refresh reloads from the store (which has not been saved to)
        codes = await e2e_virtual_lock.async_hard_refresh_codes()

        assert codes[1] is SlotCredential.empty()

    async def test_get_users_returns_correct_structure(
        self,
        hass: HomeAssistant,
        e2e_virtual_lock: VirtualLock,
    ) -> None:
        """async_get_users returns User objects wrapping the slot credentials."""
        await e2e_virtual_lock.async_set_credential(
            1,
            credential_from_slot(1, SlotCredential.known("5555")),
            "5555",
            name="slot_user",
            source="direct",
        )

        users = await e2e_virtual_lock.async_get_users()
        user_map = {u.user_id: u for u in users}

        assert 1 in user_map
        assert user_map[1].active is True
        assert user_map[1].pin_credentials[0].state == SlotCredential.known("5555")
        assert 2 in user_map
        assert user_map[2].active is False
