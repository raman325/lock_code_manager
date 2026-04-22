"""Test the Matter lock provider."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from matter_server.common.models import EventType, MatterNodeEvent
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.lock_code_manager.exceptions import (
    CodeRejectedError,
    DuplicateCodeError,
    LockCodeManagerError,
    LockCodeManagerProviderError,
    LockDisconnected,
)
from custom_components.lock_code_manager.models import SlotCode
from custom_components.lock_code_manager.providers.matter import (
    MatterLock,
    SetCredentialFailedError,
)
from tests.providers.helpers import ServiceProviderConnectionTests

# Simple fixture entity ID (for service-level tests without full integration)
LOCK_ENTITY_ID = "lock.matter_test_matter_lock"

# Module path where lock_helpers functions are imported in the provider
_PROVIDER_MODULE = "custom_components.lock_code_manager.providers.matter"


def _make_set_credential_failed_error(
    status: str = "duplicate",
) -> HomeAssistantError:
    """Create a mock SetCredentialFailedError with translation_placeholders."""
    err = SetCredentialFailedError(
        f"Failed to set credential: lock returned status `{status}`"
    )
    err.translation_placeholders = {"status": status}
    return err


@pytest.fixture
def provider_config_entry(matter_config_entry: MockConfigEntry) -> MockConfigEntry:
    """Alias matter_config_entry for shared test mixins."""
    return matter_config_entry


@pytest.fixture
def provider_domain() -> str:
    """Return the provider integration domain."""
    return "matter"


@pytest.fixture
def provider_lock_class() -> type[MatterLock]:
    """Return the provider lock class."""
    return MatterLock


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


async def test_domain_property(matter_lock_simple: MatterLock) -> None:
    """Test that domain returns 'matter'."""
    assert matter_lock_simple.domain == "matter"


async def test_supports_code_slot_events(matter_lock_simple: MatterLock) -> None:
    """Test that Matter locks support code slot events via LockOperation."""
    assert matter_lock_simple.supports_code_slot_events is True


async def test_supports_push(matter_lock_simple: MatterLock) -> None:
    """Test that Matter locks support push-based updates."""
    assert matter_lock_simple.supports_push is True


async def test_usercode_scan_interval(matter_lock_simple: MatterLock) -> None:
    """Test that scan interval is 5 minutes."""
    assert matter_lock_simple.usercode_scan_interval == timedelta(minutes=5)


async def test_hard_refresh_interval(matter_lock_simple: MatterLock) -> None:
    """Test that hard refresh interval is 1 hour for drift detection."""
    assert matter_lock_simple.hard_refresh_interval == timedelta(hours=1)


# ---------------------------------------------------------------------------
# Connection and availability tests (shared)
# ---------------------------------------------------------------------------


class TestConnection(ServiceProviderConnectionTests):
    """Connection tests for Matter provider using shared mixin."""


class TestDeviceAvailability:
    """Device availability tests for Matter provider."""

    async def test_is_device_available_success(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """Test device availability returns True on successful helper call."""
        mock_get_lock_info = AsyncMock(return_value={})
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.get_lock_info", mock_get_lock_info),
        ):
            assert await matter_lock_simple.async_is_device_available() is True

    async def test_is_device_available_error(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """Test device availability returns False when helper call fails."""
        mock_get_lock_info = AsyncMock(side_effect=HomeAssistantError("device offline"))
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.get_lock_info", mock_get_lock_info),
        ):
            assert await matter_lock_simple.async_is_device_available() is False

    async def test_is_device_available_no_client(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """Test device availability returns False when client unavailable."""
        with patch.object(matter_lock_simple, "_get_matter_client", return_value=None):
            assert await matter_lock_simple.async_is_device_available() is False


# ---------------------------------------------------------------------------
# Setup tests
# ---------------------------------------------------------------------------


async def test_setup(
    hass: HomeAssistant,
    matter_lock_simple: MatterLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test that setup validates lock supports user management and PIN credentials."""
    mock_get_lock_info = AsyncMock(
        return_value={
            "supports_user_management": True,
            "supported_credential_types": ["pin"],
        }
    )
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.get_lock_info", mock_get_lock_info),
    ):
        await matter_lock_simple.async_setup(simple_lcm_config_entry)
    assert mock_get_lock_info.call_count == 1


async def test_setup_unsupported_lock(
    hass: HomeAssistant,
    matter_lock_simple: MatterLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test that setup raises when lock does not support user management."""
    mock_get_lock_info = AsyncMock(return_value={"supports_user_management": False})
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.get_lock_info", mock_get_lock_info),
    ):
        with pytest.raises(
            LockCodeManagerError, match="does not support user management"
        ):
            await matter_lock_simple.async_setup(simple_lcm_config_entry)


async def test_setup_no_pin_support(
    hass: HomeAssistant,
    matter_lock_simple: MatterLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test that setup raises when lock supports users but not PIN credentials."""
    mock_get_lock_info = AsyncMock(
        return_value={
            "supports_user_management": True,
            "supported_credential_types": ["rfid"],
        }
    )
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.get_lock_info", mock_get_lock_info),
    ):
        with pytest.raises(
            LockCodeManagerError, match="does not support PIN credentials"
        ):
            await matter_lock_simple.async_setup(simple_lcm_config_entry)


# ---------------------------------------------------------------------------
# get_usercodes tests
# ---------------------------------------------------------------------------


