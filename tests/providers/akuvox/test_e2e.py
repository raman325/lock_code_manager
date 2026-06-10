"""Full lifecycle E2E tests for Akuvox lock provider."""

from __future__ import annotations

from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.domain.credentials import (
    CredentialRef,
    CredentialType,
    credential_from_slot,
)
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers.akuvox import (
    AKUVOX_DOMAIN,
    AkuvoxLock,
)
from tests.providers.helpers import register_mock_service

from .conftest import make_user


class TestFullSetupLifecycle:
    """Verify LCM correctly discovers and sets up the Akuvox provider."""

    async def test_provider_discovered_as_akuvox(
        self,
        hass: HomeAssistant,
        e2e_lcm_config_entry: MockConfigEntry,
        akuvox_lock_entity: er.RegistryEntry,
    ) -> None:
        """Verify LCM discovers the Akuvox lock and creates an AkuvoxLock."""
        lock = e2e_lcm_config_entry.runtime_data.locks.get(akuvox_lock_entity.entity_id)
        assert lock is not None
        assert isinstance(lock, AkuvoxLock)

    async def test_coordinator_created(
        self,
        hass: HomeAssistant,
        e2e_akuvox_lock: AkuvoxLock,
    ) -> None:
        """The coordinator is created and attached to the provider."""
        assert e2e_akuvox_lock.coordinator is not None


class TestSetAndClearCredentials:
    """Verify set/clear operations call the correct Akuvox services."""

    async def test_set_credential_new(
        self,
        hass: HomeAssistant,
        e2e_akuvox_lock: AkuvoxLock,
        akuvox_mock_services: dict[str, AsyncMock],
    ) -> None:
        """Set a credential on an empty slot and verify add_user service was called."""
        akuvox_mock_services["add_user"].reset_mock()
        result = await e2e_akuvox_lock.async_set_credential(
            1,
            credential_from_slot(1, SlotCredential.known("9999")),
            (credential_from_slot(1, SlotCredential.known("9999"))).readable_pin or "",
            name="E2E Guest",
            source="direct",
        )

        assert result is True
        assert akuvox_mock_services["add_user"].call_count >= 1

    async def test_set_credential_existing(
        self,
        hass: HomeAssistant,
        e2e_akuvox_lock: AkuvoxLock,
        akuvox_lock_entity: er.RegistryEntry,
        akuvox_mock_services: dict[str, AsyncMock],
    ) -> None:
        """Set a credential on an occupied slot and verify modify_user service was called."""
        entity_id = akuvox_lock_entity.entity_id

        # Override list_users to report an existing tagged user
        akuvox_mock_services["list_users"] = AsyncMock(
            return_value={
                entity_id: {
                    "users": [make_user("100", "[LCM:1] Existing", "1234")],
                },
            }
        )
        register_mock_service(
            hass, AKUVOX_DOMAIN, "list_users", akuvox_mock_services["list_users"]
        )

        akuvox_mock_services["modify_user"].reset_mock()
        result = await e2e_akuvox_lock.async_set_credential(
            1,
            credential_from_slot(1, SlotCredential.known("5678")),
            (credential_from_slot(1, SlotCredential.known("5678"))).readable_pin or "",
            name="Updated",
            source="direct",
        )

        assert result is True
        assert akuvox_mock_services["modify_user"].call_count >= 1

    async def test_delete_credential(
        self,
        hass: HomeAssistant,
        e2e_akuvox_lock: AkuvoxLock,
        akuvox_lock_entity: er.RegistryEntry,
        akuvox_mock_services: dict[str, AsyncMock],
    ) -> None:
        """Delete a credential and verify delete_user service was called."""
        entity_id = akuvox_lock_entity.entity_id

        # Override list_users to report a tagged user to delete
        akuvox_mock_services["list_users"] = AsyncMock(
            return_value={
                entity_id: {
                    "users": [make_user("100", "[LCM:1] Guest", "1234")],
                },
            }
        )
        register_mock_service(
            hass, AKUVOX_DOMAIN, "list_users", akuvox_mock_services["list_users"]
        )

        akuvox_mock_services["delete_user"].reset_mock()
        result = await e2e_akuvox_lock.async_delete_credential(
            CredentialRef(user_id=1, type=CredentialType.PIN, slot=1)
        )

        assert result is True
        assert akuvox_mock_services["delete_user"].call_count >= 1

    async def test_base_orchestration_set_credential(
        self,
        hass: HomeAssistant,
        e2e_akuvox_lock: AkuvoxLock,
        akuvox_mock_services: dict[str, AsyncMock],
    ) -> None:
        """Set via async_internal_set_usercode routes through base → async_set_credential."""
        akuvox_mock_services["add_user"].reset_mock()
        await e2e_akuvox_lock.async_internal_set_usercode(1, "9999", "E2E Guest")

        assert akuvox_mock_services["add_user"].call_count >= 1

    async def test_base_orchestration_clear_credential(
        self,
        hass: HomeAssistant,
        e2e_akuvox_lock: AkuvoxLock,
        akuvox_lock_entity: er.RegistryEntry,
        akuvox_mock_services: dict[str, AsyncMock],
    ) -> None:
        """Clear via async_internal_clear_usercode routes through base → async_delete_credential."""
        entity_id = akuvox_lock_entity.entity_id

        # Override list_users to report a tagged user to delete
        akuvox_mock_services["list_users"] = AsyncMock(
            return_value={
                entity_id: {
                    "users": [make_user("100", "[LCM:1] Guest", "1234")],
                },
            }
        )
        register_mock_service(
            hass, AKUVOX_DOMAIN, "list_users", akuvox_mock_services["list_users"]
        )

        akuvox_mock_services["delete_user"].reset_mock()
        await e2e_akuvox_lock.async_internal_clear_usercode(1)

        assert akuvox_mock_services["delete_user"].call_count >= 1


