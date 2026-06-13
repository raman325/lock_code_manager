"""Full lifecycle E2E tests for Matter lock provider."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.domain.credentials import WriteResult
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers.matter import MatterLock

# Module path where lock_helpers functions are imported in the provider
_PROVIDER_MODULE = "custom_components.lock_code_manager.providers.matter"


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
    """Verify set/clear operations call the correct Matter helpers via base orchestration."""

    async def test_set_usercode(
        self,
        hass: HomeAssistant,
        e2e_matter_lock: MatterLock,
        matter_mock_helpers: dict[str, AsyncMock],
    ) -> None:
        """Set a code via the base orchestration and verify set_lock_credential was called."""
        matter_mock_helpers["set_lock_credential"].reset_mock()
        matter_mock_helpers["get_lock_users"].reset_mock()
        matter_mock_helpers["set_lock_user"].reset_mock()
        with (
            patch(
                f"{_PROVIDER_MODULE}.get_lock_users",
                matter_mock_helpers["get_lock_users"],
            ),
            patch(
                f"{_PROVIDER_MODULE}.set_lock_user",
                matter_mock_helpers["set_lock_user"],
            ),
            patch(
                f"{_PROVIDER_MODULE}.set_lock_credential",
                matter_mock_helpers["set_lock_credential"],
            ),
        ):
            result = await e2e_matter_lock.async_set_usercode(4, "5678", "Test User")

        assert result is WriteResult.CONFIRMED
        assert matter_mock_helpers["set_lock_credential"].call_count >= 1

    async def test_clear_usercode(
        self,
        hass: HomeAssistant,
        e2e_matter_lock: MatterLock,
        matter_mock_helpers: dict[str, AsyncMock],
    ) -> None:
        """Clear a code via the base orchestration and verify clear_lock_credential was called."""
        matter_mock_helpers["clear_lock_credential"].reset_mock()
        matter_mock_helpers["get_lock_users"].reset_mock()
        # Slot 2 is occupied (user_index 2) in the default fixture helpers
        with (
            patch(
                f"{_PROVIDER_MODULE}.get_lock_users",
                matter_mock_helpers["get_lock_users"],
            ),
            patch(
                f"{_PROVIDER_MODULE}.clear_lock_credential",
                matter_mock_helpers["clear_lock_credential"],
            ),
            patch(
                f"{_PROVIDER_MODULE}.clear_lock_user",
                matter_mock_helpers["clear_lock_user"],
            ),
        ):
            result = await e2e_matter_lock.async_clear_usercode(2)

        assert result is True
        assert matter_mock_helpers["clear_lock_credential"].call_count >= 1

    async def test_set_usercode_optimistic_update(
        self,
        hass: HomeAssistant,
        e2e_matter_lock: MatterLock,
        matter_mock_helpers: dict[str, AsyncMock],
    ) -> None:
        """
        After set, the coordinator has the optimistic UNREADABLE_CODE value.

        Matter PINs are write-only so optimistic updates use UNREADABLE_CODE
        instead of the actual PIN value. The push comes from async_set_credential.
        """
        with (
            patch(
                f"{_PROVIDER_MODULE}.get_lock_users",
                matter_mock_helpers["get_lock_users"],
            ),
            patch(
                f"{_PROVIDER_MODULE}.set_lock_user",
                matter_mock_helpers["set_lock_user"],
            ),
            patch(
                f"{_PROVIDER_MODULE}.set_lock_credential",
                matter_mock_helpers["set_lock_credential"],
            ),
        ):
            await e2e_matter_lock.async_set_usercode(4, "5678", "Test User")

        assert e2e_matter_lock.coordinator.data.get(4) is SlotCredential.unreadable()

    async def test_clear_usercode_optimistic_update(
        self,
        hass: HomeAssistant,
        e2e_matter_lock: MatterLock,
        matter_mock_helpers: dict[str, AsyncMock],
    ) -> None:
        """After clear, the coordinator has SlotCredential.empty()."""
        with (
            patch(
                f"{_PROVIDER_MODULE}.get_lock_users",
                matter_mock_helpers["get_lock_users"],
            ),
            patch(
                f"{_PROVIDER_MODULE}.clear_lock_credential",
                matter_mock_helpers["clear_lock_credential"],
            ),
            patch(
                f"{_PROVIDER_MODULE}.clear_lock_user",
                matter_mock_helpers["clear_lock_user"],
            ),
        ):
            await e2e_matter_lock.async_clear_usercode(2)

        assert e2e_matter_lock.coordinator.data.get(2) is SlotCredential.empty()


class TestGetUsercodes:
    """Verify reading usercodes from the Matter lock."""

    async def test_get_usercodes_returns_codes(
        self,
        hass: HomeAssistant,
        e2e_matter_lock: MatterLock,
        lock_entity: er.RegistryEntry,
        matter_mock_helpers: dict[str, AsyncMock],
    ) -> None:
        """
        Get usercodes returns slot occupancy from Matter.

        The base async_get_usercodes projects async_get_users output onto
        the managed-slot map. Slot 1 is occupied (unreadable), slot 2 is
        empty when the mock reports only a user on slot 1.
        """
        mock_get_lock_users = AsyncMock(
            return_value={
                "max_users": 10,
                "users": [
                    {
                        "user_index": 1,
                        "credentials": [
                            {
                                "type": "pin",
                                "index": 1,
                            }
                        ],
                    },
                ],
            }
        )

        with patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users):
            codes = await e2e_matter_lock.async_get_usercodes()

        assert codes[1] is SlotCredential.unreadable()
        assert codes[2] is SlotCredential.empty()