async def test_get_usercodes_no_users(
    hass: HomeAssistant,
    matter_lock_simple: MatterLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test get_usercodes when no users exist on the lock."""
    mock_get_lock_users = AsyncMock(return_value={"max_users": 10, "users": []})
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users),
    ):
        codes = await matter_lock_simple.async_get_usercodes()

    assert codes[1] is SlotCode.EMPTY
    assert codes[2] is SlotCode.EMPTY


async def test_get_usercodes_no_configured_slots(
    hass: HomeAssistant,
    matter_lock_simple: MatterLock,
) -> None:
    """Test get_usercodes returns empty dict when no slots configured and no occupied slots."""
    mock_get_lock_users = AsyncMock(return_value={"users": []})
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users),
    ):
        codes = await matter_lock_simple.async_get_usercodes()
    assert codes == {}


async def test_get_usercodes_unmanaged_occupied_slots(
    hass: HomeAssistant,
    matter_lock_simple: MatterLock,
) -> None:
    """Test get_usercodes returns unmanaged occupied slots as UNREADABLE_CODE."""
    mock_get_lock_users = AsyncMock(
        return_value={
            "users": [
                {
                    "credentials": [
                        {"type": "pin", "index": 5},
                        {"type": "pin", "index": 8},
                    ]
                }
            ]
        }
    )
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users),
    ):
        codes = await matter_lock_simple.async_get_usercodes()
    assert codes == {5: SlotCode.UNREADABLE_CODE, 8: SlotCode.UNREADABLE_CODE}


async def test_get_usercodes_none_credential_index_skipped(
    hass: HomeAssistant,
    matter_lock_simple: MatterLock,
) -> None:
    """Test that PIN credentials with a None index are skipped."""
    mock_get_lock_users = AsyncMock(
        return_value={
            "users": [
                {
                    "credentials": [
                        {"type": "pin"},  # no "index" key → cred_index is None
                        {"type": "pin", "index": 4},
                    ]
                }
            ]
        }
    )
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users),
    ):
        codes = await matter_lock_simple.async_get_usercodes()
    # Only slot 4 should appear; the credential with no index is skipped
    assert codes == {4: SlotCode.UNREADABLE_CODE}


async def test_get_usercodes_invalid_credential_index_skipped(
    hass: HomeAssistant,
    matter_lock_simple: MatterLock,
) -> None:
    """Test that invalid credential_index values are skipped with a warning."""
    mock_get_lock_users = AsyncMock(
        return_value={
            "users": [
                {
                    "credentials": [
                        {"type": "pin", "index": "bad"},
                        {"type": "pin", "index": 3},
                    ]
                }
            ]
        }
    )
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users),
    ):
        codes = await matter_lock_simple.async_get_usercodes()
    # Only slot 3 should appear; "bad" index is skipped
    assert codes == {3: SlotCode.UNREADABLE_CODE}


# ---------------------------------------------------------------------------
# set_usercode tests
# ---------------------------------------------------------------------------


async def test_set_usercode_no_name(
    hass: HomeAssistant, matter_lock_simple: MatterLock
) -> None:
    """Test set_usercode without a name only calls set_lock_credential."""
    mock_set_credential = AsyncMock(
        return_value={"credential_index": 3, "user_index": 3}
    )
    mock_set_user = AsyncMock(return_value={})
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
        patch(f"{_PROVIDER_MODULE}.set_lock_user", mock_set_user),
    ):
        result = await matter_lock_simple.async_set_usercode(3, "9999")

    assert result is True
    assert mock_set_credential.call_count == 1
    assert mock_set_user.call_count == 0


async def test_set_usercode_skips_name_when_no_user_index(
    hass: HomeAssistant, matter_lock_simple: MatterLock
) -> None:
    """Test set_usercode skips set_lock_user when response has no user_index."""
    mock_set_credential = AsyncMock(return_value={})
    mock_set_user = AsyncMock(return_value={})
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
        patch(f"{_PROVIDER_MODULE}.set_lock_user", mock_set_user),
    ):
        result = await matter_lock_simple.async_set_usercode(1, "1234", "User One")

    assert result is True
    assert mock_set_credential.call_count == 1
    assert mock_set_user.call_count == 0


# ---------------------------------------------------------------------------
# clear_usercode tests
# ---------------------------------------------------------------------------


async def test_clear_usercode_already_empty(
    hass: HomeAssistant, matter_lock_simple: MatterLock
) -> None:
    """Test clear_usercode returns False when the credential does not exist."""
    mock_get_status = AsyncMock(return_value={"credential_exists": False})
    mock_clear = AsyncMock(return_value={})
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.get_lock_credential_status", mock_get_status),
        patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
    ):
        result = await matter_lock_simple.async_clear_usercode(2)

    assert result is False
    # Only the credential status check should have been called
    assert mock_get_status.call_count == 1
    assert mock_clear.call_count == 0


# ---------------------------------------------------------------------------
# hard_refresh_codes tests
# ---------------------------------------------------------------------------


async def test_hard_refresh_codes(
    hass: HomeAssistant,
    matter_lock_simple: MatterLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test hard_refresh_codes returns same result as get_usercodes."""
    mock_get_lock_users = AsyncMock(
        return_value={
            "max_users": 10,
            "users": [
                {
                    "user_index": 1,
                    "credentials": [
                        {"type": "pin", "index": 2},
                    ],
                }
            ],
        }
    )
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users),
    ):
        codes = await matter_lock_simple.async_hard_refresh_codes()

    assert codes[1] is SlotCode.EMPTY
    assert codes[2] is SlotCode.UNREADABLE_CODE


