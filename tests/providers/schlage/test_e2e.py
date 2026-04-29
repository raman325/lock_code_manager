"""Full lifecycle E2E tests for Schlage lock provider."""

from __future__ import annotations

from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.models import SlotCode
from custom_components.lock_code_manager.providers.schlage import (
    SCHLAGE_DOMAIN,
    SchlageLock,
)
from tests.providers.helpers import register_mock_service


class TestFullSetupLifecycle:
    """Verify LCM correctly discovers and sets up the Schlage provider."""

    async def test_provider_discovered_as_schlage(
        self,
        hass: HomeAssistant,
        lcm_config_entry: MockConfigEntry,
        schlage_lock_entity: er.RegistryEntry,
    ) -> None:
        """Verify LCM discovers the Schlage lock and creates a SchlageLock."""
        lock = lcm_config_entry.runtime_data.locks.get(schlage_lock_entity.entity_id)
        assert lock is not None
        assert isinstance(lock, SchlageLock)

    async def test_coordinator_created(
        self,
        hass: HomeAssistant,
        e2e_schlage_lock: SchlageLock,
    ) -> None:
        """The coordinator is created and attached to the provider."""
        assert e2e_schlage_lock.coordinator is not None


class TestSetAndClearUsercodes:
    """Verify set/clear operations call the correct Schlage services."""

    async def test_set_usercode(
        self,
        hass: HomeAssistant,
        e2e_schlage_lock: SchlageLock,
        schlage_mock_services: dict[str, AsyncMock],
    ) -> None:
        """Set a code via the provider and verify the add_code service was called."""
        schlage_mock_services["add_code"].reset_mock()
        result = await e2e_schlage_lock.async_set_usercode(1, "9999", "Test User")

        assert result is True
        assert schlage_mock_services["add_code"].call_count >= 1
        add_call = schlage_mock_services["add_code"].call_args[0][0]
        assert add_call.data["name"] == "[LCM:1] Test User"
        assert add_call.data["code"] == "9999"

    async def test_clear_usercode(
        self,
        hass: HomeAssistant,
        e2e_schlage_lock: SchlageLock,
        schlage_lock_entity: er.RegistryEntry,
        schlage_mock_services: dict[str, AsyncMock],
    ) -> None:
        """Clear a code via the provider and verify the delete_code service was called."""
        entity_id = schlage_lock_entity.entity_id

        # First set a code so there is something to clear
        schlage_mock_services["get_codes"] = AsyncMock(
            return_value={
                entity_id: {
                    "code1": {"name": "[LCM:1] Guest", "code": "****"},
                },
            }
        )
        register_mock_service(
            hass, SCHLAGE_DOMAIN, "get_codes", schlage_mock_services["get_codes"]
        )
        schlage_mock_services["delete_code"].reset_mock()

        result = await e2e_schlage_lock.async_clear_usercode(1)

        assert result is True
        assert schlage_mock_services["delete_code"].call_count >= 1

    async def test_coordinator_reflects_set_usercode(
        self,
        hass: HomeAssistant,
        e2e_schlage_lock: SchlageLock,
        schlage_lock_entity: er.RegistryEntry,
        schlage_mock_services: dict[str, AsyncMock],
    ) -> None:
        """
        After a coordinator refresh, data reflects the lock state.

        Schlage is not a push provider, so the coordinator reads state
        from the lock via get_codes. When the mock reports a tagged code
        on slot 1, the coordinator data should show UNREADABLE_CODE.
        """
        entity_id = schlage_lock_entity.entity_id

        schlage_mock_services["get_codes"] = AsyncMock(
            return_value={
                entity_id: {
                    "code1": {"name": "[LCM:1] Test User", "code": "****"},
                },
            }
        )
        register_mock_service(
            hass, SCHLAGE_DOMAIN, "get_codes", schlage_mock_services["get_codes"]
        )

        await e2e_schlage_lock.coordinator.async_refresh()

        assert e2e_schlage_lock.coordinator.data.get(1) is SlotCode.UNREADABLE_CODE
        assert e2e_schlage_lock.coordinator.data.get(2) is SlotCode.EMPTY

    async def test_coordinator_reflects_clear_usercode(
        self,
        hass: HomeAssistant,
        e2e_schlage_lock: SchlageLock,
        schlage_lock_entity: er.RegistryEntry,
        schlage_mock_services: dict[str, AsyncMock],
    ) -> None:
        """After a coordinator refresh with empty codes, all slots are EMPTY."""
        entity_id = schlage_lock_entity.entity_id

        # Ensure get_codes returns empty
        schlage_mock_services["get_codes"] = AsyncMock(return_value={entity_id: {}})
        register_mock_service(
            hass, SCHLAGE_DOMAIN, "get_codes", schlage_mock_services["get_codes"]
        )

        await e2e_schlage_lock.coordinator.async_refresh()

        assert e2e_schlage_lock.coordinator.data.get(1) is SlotCode.EMPTY
        assert e2e_schlage_lock.coordinator.data.get(2) is SlotCode.EMPTY


class TestGetUsercodes:
    """Verify reading usercodes from the Schlage lock."""

    async def test_get_usercodes_returns_codes(
        self,
        hass: HomeAssistant,
        e2e_schlage_lock: SchlageLock,
        schlage_lock_entity: er.RegistryEntry,
        schlage_mock_services: dict[str, AsyncMock],
    ) -> None:
        """
        Get usercodes returns slot occupancy from Schlage.

        After initial setup with empty codes, both managed slots should
        be EMPTY. When the mock reports a tagged code on slot 1, that
        slot should become UNREADABLE_CODE.
        """
        entity_id = schlage_lock_entity.entity_id

        # Override the get_codes mock to report an occupied slot
        schlage_mock_services["get_codes"] = AsyncMock(
            return_value={
                entity_id: {
                    "code1": {"name": "[LCM:1] Guest", "code": "****"},
                },
            }
        )
        register_mock_service(
            hass,
            SCHLAGE_DOMAIN,
            "get_codes",
            schlage_mock_services["get_codes"],
        )

        codes = await e2e_schlage_lock.async_get_usercodes()

        assert codes[1] is SlotCode.UNREADABLE_CODE
        assert codes[2] is SlotCode.EMPTY
