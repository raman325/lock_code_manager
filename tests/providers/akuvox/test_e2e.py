"""Full lifecycle E2E tests for Akuvox lock provider."""

from __future__ import annotations

from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.models import SlotCode
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


class TestSetAndClearUsercodes:
    """Verify set/clear operations call the correct Akuvox services."""

    async def test_set_usercode_new(
        self,
        hass: HomeAssistant,
        e2e_akuvox_lock: AkuvoxLock,
        akuvox_mock_services: dict[str, AsyncMock],
    ) -> None:
        """Set a code on an empty slot and verify add_user service was called."""
        akuvox_mock_services["add_user"].reset_mock()
        result = await e2e_akuvox_lock.async_set_usercode(1, "9999", "E2E Guest")

        assert result is True
        assert akuvox_mock_services["add_user"].call_count >= 1

    async def test_set_usercode_existing(
        self,
        hass: HomeAssistant,
        e2e_akuvox_lock: AkuvoxLock,
        akuvox_lock_entity: er.RegistryEntry,
        akuvox_mock_services: dict[str, AsyncMock],
    ) -> None:
        """Set a code on an occupied slot and verify modify_user service was called."""
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
        result = await e2e_akuvox_lock.async_set_usercode(1, "5678", "Updated")

        assert result is True
        assert akuvox_mock_services["modify_user"].call_count >= 1

    async def test_clear_usercode(
        self,
        hass: HomeAssistant,
        e2e_akuvox_lock: AkuvoxLock,
        akuvox_lock_entity: er.RegistryEntry,
        akuvox_mock_services: dict[str, AsyncMock],
    ) -> None:
        """Clear a code and verify delete_user service was called."""
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
        result = await e2e_akuvox_lock.async_clear_usercode(1)

        assert result is True
        assert akuvox_mock_services["delete_user"].call_count >= 1


class TestGetUsercodes:
    """Verify reading usercodes from the Akuvox lock."""

    async def test_get_usercodes_returns_codes(
        self,
        hass: HomeAssistant,
        e2e_akuvox_lock: AkuvoxLock,
        akuvox_lock_entity: er.RegistryEntry,
        akuvox_mock_services: dict[str, AsyncMock],
    ) -> None:
        """Get usercodes returns mapped slot data from Akuvox.

        After initial setup with empty users, both managed slots should
        be EMPTY. When the mock reports a tagged user with a PIN on
        slot 1, that slot should report the PIN value.
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

        codes = await e2e_akuvox_lock.async_get_usercodes()

        assert codes[1] == "4321"
        assert codes[2] is SlotCode.EMPTY

    async def test_get_usercodes_empty(
        self,
        hass: HomeAssistant,
        e2e_akuvox_lock: AkuvoxLock,
    ) -> None:
        """All managed slots are EMPTY when the device has no users."""
        codes = await e2e_akuvox_lock.async_get_usercodes()

        assert codes[1] is SlotCode.EMPTY
        assert codes[2] is SlotCode.EMPTY