# ---------------------------------------------------------------------------
# Error tests
# ---------------------------------------------------------------------------


async def test_helper_failure_raises_lock_disconnected(
    hass: HomeAssistant, matter_lock_simple: MatterLock
) -> None:
    """Test that Matter helper failures raise LockDisconnected."""
    mock_set_credential = AsyncMock(side_effect=HomeAssistantError("connection lost"))
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
    ):
        with pytest.raises(LockDisconnected, match="connection lost"):
            await matter_lock_simple.async_set_usercode(1, "1234")


async def test_service_validation_error_raises_provider_error(
    hass: HomeAssistant, matter_lock_simple: MatterLock
) -> None:
    """Test that ServiceValidationError raises LockCodeManagerProviderError, not LockDisconnected."""
    mock_set_credential = AsyncMock(side_effect=ServiceValidationError("invalid data"))
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
    ):
        with pytest.raises(LockCodeManagerProviderError, match="rejected input"):
            await matter_lock_simple.async_set_usercode(1, "1234")


async def test_set_usercode_user_name_failure_does_not_propagate(
    hass: HomeAssistant, matter_lock_simple: MatterLock
) -> None:
    """Test that a user name set failure does not propagate when credential set succeeds."""
    mock_set_credential = AsyncMock(
        return_value={"credential_index": 1, "user_index": 1}
    )
    mock_set_user = AsyncMock(
        side_effect=HomeAssistantError("500 Internal Server Error")
    )
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
        patch(f"{_PROVIDER_MODULE}.set_lock_user", mock_set_user),
    ):
        result = await matter_lock_simple.async_set_usercode(1, "1234", name="Test")

    assert result is True


async def test_set_usercode_duplicate_direct_raises_immediately(
    hass: HomeAssistant, matter_lock_simple: MatterLock
) -> None:
    """Test that a duplicate on a direct (user-initiated) call raises immediately."""
    mock_set_credential = AsyncMock(
        side_effect=_make_set_credential_failed_error("duplicate")
    )
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
    ):
        with pytest.raises(DuplicateCodeError) as exc_info:
            await matter_lock_simple.async_set_usercode(1, "1234")
    assert exc_info.value.code_slot == 1
    assert exc_info.value.lock_entity_id == LOCK_ENTITY_ID
    # Only one set attempt, no clear-and-retry for direct calls
    assert mock_set_credential.call_count == 1


async def test_set_usercode_duplicate_sync_retries_after_clear(
    hass: HomeAssistant, matter_lock_simple: MatterLock
) -> None:
    """Test that a duplicate during sync clears and retries successfully."""
    mock_set_credential = AsyncMock(
        side_effect=[
            _make_set_credential_failed_error("duplicate"),
            {"credential_index": 1, "user_index": 1},
        ]
    )
    mock_clear = AsyncMock(return_value={})
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
        patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
    ):
        result = await matter_lock_simple.async_set_usercode(1, "1234", source="sync")

    assert result is True
    assert mock_set_credential.call_count == 2
    assert mock_clear.call_count == 1


async def test_set_usercode_duplicate_sync_persistent_raises(
    hass: HomeAssistant, matter_lock_simple: MatterLock
) -> None:
    """Test that a persistent duplicate during sync raises after retry."""
    mock_set_credential = AsyncMock(
        side_effect=_make_set_credential_failed_error("duplicate")
    )
    mock_clear = AsyncMock(return_value={})
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
        patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
    ):
        with pytest.raises(DuplicateCodeError) as exc_info:
            await matter_lock_simple.async_set_usercode(1, "1234", source="sync")
    assert exc_info.value.code_slot == 1
    assert exc_info.value.lock_entity_id == LOCK_ENTITY_ID
    assert mock_set_credential.call_count == 2
    assert mock_clear.call_count == 1


async def test_set_credential_failed_non_duplicate_raises_code_rejected(
    hass: HomeAssistant, matter_lock_simple: MatterLock
) -> None:
    """Test that SetCredentialFailedError with non-duplicate status raises CodeRejectedError."""
    mock_set_credential = AsyncMock(
        side_effect=_make_set_credential_failed_error("occupied")
    )
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
    ):
        with pytest.raises(CodeRejectedError) as exc_info:
            await matter_lock_simple.async_set_usercode(1, "1234")
    assert exc_info.value.code_slot == 1
    assert exc_info.value.lock_entity_id == LOCK_ENTITY_ID


async def test_clear_lock_credential_service_validation_error(
    hass: HomeAssistant, matter_lock_simple: MatterLock
) -> None:
    """Test _clear_lock_credential raises LockCodeManagerProviderError on ServiceValidationError."""
    mock_clear = AsyncMock(side_effect=ServiceValidationError("bad slot"))
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
    ):
        with pytest.raises(LockCodeManagerProviderError, match="rejected input"):
            await matter_lock_simple._clear_lock_credential(1)


async def test_clear_lock_credential_communication_error(
    hass: HomeAssistant, matter_lock_simple: MatterLock
) -> None:
    """Test _clear_lock_credential raises LockDisconnected on HomeAssistantError."""
    mock_clear = AsyncMock(side_effect=HomeAssistantError("connection lost"))
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
    ):
        with pytest.raises(LockDisconnected, match="clear_lock_credential failed"):
            await matter_lock_simple._clear_lock_credential(1)