class TestGetUsers:
    """Verify reading users (usercodes) from the Akuvox lock."""

    async def test_get_users_returns_codes(
        self,
        hass: HomeAssistant,
        e2e_akuvox_lock: AkuvoxLock,
        akuvox_lock_entity: er.RegistryEntry,
        akuvox_mock_services: dict[str, AsyncMock],
    ) -> None:
        """
        async_get_users returns mapped slot data from Akuvox.

        After initial setup with empty users, both managed slots should
        be EMPTY. When the mock reports a tagged user with a Personal
        Identification Number on slot 1, that slot should report the PIN value.
        """
        entity_id = akuvox_lock_entity.entity_id

        # Override list_users to report a tagged user on slot 1
        akuvox_mock_services["list_users"] = AsyncMock(
            return_value={
                entity_id: {
                    "users": [make_user("100", "[LCM:1] Guest", "4321")],
                },
            }
        )
        register_mock_service(
            hass, AKUVOX_DOMAIN, "list_users", akuvox_mock_services["list_users"]
        )

        users = await e2e_akuvox_lock.async_get_users()
        user_map = {u.user_id: u for u in users}

        assert user_map[1].pin_credentials[0].state == SlotCredential.known("4321")
        assert user_map[2].pin_credentials[0].state is SlotCredential.empty()

    async def test_get_usercodes_projection(
        self,
        hass: HomeAssistant,
        e2e_akuvox_lock: AkuvoxLock,
        akuvox_lock_entity: er.RegistryEntry,
        akuvox_mock_services: dict[str, AsyncMock],
    ) -> None:
        """The base async_get_usercodes projection produces the same slot->SlotCredential dict."""
        entity_id = akuvox_lock_entity.entity_id

        akuvox_mock_services["list_users"] = AsyncMock(
            return_value={
                entity_id: {
                    "users": [make_user("100", "[LCM:1] Guest", "4321")],
                },
            }
        )
        register_mock_service(
            hass, AKUVOX_DOMAIN, "list_users", akuvox_mock_services["list_users"]
        )

        codes = await e2e_akuvox_lock.async_get_usercodes()

        assert codes[1] == SlotCredential.known("4321")
        assert codes[2] is SlotCredential.empty()

    async def test_get_usercodes_empty(
        self,
        hass: HomeAssistant,
        e2e_akuvox_lock: AkuvoxLock,
    ) -> None:
        """All managed slots are EMPTY when the device has no users."""
        codes = await e2e_akuvox_lock.async_get_usercodes()

        assert codes[1] is SlotCredential.empty()
        assert codes[2] is SlotCredential.empty()
