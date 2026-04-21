"""Full lifecycle E2E tests for Matter lock provider."""

from __future__ import annotations

from unittest.mock import AsyncMock

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.models import SlotCode
from custom_components.lock_code_manager.providers.matter import (
    MATTER_DOMAIN,
    MatterLock,
)
from tests.providers.helpers import register_mock_service


class TestFullSetupLifecycle:
    """Verify LCM correctly discovers and sets up the Matter provider."""

    async def test_provider_discovered_as_matter(
        self,
        hass: HomeAssistant,
        lcm_config_entry: MockConfigEntry,
        lock_entity: er.RegistryEntry,
    ) -> None:
        """Verify LCM discovers the Matter lock and creates a MatterLock."""
        lock = lcm_config_entry.runtime_data.locks.get(lock_entity.entity_id)
        assert lock is not None
        assert isinstance(lock, MatterLock)

    async def test_coordinator_created(
        self,
        hass: HomeAssistant,
        e2e_matter_lock: MatterLock,
    ) -> None:
        """The coordinator is created and attached to the provider."""
        assert e2e_matter_lock.coordinator is not None


class TestSetAndClearUsercodes:
    """Verify set/clear operations call the correct Matter services."""

    async def test_set_usercode(
        self,
        hass: HomeAssistant,
        e2e_matter_lock: MatterLock,
        matter_mock_services: dict[str, AsyncMock],
    ) -> None:
        """Set a code via the provider and verify the Matter service was called."""
        matter_mock_services["set_lock_credential"].reset_mock()
        result = await e2e_matter_lock.async_set_usercode(4, "5678", "Test User")

        assert result is True
        assert matter_mock_services["set_lock_credential"].call_count >= 1

    async def test_clear_usercode(
        self,
        hass: HomeAssistant,
        e2e_matter_lock: MatterLock,
        matter_mock_services: dict[str, AsyncMock],
    ) -> None:
        """Clear a code via the provider and verify the Matter service was called."""
        matter_mock_services["clear_lock_credential"].reset_mock()
        result = await e2e_matter_lock.async_clear_usercode(2)

        assert result is True
        assert matter_mock_services["clear_lock_credential"].call_count >= 1

    async def test_set_usercode_optimistic_update(
        self,
        hass: HomeAssistant,
        e2e_matter_lock: MatterLock,
    ) -> None:
        """After set, the coordinator has the optimistic UNREADABLE_CODE value.

        Matter PINs are write-only so optimistic updates use UNREADABLE_CODE
        instead of the actual PIN value.
        """
        await e2e_matter_lock.async_set_usercode(4, "5678", "Test User")

        assert e2e_matter_lock.coordinator.data.get(4) is SlotCode.UNREADABLE_CODE

    async def test_clear_usercode_optimistic_update(
        self,
        hass: HomeAssistant,
        e2e_matter_lock: MatterLock,
    ) -> None:
        """After clear, the coordinator has SlotCode.EMPTY."""
        await e2e_matter_lock.async_clear_usercode(2)

        assert e2e_matter_lock.coordinator.data.get(2) is SlotCode.EMPTY


class TestGetUsercodes:
    """Verify reading usercodes from the Matter lock."""

    async def test_get_usercodes_returns_codes(
        self,
        hass: HomeAssistant,
        e2e_matter_lock: MatterLock,
        lock_entity: er.RegistryEntry,
        matter_mock_services: dict[str, AsyncMock],
    ) -> None:
        """Get usercodes returns slot occupancy from Matter.

        After initial setup with empty users, both managed slots should
        be EMPTY. When the mock reports a user with a PIN credential on
        slot 1, that slot should become UNREADABLE_CODE.
        """
        entity_id = lock_entity.entity_id

        # Override the get_lock_users mock to report an occupied slot
        matter_mock_services["get_lock_users"] = AsyncMock(
            return_value={
                entity_id: {
                    "max_users": 10,
                    "users": [
                        {
                            "user_index": 1,
                            "credentials": [
                                {
                                    "credential_type": "pin",
                                    "credential_index": 1,
                                }
                            ],
                        },
                    ],
                },
            }
        )
        register_mock_service(
            hass,
            MATTER_DOMAIN,
            "get_lock_users",
            matter_mock_services["get_lock_users"],
        )

        codes = await e2e_matter_lock.async_get_usercodes()

        assert codes[1] is SlotCode.UNREADABLE_CODE
        assert codes[2] is SlotCode.EMPTY