async def test_get_usercodes_client_unavailable(
    hass: HomeAssistant, matter_lock_simple: MatterLock
) -> None:
    """Test async_get_usercodes raises LockDisconnected when client/node unavailable."""
    with patch.object(matter_lock_simple, "_get_matter_client", return_value=None):
        with pytest.raises(LockDisconnected, match="client or node unavailable"):
            await matter_lock_simple.async_get_usercodes()


async def test_get_usercodes_get_lock_users_service_validation_error(
    hass: HomeAssistant, matter_lock_simple: MatterLock
) -> None:
    """Test async_get_usercodes raises LockCodeManagerProviderError on ServiceValidationError from get_lock_users."""
    mock_get_lock_users = AsyncMock(side_effect=ServiceValidationError("bad request"))
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users),
    ):
        with pytest.raises(LockCodeManagerProviderError, match="rejected input"):
            await matter_lock_simple.async_get_usercodes()


async def test_get_usercodes_get_lock_users_communication_error(
    hass: HomeAssistant, matter_lock_simple: MatterLock
) -> None:
    """Test async_get_usercodes raises LockDisconnected on HomeAssistantError from get_lock_users."""
    mock_get_lock_users = AsyncMock(side_effect=HomeAssistantError("connection lost"))
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users),
    ):
        with pytest.raises(LockDisconnected, match="get_lock_users failed"):
            await matter_lock_simple.async_get_usercodes()


async def test_get_usercodes_invalid_users_type(
    hass: HomeAssistant, matter_lock_simple: MatterLock
) -> None:
    """Test async_get_usercodes raises LockCodeManagerProviderError when users is not a list."""
    mock_get_lock_users = AsyncMock(return_value={"users": "not-a-list"})
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users),
    ):
        with pytest.raises(
            LockCodeManagerProviderError, match="unexpected 'users' value"
        ):
            await matter_lock_simple.async_get_usercodes()


async def test_set_credential_failed_non_duplicate_on_retry_raises_code_rejected(
    hass: HomeAssistant, matter_lock_simple: MatterLock
) -> None:
    """Test that non-duplicate SetCredentialFailedError on retry raises CodeRejectedError."""
    mock_set_credential = AsyncMock(
        side_effect=[
            _make_set_credential_failed_error("duplicate"),
            _make_set_credential_failed_error("occupied"),
        ]
    )
    mock_clear = AsyncMock(return_value={})
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
        patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
    ):
        with pytest.raises(CodeRejectedError) as exc_info:
            await matter_lock_simple.async_set_usercode(1, "1234", source="sync")
    assert exc_info.value.code_slot == 1
    assert exc_info.value.lock_entity_id == LOCK_ENTITY_ID
    assert mock_set_credential.call_count == 2
    assert mock_clear.call_count == 1


async def test_clear_usercode_get_status_service_validation_error(
    hass: HomeAssistant, matter_lock_simple: MatterLock
) -> None:
    """Test async_clear_usercode raises LockCodeManagerProviderError on ServiceValidationError."""
    mock_get_status = AsyncMock(side_effect=ServiceValidationError("bad index"))
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.get_lock_credential_status", mock_get_status),
    ):
        with pytest.raises(
            LockCodeManagerProviderError, match="get_lock_credential_status rejected"
        ):
            await matter_lock_simple.async_clear_usercode(1)


async def test_clear_usercode_get_status_communication_error(
    hass: HomeAssistant, matter_lock_simple: MatterLock
) -> None:
    """Test async_clear_usercode raises LockDisconnected on HomeAssistantError from status check."""
    mock_get_status = AsyncMock(side_effect=HomeAssistantError("connection lost"))
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.get_lock_credential_status", mock_get_status),
    ):
        with pytest.raises(LockDisconnected, match="get_lock_credential_status failed"):
            await matter_lock_simple.async_clear_usercode(1)


