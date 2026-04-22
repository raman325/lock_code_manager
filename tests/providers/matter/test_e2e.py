"""Full lifecycle E2E tests for Matter lock provider."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.models import SlotCode
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
    """Verify set/clear operations call the correct Matter helpers."""

    async def test_set_usercode(
        self,
        hass: HomeAssistant,
        e2e_matter_lock: MatterLock,
        matter_mock_helpers: dict[str, AsyncMock],
    ) -> None:
        """Set a code via the provider and verify the Matter helper was called."""
        matter_mock_helpers["set_lock_credential"].reset_mock()
        with (
            patch(
                f"{_PROVIDER_MODULE}.set_lock_credential",
                matter_mock_helpers["set_lock_credential"],
            ),
            patch(
                f"{_PROVIDER_MODULE}.set_lock_user",
                matter_mock_helpers["set_lock_user"],
            ),
        ):
            result = await e2e_matter_lock.async_set_usercode(4, "5678", "Test User")

        assert result is True
        assert matter_mock_helpers["set_lock_credential"].call_count >= 1

    async def test_clear_usercode(
        self,
        hass: HomeAssistant,
        e2e_matter_lock: MatterLock,
        matter_mock_helpers: dict[str, AsyncMock],
    ) -> None:
        """Clear a code via the provider and verify the Matter helper was called."""
        matter_mock_helpers["clear_lock_credential"].reset_mock()
        with (
            patch(
                f"{_PROVIDER_MODULE}.get_lock_credential_status",
                matter_mock_helpers["get_lock_credential_status"],
            ),
            patch(
                f"{_PROVIDER_MODULE}.clear_lock_credential",
                matter_mock_helpers["clear_lock_credential"],
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
        """After set, the coordinator has the optimistic UNREADABLE_CODE value.

        Matter PINs are write-only so optimistic updates use UNREADABLE_CODE
        instead of the actual PIN value.
        """
        with (
            patch(
                f"{_PROVIDER_MODULE}.set_lock_credential",
                matter_mock_helpers["set_lock_credential"],
            ),
            patch(
                f"{_PROVIDER_MODULE}.set_lock_user",
                matter_mock_helpers["set_lock_user"],
            ),
        ):
            await e2e_matter_lock.async_set_usercode(4, "5678", "Test User")

        assert e2e_matter_lock.coordinator.data.get(4) is SlotCode.UNREADABLE_CODE

    async def test_clear_usercode_optimistic_update(
        self,
        hass: HomeAssistant,
        e2e_matter_lock: MatterLock,
        matter_mock_helpers: dict[str, AsyncMock],
    ) -> None:
        """After clear, the coordinator has SlotCode.EMPTY."""
        with (
            patch(
                f"{_PROVIDER_MODULE}.get_lock_credential_status",
                matter_mock_helpers["get_lock_credential_status"],
            ),
            patch(
                f"{_PROVIDER_MODULE}.clear_lock_credential",
                matter_mock_helpers["clear_lock_credential"],
            ),
        ):
            await e2e_matter_lock.async_clear_usercode(2)

        assert e2e_matter_lock.coordinator.data.get(2) is SlotCode.EMPTY


class TestGetUsercodes:
    """Verify reading usercodes from the Matter lock."""

    async def test_get_usercodes_returns_codes(
        self,
        hass: HomeAssistant,
        e2e_matter_lock: MatterLock,
        lock_entity: er.RegistryEntry,
        matter_mock_helpers: dict[str, AsyncMock],
    ) -> None:
        """Get usercodes returns slot occupancy from Matter.

        After initial setup with empty users, both managed slots should
        be EMPTY. When the mock reports a user with a PIN credential on
        slot 1, that slot should become UNREADABLE_CODE.
        """
        # Override the get_lock_users mock to report an occupied slot
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

        assert codes[1] is SlotCode.UNREADABLE_CODE
        assert codes[2] is SlotCode.EMPTY