async def test_get_usercodes_multiple_credential_types(
    hass: HomeAssistant,
    matter_lock_simple: MatterLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test that only PIN credentials are considered, not other types like RFID."""
    mock_get_lock_users = AsyncMock(
        return_value={
            "max_users": 10,
            "users": [
                {
                    "user_index": 1,
                    "credentials": [
                        {"type": "rfid", "index": 1},
                        {"type": "pin", "index": 2},
                    ],
                },
            ],
        }
    )
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users),
    ):
        codes = await matter_lock_simple.async_get_usercodes()

    # Slot 1 has only RFID credential, not PIN
    assert codes[1] is SlotCode.EMPTY
    # Slot 2 has a PIN credential
    assert codes[2] is SlotCode.UNREADABLE_CODE


async def test_get_matter_node_exception_returns_none(
    hass: HomeAssistant, matter_lock_simple: MatterLock
) -> None:
    """Test _get_matter_node returns None when get_node_from_device_entry raises."""
    # Give the lock a fake device entry so the device_entry guard is bypassed
    matter_lock_simple.device_entry = MagicMock()
    with patch(
        f"{_PROVIDER_MODULE}.get_node_from_device_entry",
        side_effect=Exception("node lookup failed"),
    ):
        result = matter_lock_simple._get_matter_node()
    assert result is None


async def test_setup_client_unavailable(
    hass: HomeAssistant,
    matter_lock_simple: MatterLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test async_setup raises LockDisconnected when _require_client_and_node fails."""
    with patch.object(matter_lock_simple, "_get_matter_client", return_value=None):
        with pytest.raises(LockDisconnected, match="client or node unavailable"):
            await matter_lock_simple.async_setup(simple_lcm_config_entry)


async def test_setup_get_lock_info_service_validation_error(
    hass: HomeAssistant,
    matter_lock_simple: MatterLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test async_setup raises LockCodeManagerProviderError on ServiceValidationError."""
    mock_get_lock_info = AsyncMock(side_effect=ServiceValidationError("bad input"))
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.get_lock_info", mock_get_lock_info),
    ):
        with pytest.raises(LockCodeManagerProviderError, match="rejected input"):
            await matter_lock_simple.async_setup(simple_lcm_config_entry)


async def test_setup_get_lock_info_communication_error(
    hass: HomeAssistant,
    matter_lock_simple: MatterLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test async_setup raises LockDisconnected on HomeAssistantError from get_lock_info."""
    mock_get_lock_info = AsyncMock(side_effect=HomeAssistantError("connection lost"))
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=MagicMock()),
        patch(f"{_PROVIDER_MODULE}.get_lock_info", mock_get_lock_info),
    ):
        with pytest.raises(LockDisconnected, match="get_lock_info failed"):
            await matter_lock_simple.async_setup(simple_lcm_config_entry)


async def test_require_client_and_node_no_client(
    hass: HomeAssistant, matter_lock_simple: MatterLock
) -> None:
    """Test _require_client_and_node raises LockDisconnected when client is None."""
    with patch.object(matter_lock_simple, "_get_matter_client", return_value=None):
        with pytest.raises(LockDisconnected, match="client or node unavailable"):
            matter_lock_simple._require_client_and_node()


async def test_require_client_and_node_no_node(
    hass: HomeAssistant, matter_lock_simple: MatterLock
) -> None:
    """Test _require_client_and_node raises LockDisconnected when node is None."""
    with (
        patch.object(
            matter_lock_simple, "_get_matter_client", return_value=MagicMock()
        ),
        patch.object(matter_lock_simple, "_get_matter_node", return_value=None),
    ):
        with pytest.raises(LockDisconnected, match="client or node unavailable"):
            matter_lock_simple._require_client_and_node()


# =============================================================================
# LockOperation event tests
# =============================================================================


def _make_node_event(
    cluster_id: int = 257,
    event_id: int = 2,
    data: dict[str, Any] | None = None,
) -> MatterNodeEvent:
    """Create a MatterNodeEvent for testing."""
    return MatterNodeEvent(
        node_id=1,
        endpoint_id=1,
        cluster_id=cluster_id,
        event_id=event_id,
        event_number=0,
        priority=1,
        timestamp=0,
        timestamp_type=0,
        data=data,
    )


class TestLockOperationEvent:
    """Test _on_node_event callback filtering and event firing."""

    def test_unlock_with_pin_credential(self, matter_lock_simple: MatterLock) -> None:
        """Unlock with PIN credential fires code slot event."""
        fired: list[dict[str, Any]] = []
        matter_lock_simple.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        matter_lock_simple._on_node_event(
            None,
            _make_node_event(
                data={
                    "lockOperationType": 1,  # kUnlock
                    "credentials": [
                        {"credentialType": 1, "credentialIndex": 2},  # PIN, slot 2
                    ],
                }
            ),
        )

        assert len(fired) == 1
        assert fired[0]["code_slot"] == 2
        assert fired[0]["to_locked"] is False
        assert fired[0]["action_text"] == "unlocked"

    def test_lock_with_pin_credential(self, matter_lock_simple: MatterLock) -> None:
        """Lock with PIN credential fires code slot event."""
        fired: list[dict[str, Any]] = []
        matter_lock_simple.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        matter_lock_simple._on_node_event(
            None,
            _make_node_event(
                data={
                    "lockOperationType": 0,  # kLock
                    "credentials": [
                        {"credentialType": 1, "credentialIndex": 5},
                    ],
                }
            ),
        )

        assert len(fired) == 1
        assert fired[0]["code_slot"] == 5
        assert fired[0]["to_locked"] is True
        assert fired[0]["action_text"] == "locked"

    def test_rfid_credential_ignored(self, matter_lock_simple: MatterLock) -> None:
        """RFID credential does not fire pin_used event."""
        fired: list[dict[str, Any]] = []
        matter_lock_simple.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        matter_lock_simple._on_node_event(
            None,
            _make_node_event(
                data={
                    "lockOperationType": 1,
                    "credentials": [
                        {"credentialType": 2, "credentialIndex": 3},  # RFID
                    ],
                }
            ),
        )

        assert len(fired) == 0

    def test_fingerprint_credential_ignored(
        self, matter_lock_simple: MatterLock
    ) -> None:
        """Fingerprint credential does not fire pin_used event."""
        fired: list[dict[str, Any]] = []
        matter_lock_simple.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        matter_lock_simple._on_node_event(
            None,
            _make_node_event(
                data={
                    "lockOperationType": 1,
                    "credentials": [
                        {"credentialType": 3, "credentialIndex": 1},  # Fingerprint
                    ],
                }
            ),
        )

        assert len(fired) == 0

    def test_wrong_cluster_ignored(self, matter_lock_simple: MatterLock) -> None:
        """Events from non-DoorLock clusters are ignored."""
        fired: list[dict[str, Any]] = []
        matter_lock_simple.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        matter_lock_simple._on_node_event(
            None,
            _make_node_event(
                cluster_id=6,  # OnOff cluster
                data={"credentials": [{"credentialType": 1, "credentialIndex": 1}]},
            ),
        )

        assert len(fired) == 0

    def test_wrong_event_id_ignored(self, matter_lock_simple: MatterLock) -> None:
        """Non-LockOperation DoorLock events are ignored."""
        fired: list[dict[str, Any]] = []
        matter_lock_simple.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        matter_lock_simple._on_node_event(
            None,
            _make_node_event(
                event_id=3,  # LockOperationError
                data={"credentials": [{"credentialType": 1, "credentialIndex": 1}]},
            ),
        )

        assert len(fired) == 0

    def test_no_credentials_ignored(self, matter_lock_simple: MatterLock) -> None:
        """Event without credentials is ignored."""
        fired: list[dict[str, Any]] = []
        matter_lock_simple.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        matter_lock_simple._on_node_event(
            None,
            _make_node_event(data={"lockOperationType": 1}),
        )

        assert len(fired) == 0

    def test_empty_credentials_ignored(self, matter_lock_simple: MatterLock) -> None:
        """Event with empty credentials list is ignored."""
        fired: list[dict[str, Any]] = []
        matter_lock_simple.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        matter_lock_simple._on_node_event(
            None,
            _make_node_event(data={"lockOperationType": 1, "credentials": []}),
        )

        assert len(fired) == 0

    def test_no_operation_type(self, matter_lock_simple: MatterLock) -> None:
        """Event without lockOperationType fires with to_locked=None."""
        fired: list[dict[str, Any]] = []
        matter_lock_simple.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        matter_lock_simple._on_node_event(
            None,
            _make_node_event(
                data={
                    "credentials": [{"credentialType": 1, "credentialIndex": 3}],
                }
            ),
        )

        assert len(fired) == 1
        assert fired[0]["to_locked"] is None
        assert fired[0]["action_text"] == "operated"

    def test_none_data_ignored(self, matter_lock_simple: MatterLock) -> None:
        """Event with None data is ignored (no credentials)."""
        fired: list[dict[str, Any]] = []
        matter_lock_simple.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        matter_lock_simple._on_node_event(None, _make_node_event(data=None))

        assert len(fired) == 0


# =============================================================================
# Event subscription lifecycle tests
# =============================================================================


class TestEventSubscription:
    """Test event subscription setup and teardown."""

    def test_matter_node_id_no_device(self, matter_lock_simple: MatterLock) -> None:
        """Test _matter_node_id returns None when no device entry."""
        matter_lock_simple.device_entry = None
        assert matter_lock_simple._matter_node_id is None

    def test_get_matter_client_no_data(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """Test _get_matter_client returns None when no Matter data."""
        hass.data.pop("matter", None)
        assert matter_lock_simple._get_matter_client() is None

    def test_setup_push_idempotent(self, matter_lock_simple: MatterLock) -> None:
        """Test setup_push_subscription is a no-op if already subscribed."""
        matter_lock_simple._event_unsub = lambda: None  # already subscribed
        matter_lock_simple.setup_push_subscription()  # should be a no-op
        # If it tried to subscribe again, it would fail (no client)
        assert matter_lock_simple._event_unsub is not None

    def test_setup_push_no_client_raises(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """Test setup_push_subscription raises when client unavailable."""
        hass.data.pop("matter", None)
        with pytest.raises(LockDisconnected):
            matter_lock_simple.setup_push_subscription()

    def test_teardown_push_unsubscribes(self, matter_lock_simple: MatterLock) -> None:
        """Test teardown_push_subscription cleans up event subscription."""
        unsub_called = [False]

        def _unsub() -> None:
            unsub_called[0] = True

        matter_lock_simple._event_unsub = _unsub
        matter_lock_simple.teardown_push_subscription()
        assert unsub_called[0]
        assert matter_lock_simple._event_unsub is None

    def test_teardown_push_no_subscription(
        self, matter_lock_simple: MatterLock
    ) -> None:
        """Test teardown_push_subscription handles no active subscription."""
        matter_lock_simple._event_unsub = None
        matter_lock_simple.teardown_push_subscription()  # should not crash

    # -- Tests using the full Matter integration fixture --

    def test_matter_node_id_resolves(self, matter_lock: MatterLock) -> None:
        """Test _matter_node_id resolves from real Matter integration device."""
        assert matter_lock._matter_node_id == 16  # from mock_door_lock.json

    def test_get_matter_client_from_integration(
        self, matter_lock: MatterLock, matter_client: MagicMock
    ) -> None:
        """Test _get_matter_client returns the client from the real integration."""
        client = matter_lock._get_matter_client()
        assert client is matter_client

    def test_setup_push_subscription_success(
        self, matter_lock: MatterLock, matter_client: MagicMock
    ) -> None:
        """Test setup_push_subscription subscribes with correct node ID."""
        matter_lock.setup_push_subscription()
        assert matter_lock._event_unsub is not None
        # Find LCM's subscription by its callback method name
        lcm_calls = [
            call
            for call in matter_client.subscribe_events.call_args_list
            if call.kwargs.get("event_filter") == EventType.NODE_EVENT
            and hasattr(call.kwargs.get("callback"), "__func__")
            and call.kwargs["callback"].__func__.__name__ == "_on_node_event"
        ]
        assert len(lcm_calls) == 1
        assert lcm_calls[0].kwargs["node_filter"] == 16

    # -- Tests using the simple fixture (no integration needed) --

    def test_get_matter_client_bad_adapter(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """Test _get_matter_client returns None when adapter has no matter_client."""
        mock_entry_data = MagicMock(spec=[])  # no attributes
        hass.data["matter"] = {"entry_id": mock_entry_data}
        assert matter_lock_simple._get_matter_client() is None

    def test_setup_push_no_node_id_raises(
        self,
        hass: HomeAssistant,
        matter_lock_simple: MatterLock,
    ) -> None:
        """Test setup_push_subscription raises when node ID is None."""
        mock_client = MagicMock()
        mock_adapter = MagicMock()
        mock_adapter.matter_client = mock_client
        mock_entry_data = MagicMock()
        mock_entry_data.adapter = mock_adapter
        hass.data["matter"] = {"entry_id": mock_entry_data}
        matter_lock_simple.device_entry = None  # no node ID

        with pytest.raises(LockDisconnected):
            matter_lock_simple.setup_push_subscription()

        assert matter_lock_simple._event_unsub is None
        mock_client.subscribe_events.assert_not_called()


# =============================================================================
# LockUserChange event tests
# =============================================================================


class TestLockUserChangeEvent:
    """Test _handle_lock_user_change callback and coordinator push updates."""

    def test_pin_added_pushes_unknown(self, matter_lock_simple: MatterLock) -> None:
        """Adding a PIN credential pushes SlotCode.UNREADABLE_CODE to coordinator."""
        mock_coordinator = MagicMock()
        mock_coordinator.data = {3: SlotCode.EMPTY}
        matter_lock_simple.coordinator = mock_coordinator

        matter_lock_simple._on_node_event(
            None,
            _make_node_event(
                event_id=4,
                data={
                    "lockDataType": 6,  # PIN
                    "dataOperationType": 0,  # Add
                    "dataIndex": 3,
                },
            ),
        )

        mock_coordinator.push_update.assert_called_once_with(
            {3: SlotCode.UNREADABLE_CODE}
        )

    def test_pin_modified_pushes_unknown(self, matter_lock_simple: MatterLock) -> None:
        """Modifying a PIN credential pushes SlotCode.UNREADABLE_CODE to coordinator."""
        mock_coordinator = MagicMock()
        mock_coordinator.data = {5: SlotCode.UNREADABLE_CODE}
        matter_lock_simple.coordinator = mock_coordinator

        matter_lock_simple._on_node_event(
            None,
            _make_node_event(
                event_id=4,
                data={
                    "lockDataType": 6,  # PIN
                    "dataOperationType": 2,  # Modify
                    "dataIndex": 5,
                },
            ),
        )

        mock_coordinator.push_update.assert_called_once_with(
            {5: SlotCode.UNREADABLE_CODE}
        )

    def test_pin_cleared_pushes_empty(self, matter_lock_simple: MatterLock) -> None:
        """Clearing a PIN credential pushes SlotCode.EMPTY to coordinator."""
        mock_coordinator = MagicMock()
        mock_coordinator.data = {2: SlotCode.UNREADABLE_CODE}
        matter_lock_simple.coordinator = mock_coordinator

        matter_lock_simple._on_node_event(
            None,
            _make_node_event(
                event_id=4,
                data={
                    "lockDataType": 6,  # PIN
                    "dataOperationType": 1,  # Clear
                    "dataIndex": 2,
                },
            ),
        )

        mock_coordinator.push_update.assert_called_once_with({2: SlotCode.EMPTY})

    def test_non_pin_data_type_ignored(self, matter_lock_simple: MatterLock) -> None:
        """Non-PIN LockDataType (e.g. RFID=7) is ignored."""
        mock_coordinator = MagicMock()
        matter_lock_simple.coordinator = mock_coordinator

        matter_lock_simple._on_node_event(
            None,
            _make_node_event(
                event_id=4,
                data={
                    "lockDataType": 7,  # RFID, not PIN
                    "dataOperationType": 0,
                    "dataIndex": 1,
                },
            ),
        )

        mock_coordinator.push_update.assert_not_called()

    def test_missing_data_index_ignored(self, matter_lock_simple: MatterLock) -> None:
        """Event with no dataIndex is ignored."""
        mock_coordinator = MagicMock()
        matter_lock_simple.coordinator = mock_coordinator

        matter_lock_simple._on_node_event(
            None,
            _make_node_event(
                event_id=4,
                data={
                    "lockDataType": 6,  # PIN
                    "dataOperationType": 0,
                    # no dataIndex
                },
            ),
        )

        mock_coordinator.push_update.assert_not_called()

    def test_non_integer_data_index_ignored(
        self, matter_lock_simple: MatterLock
    ) -> None:
        """Non-integer dataIndex logs warning and is ignored."""
        mock_coordinator = MagicMock()
        matter_lock_simple.coordinator = mock_coordinator

        matter_lock_simple._on_node_event(
            None,
            _make_node_event(
                event_id=4,
                data={
                    "lockDataType": 6,  # PIN
                    "dataOperationType": 0,
                    "dataIndex": "not_a_number",
                },
            ),
        )

        mock_coordinator.push_update.assert_not_called()

    def test_unknown_operation_type_ignored(
        self, matter_lock_simple: MatterLock
    ) -> None:
        """Unknown DataOperationType is ignored."""
        mock_coordinator = MagicMock()
        matter_lock_simple.coordinator = mock_coordinator

        matter_lock_simple._on_node_event(
            None,
            _make_node_event(
                event_id=4,
                data={
                    "lockDataType": 6,  # PIN
                    "dataOperationType": 99,  # unknown
                    "dataIndex": 1,
                },
            ),
        )

        mock_coordinator.push_update.assert_not_called()

    def test_no_coordinator_does_not_crash(
        self, matter_lock_simple: MatterLock
    ) -> None:
        """LockUserChange with no coordinator attached does not crash."""
        matter_lock_simple.coordinator = None

        matter_lock_simple._on_node_event(
            None,
            _make_node_event(
                event_id=4,
                data={
                    "lockDataType": 6,
                    "dataOperationType": 0,
                    "dataIndex": 1,
                },
            ),
        )
        # No assert — just verifying it doesn't raise

    def test_wrong_cluster_ignored(self, matter_lock_simple: MatterLock) -> None:
        """Event from non-DoorLock cluster is ignored."""
        mock_coordinator = MagicMock()
        matter_lock_simple.coordinator = mock_coordinator

        matter_lock_simple._on_node_event(
            None,
            _make_node_event(
                cluster_id=999,  # not DoorLock
                event_id=4,
                data={
                    "lockDataType": 6,
                    "dataOperationType": 0,
                    "dataIndex": 1,
                },
            ),
        )

        mock_coordinator.push_update.assert_not_called()


# =============================================================================
# Optimistic push update tests (set/clear usercode)
# =============================================================================


class TestOptimisticPushUpdates:
    """Test that set/clear usercode pushes to coordinator optimistically."""

    async def test_set_usercode_pushes_unknown(
        self,
        hass: HomeAssistant,
        matter_lock_simple: MatterLock,
    ) -> None:
        """async_set_usercode pushes SlotCode.UNREADABLE_CODE after helper call."""
        mock_coordinator = MagicMock()
        matter_lock_simple.coordinator = mock_coordinator

        mock_set_credential = AsyncMock(return_value={})
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
        ):
            result = await matter_lock_simple.async_set_usercode(3, "1234")

        assert result is True
        mock_coordinator.push_update.assert_called_once_with(
            {3: SlotCode.UNREADABLE_CODE}
        )

    async def test_set_usercode_no_coordinator(
        self,
        hass: HomeAssistant,
        matter_lock_simple: MatterLock,
    ) -> None:
        """async_set_usercode without coordinator does not crash."""
        matter_lock_simple.coordinator = None

        mock_set_credential = AsyncMock(return_value={})
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
        ):
            result = await matter_lock_simple.async_set_usercode(3, "1234")
        assert result is True

    async def test_clear_usercode_pushes_empty(
        self,
        hass: HomeAssistant,
        matter_lock_simple: MatterLock,
    ) -> None:
        """async_clear_usercode pushes SlotCode.EMPTY after clearing."""
        mock_coordinator = MagicMock()
        matter_lock_simple.coordinator = mock_coordinator

        mock_get_status = AsyncMock(return_value={"credential_exists": True})
        mock_clear = AsyncMock(return_value={})
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.get_lock_credential_status", mock_get_status),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
        ):
            result = await matter_lock_simple.async_clear_usercode(5)

        assert result is True
        mock_coordinator.push_update.assert_called_once_with({5: SlotCode.EMPTY})

    async def test_clear_empty_slot_no_push(
        self,
        hass: HomeAssistant,
        matter_lock_simple: MatterLock,
    ) -> None:
        """async_clear_usercode on empty slot does not push to coordinator."""
        mock_coordinator = MagicMock()
        matter_lock_simple.coordinator = mock_coordinator

        mock_get_status = AsyncMock(return_value={"credential_exists": False})
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.get_lock_credential_status", mock_get_status),
        ):
            result = await matter_lock_simple.async_clear_usercode(5)

        assert result is False
        mock_coordinator.push_update.assert_not_called()

    async def test_set_usercode_failure_no_push(
        self,
        hass: HomeAssistant,
        matter_lock_simple: MatterLock,
    ) -> None:
        """async_set_usercode does not push when helper call fails."""
        mock_coordinator = MagicMock()
        matter_lock_simple.coordinator = mock_coordinator

        mock_set_credential = AsyncMock(side_effect=HomeAssistantError("timeout"))
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
        ):
            with pytest.raises(LockDisconnected):
                await matter_lock_simple.async_set_usercode(3, "1234")

        mock_coordinator.push_update.assert_not_called()

    async def test_clear_usercode_failure_no_push(
        self,
        hass: HomeAssistant,
        matter_lock_simple: MatterLock,
    ) -> None:
        """async_clear_usercode does not push when clear helper fails."""
        mock_coordinator = MagicMock()
        matter_lock_simple.coordinator = mock_coordinator

        mock_get_status = AsyncMock(return_value={"credential_exists": True})
        mock_clear = AsyncMock(side_effect=HomeAssistantError("timeout"))
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.get_lock_credential_status", mock_get_status),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
        ):
            with pytest.raises(LockDisconnected):
                await matter_lock_simple.async_clear_usercode(5)

        mock_coordinator.push_update.assert_not_called()
