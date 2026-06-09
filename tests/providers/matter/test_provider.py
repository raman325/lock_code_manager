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

from custom_components.lock_code_manager.domain.credentials import (
    Credential,
    CredentialRef,
    CredentialType,
    LockCapabilities,
    SetUserResult,
    User,
)
from custom_components.lock_code_manager.domain.exceptions import (
    CodeRejectedError,
    DuplicateCodeError,
    LockCodeManagerError,
    LockCodeManagerProviderError,
    LockDisconnected,
    LockOperationFailed,
)
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers.matter import (
    MatterLock,
    SetCredentialFailedError,
)
from tests.providers.helpers import ServiceProviderConnectionTests

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
        pytest.raises(LockCodeManagerError, match="does not support user management"),
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
        pytest.raises(LockCodeManagerError, match="does not support PIN credentials"),
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

    assert codes[1] is SlotCredential.empty()
    assert codes[2] is SlotCredential.empty()


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
    """Test get_usercodes includes unmanaged occupied slots as UNREADABLE_CODE."""
    mock_get_lock_users = AsyncMock(
        return_value={
            "users": [
                {
                    "user_index": 5,
                    "credentials": [{"type": "pin", "index": 5}],
                },
                {
                    "user_index": 8,
                    "credentials": [{"type": "pin", "index": 8}],
                },
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
    assert codes == {5: SlotCredential.unreadable(), 8: SlotCredential.unreadable()}


async def test_get_usercodes_none_credential_index_skipped(
    hass: HomeAssistant,
    matter_lock_simple: MatterLock,
) -> None:
    """Test that PIN credentials with a None index are skipped."""
    mock_get_lock_users = AsyncMock(
        return_value={
            "users": [
                {
                    "user_index": 4,
                    "credentials": [
                        {
                            "type": "pin"
                        },  # no "index" key → filtered out by async_get_users
                        {"type": "pin", "index": 4},
                    ],
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
    assert codes == {4: SlotCredential.unreadable()}


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

    assert codes[1] is SlotCredential.empty()
    assert codes[2] is SlotCredential.unreadable()


# ---------------------------------------------------------------------------
# Error tests
# ---------------------------------------------------------------------------


async def test_get_usercodes_client_unavailable(
    hass: HomeAssistant, matter_lock_simple: MatterLock
) -> None:
    """Test async_get_usercodes raises LockDisconnected when client/node unavailable."""
    with patch.object(matter_lock_simple, "_get_matter_client", return_value=None):
        with pytest.raises(LockDisconnected, match="client or node unavailable"):
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
    assert codes[1] is SlotCredential.empty()
    # Slot 2 has a PIN credential
    assert codes[2] is SlotCredential.unreadable()


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

    def test_get_matter_node_no_device(self, matter_lock_simple: MatterLock) -> None:
        """Test node resolution returns None when no device entry."""
        matter_lock_simple.device_entry = None
        assert matter_lock_simple._get_matter_node() is None

    def test_get_matter_client_no_data(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """Test _get_matter_client returns None when no Matter data."""
        hass.data.pop("matter", None)
        assert matter_lock_simple._get_matter_client() is None

    def test_setup_push_idempotent(self, matter_lock_simple: MatterLock) -> None:
        """Test setup_push_subscription is a no-op if already subscribed."""
        matter_lock_simple._push_unsubs.append(lambda: None)  # already subscribed
        matter_lock_simple.setup_push_subscription()  # should be a no-op
        # If it tried to subscribe again, it would fail (no client)
        assert matter_lock_simple._push_unsubs

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

        matter_lock_simple._push_unsubs.append(_unsub)
        matter_lock_simple.teardown_push_subscription()
        assert unsub_called[0]
        assert not matter_lock_simple._push_unsubs

    def test_teardown_push_no_subscription(
        self, matter_lock_simple: MatterLock
    ) -> None:
        """Test teardown_push_subscription handles no active subscription."""
        assert not matter_lock_simple._push_unsubs
        matter_lock_simple.teardown_push_subscription()  # should not crash

    # -- Tests using the full Matter integration fixture --

    def test_get_matter_node_resolves(self, matter_lock: MatterLock) -> None:
        """Test node resolves from real Matter integration device."""
        node = matter_lock._get_matter_node()
        assert node is not None
        assert node.node_id == 16  # from mock_door_lock.json

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
        assert matter_lock._push_unsubs
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

        assert not matter_lock_simple._push_unsubs
        mock_client.subscribe_events.assert_not_called()


# =============================================================================
# LockUserChange event tests
# =============================================================================


class TestLockUserChangeEvent:
    """Test _handle_lock_user_change callback and coordinator push updates."""

    def test_pin_added_pushes_unknown(self, matter_lock_simple: MatterLock) -> None:
        """Adding a PIN credential pushes SlotCredential.unreadable() to coordinator."""
        mock_coordinator = MagicMock()
        mock_coordinator.data = {3: SlotCredential.empty()}
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
            {3: SlotCredential.unreadable()}
        )

    def test_pin_modified_pushes_unknown(self, matter_lock_simple: MatterLock) -> None:
        """Modifying a PIN credential pushes SlotCredential.unreadable() to coordinator."""
        mock_coordinator = MagicMock()
        mock_coordinator.data = {5: SlotCredential.unreadable()}
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
            {5: SlotCredential.unreadable()}
        )

    def test_pin_cleared_pushes_empty(self, matter_lock_simple: MatterLock) -> None:
        """Clearing a PIN credential pushes SlotCredential.empty() to coordinator."""
        mock_coordinator = MagicMock()
        mock_coordinator.data = {2: SlotCredential.unreadable()}
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

        mock_coordinator.push_update.assert_called_once_with(
            {2: SlotCredential.empty()}
        )

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
# supports_native_users tests
# =============================================================================


async def test_supports_native_users(matter_lock_simple: MatterLock) -> None:
    """Test that Matter locks report native user support."""
    assert matter_lock_simple.supports_native_users is True


# =============================================================================
# async_get_users tests
# =============================================================================


class TestGetUsers:
    """Test async_get_users projects lock users onto User domain objects."""

    async def test_get_users_empty(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """Returns empty list when lock has no users."""
        mock_get_lock_users = AsyncMock(return_value={"max_users": 10, "users": []})
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users),
        ):
            users = await matter_lock_simple.async_get_users()
        assert users == []

    async def test_get_users_pin_credentials_become_unreadable(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """Each PIN credential becomes a Credential with unreadable state."""
        mock_get_lock_users = AsyncMock(
            return_value={
                "max_users": 10,
                "users": [
                    {
                        "user_index": 1,
                        "user_name": "Alice",
                        "credentials": [{"type": "pin", "index": 1}],
                    }
                ],
            }
        )
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users),
        ):
            users = await matter_lock_simple.async_get_users()

        assert len(users) == 1
        user = users[0]
        assert user.user_id == 1
        assert user.name == "Alice"
        assert user.active is True
        assert len(user.credentials) == 1
        cred = user.credentials[0]
        assert cred.type is CredentialType.PIN
        assert cred.slot == 1
        assert cred.state is SlotCredential.unreadable()

    async def test_get_users_non_pin_credentials_excluded(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """Non-PIN credentials (RFID etc.) are not included in the user's credentials."""
        mock_get_lock_users = AsyncMock(
            return_value={
                "max_users": 10,
                "users": [
                    {
                        "user_index": 2,
                        "user_name": "Bob",
                        "credentials": [
                            {"type": "rfid", "index": 2},
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
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users),
        ):
            users = await matter_lock_simple.async_get_users()

        assert len(users) == 1
        user = users[0]
        assert len(user.credentials) == 1
        assert user.credentials[0].type is CredentialType.PIN

    async def test_get_users_multiple_users(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """Multiple lock users produce multiple User objects."""
        mock_get_lock_users = AsyncMock(
            return_value={
                "max_users": 10,
                "users": [
                    {
                        "user_index": 1,
                        "user_name": "Alice",
                        "credentials": [{"type": "pin", "index": 1}],
                    },
                    {
                        "user_index": 3,
                        "user_name": "Charlie",
                        "credentials": [{"type": "pin", "index": 3}],
                    },
                ],
            }
        )
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users),
        ):
            users = await matter_lock_simple.async_get_users()

        assert len(users) == 2
        assert users[0].user_id == 1
        assert users[1].user_id == 3

    async def test_get_users_disconnected(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """HomeAssistantError from get_lock_users raises LockDisconnected."""
        mock_get_lock_users = AsyncMock(
            side_effect=HomeAssistantError("connection lost")
        )
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users),
            pytest.raises(LockDisconnected),
        ):
            await matter_lock_simple.async_get_users()


# =============================================================================
# async_get_capabilities tests
# =============================================================================


class TestGetCapabilities:
    """Test async_get_capabilities maps lock_info to LockCapabilities."""

    async def test_get_capabilities_full(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """All fields present produce a complete LockCapabilities."""
        mock_get_lock_info = AsyncMock(
            return_value={
                "supports_user_management": True,
                "supported_credential_types": ["pin"],
                "max_users": 20,
                "max_pin_users": 15,
                "min_pin_length": 4,
                "max_pin_length": 8,
            }
        )
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.get_lock_info", mock_get_lock_info),
        ):
            caps = await matter_lock_simple.async_get_capabilities()

        assert isinstance(caps, LockCapabilities)
        assert caps.supports_user_management is True
        assert caps.max_users == 20
        pin_cap = caps.capability_for(CredentialType.PIN)
        assert pin_cap is not None
        assert pin_cap.num_slots == 15
        assert pin_cap.min_length == 4
        assert pin_cap.max_length == 8
        assert pin_cap.supports_learn is False

    async def test_get_capabilities_none_fields_default_to_zero(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """None capacity fields default to 0 instead of raising."""
        mock_get_lock_info = AsyncMock(
            return_value={
                "supports_user_management": True,
                "supported_credential_types": ["pin"],
                "max_users": None,
                "max_pin_users": None,
                "min_pin_length": None,
                "max_pin_length": None,
            }
        )
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.get_lock_info", mock_get_lock_info),
        ):
            caps = await matter_lock_simple.async_get_capabilities()

        assert caps.max_users == 0
        pin_cap = caps.capability_for(CredentialType.PIN)
        assert pin_cap is not None
        assert pin_cap.num_slots == 0
        assert pin_cap.min_length == 0
        assert pin_cap.max_length == 0

    async def test_get_capabilities_no_pin_support_empty_types(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """Lock without PIN support has empty credential_types mapping."""
        mock_get_lock_info = AsyncMock(
            return_value={
                "supports_user_management": True,
                "supported_credential_types": ["rfid"],
                "max_users": 10,
                "max_pin_users": 0,
                "min_pin_length": None,
                "max_pin_length": None,
            }
        )
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.get_lock_info", mock_get_lock_info),
        ):
            caps = await matter_lock_simple.async_get_capabilities()

        assert caps.capability_for(CredentialType.PIN) is None
        assert not caps.supports(CredentialType.PIN)

    async def test_get_capabilities_disconnected(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """HomeAssistantError from get_lock_info raises LockDisconnected."""
        mock_get_lock_info = AsyncMock(side_effect=HomeAssistantError("offline"))
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.get_lock_info", mock_get_lock_info),
            pytest.raises(LockDisconnected),
        ):
            await matter_lock_simple.async_get_capabilities()


# =============================================================================
# async_set_user tests
# =============================================================================


class TestSetUser:
    """Test async_set_user creates or updates lock users."""

    async def test_set_user_creates_new_user(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """When credential slot is empty, set_user returns created=True."""
        mock_get_status = AsyncMock(return_value={"credential_exists": False})
        mock_set_user = AsyncMock(return_value={"user_index": 1})
        user = User(user_id=1, name="Alice")
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.get_lock_credential_status", mock_get_status),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", mock_set_user),
        ):
            result = await matter_lock_simple.async_set_user(user)

        assert isinstance(result, SetUserResult)
        assert result.user_id == 1
        assert result.created is True
        mock_set_user.assert_called_once()

    async def test_set_user_updates_existing_user(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """When credential slot is occupied, set_user updates that owner."""
        mock_get_status = AsyncMock(
            return_value={"credential_exists": True, "user_index": 2}
        )
        mock_set_user = AsyncMock(return_value={"user_index": 2})
        user = User(user_id=2, name="Bob")
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.get_lock_credential_status", mock_get_status),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", mock_set_user),
        ):
            result = await matter_lock_simple.async_set_user(user)

        assert result.created is False
        assert result.user_id == 2

    async def test_set_user_create_auto_allocates_index_and_passes_name(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """On create, set_lock_user is called with user_index=None (auto-allocate)."""
        mock_get_status = AsyncMock(return_value={"credential_exists": False})
        mock_set_user = AsyncMock(return_value={"user_index": 5})
        user = User(user_id=5, name="Eve")
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.get_lock_credential_status", mock_get_status),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", mock_set_user),
        ):
            result = await matter_lock_simple.async_set_user(user)

        call_kwargs = mock_set_user.call_args.kwargs
        assert call_kwargs["user_index"] is None  # Matter allocates the index
        assert call_kwargs["user_name"] == "Eve"
        assert result.user_id == 5  # the allocated index is returned

    async def test_set_user_name_none_preserved(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """name=None flows through unchanged (helper preserves the existing name)."""
        mock_get_status = AsyncMock(
            return_value={"credential_exists": True, "user_index": 7}
        )
        mock_set_user = AsyncMock(return_value={"user_index": 7})
        user = User(user_id=7, name=None)
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.get_lock_credential_status", mock_get_status),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", mock_set_user),
        ):
            await matter_lock_simple.async_set_user(user)

        assert mock_set_user.call_args.kwargs["user_name"] is None

    async def test_set_user_disconnected(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """HomeAssistantError from set_lock_user raises LockDisconnected."""
        mock_get_status = AsyncMock(return_value={"credential_exists": False})
        mock_set_user = AsyncMock(side_effect=HomeAssistantError("offline"))
        user = User(user_id=1, name="Alice")
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.get_lock_credential_status", mock_get_status),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", mock_set_user),
            pytest.raises(LockDisconnected),
        ):
            await matter_lock_simple.async_set_user(user)


# =============================================================================
# async_delete_user tests
# =============================================================================


class TestDeleteUser:
    """Test async_delete_user removes lock users."""

    async def test_delete_user_calls_clear_lock_user(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """clear_lock_user is called with the correct user_index."""
        mock_clear_user = AsyncMock(return_value=None)
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.clear_lock_user", mock_clear_user),
        ):
            await matter_lock_simple.async_delete_user(3)

        mock_clear_user.assert_called_once()
        # The first positional arg after client/node is user_index
        call_args = mock_clear_user.call_args
        assert 3 in call_args.args or call_args.kwargs.get("user_index") == 3

    async def test_delete_user_disconnected(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """HomeAssistantError from clear_lock_user raises LockDisconnected."""
        mock_clear_user = AsyncMock(side_effect=HomeAssistantError("offline"))
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.clear_lock_user", mock_clear_user),
            pytest.raises(LockDisconnected),
        ):
            await matter_lock_simple.async_delete_user(3)


# =============================================================================
# async_set_credential tests
# =============================================================================


class TestSetCredential:
    """Test async_set_credential writes Personal Identification Number credentials."""

    def _make_credential(self, slot: int = 1, pin: str = "1234") -> Credential:
        """Build a readable PIN credential for the given slot."""
        return Credential(
            type=CredentialType.PIN,
            slot=slot,
            state=SlotCredential.known(pin),
        )

    async def test_set_credential_success_returns_true(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """Successful set_lock_credential returns True."""
        mock_set_credential = AsyncMock(
            return_value={"credential_index": 1, "user_index": 1}
        )
        credential = self._make_credential(slot=1, pin="1234")
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
        ):
            result = await matter_lock_simple.async_set_credential(
                1, credential, name="Alice", source="direct"
            )
        assert result is True

    async def test_set_credential_pushes_unreadable_optimistically(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """async_set_credential pushes SlotCredential.unreadable() on success."""
        mock_coordinator = MagicMock()
        matter_lock_simple.coordinator = mock_coordinator
        mock_set_credential = AsyncMock(
            return_value={"credential_index": 2, "user_index": 2}
        )
        credential = self._make_credential(slot=2, pin="5678")
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
        ):
            await matter_lock_simple.async_set_credential(
                2, credential, name=None, source="direct"
            )

        mock_coordinator.push_update.assert_called_once_with(
            {2: SlotCredential.unreadable()}
        )

    async def test_set_credential_unreadable_pin_raises_code_rejected(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """A credential with no readable_pin raises CodeRejectedError immediately."""
        credential = Credential(
            type=CredentialType.PIN,
            slot=1,
            state=SlotCredential.unreadable(),
        )
        with pytest.raises(CodeRejectedError):
            await matter_lock_simple.async_set_credential(
                1, credential, name=None, source="direct"
            )

    async def test_set_credential_duplicate_direct_raises(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """Duplicate status from lock raises DuplicateCodeError for direct source."""
        mock_set_credential = AsyncMock(
            side_effect=_make_set_credential_failed_error("duplicate")
        )
        credential = self._make_credential(slot=1, pin="1234")
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
            pytest.raises(DuplicateCodeError),
        ):
            await matter_lock_simple.async_set_credential(
                1, credential, name=None, source="direct"
            )

    async def test_set_credential_duplicate_sync_retries_and_succeeds(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """Duplicate on sync source clears the slot and retries once."""
        mock_set_credential = AsyncMock(
            side_effect=[
                _make_set_credential_failed_error("duplicate"),
                {"credential_index": 1, "user_index": 1},
            ]
        )
        mock_clear = AsyncMock(return_value={})
        credential = self._make_credential(slot=1, pin="1234")
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
        ):
            result = await matter_lock_simple.async_set_credential(
                1, credential, name=None, source="sync"
            )

        assert result is True
        assert mock_set_credential.call_count == 2
        assert mock_clear.call_count == 1

    async def test_set_credential_duplicate_sync_persistent_raises(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """Persistent duplicate on sync source raises DuplicateCodeError after retry."""
        mock_set_credential = AsyncMock(
            side_effect=_make_set_credential_failed_error("duplicate")
        )
        mock_clear = AsyncMock(return_value={})
        credential = self._make_credential(slot=1, pin="1234")
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
            pytest.raises(DuplicateCodeError),
        ):
            await matter_lock_simple.async_set_credential(
                1, credential, name=None, source="sync"
            )

        assert mock_set_credential.call_count == 2
        assert mock_clear.call_count == 1

    async def test_set_credential_non_duplicate_failure_raises_code_rejected(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """Non-duplicate SetCredentialFailedError raises CodeRejectedError."""
        mock_set_credential = AsyncMock(
            side_effect=_make_set_credential_failed_error("occupied")
        )
        credential = self._make_credential(slot=1, pin="1234")
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
            pytest.raises(CodeRejectedError),
        ):
            await matter_lock_simple.async_set_credential(
                1, credential, name=None, source="direct"
            )

    async def test_set_credential_ha_error_raises_code_rejected(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """ServiceValidationError from helper raises CodeRejectedError."""
        mock_set_credential = AsyncMock(
            side_effect=ServiceValidationError("bad PIN length")
        )
        credential = self._make_credential(slot=1, pin="1")
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
            pytest.raises(CodeRejectedError),
        ):
            await matter_lock_simple.async_set_credential(
                1, credential, name=None, source="direct"
            )

    async def test_set_credential_transport_error_raises_lock_disconnected(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """A non-validation HomeAssistantError (transport) routes to retry path."""
        mock_set_credential = AsyncMock(side_effect=HomeAssistantError("endpoint gone"))
        credential = self._make_credential(slot=1, pin="1234")
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
            pytest.raises(LockDisconnected),
        ):
            await matter_lock_simple.async_set_credential(
                1, credential, name=None, source="direct"
            )

    async def test_set_credential_passes_correct_args(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """set_lock_credential receives credential_type='pin', data, index, user_index."""
        mock_set_credential = AsyncMock(
            return_value={"credential_index": 3, "user_index": 3}
        )
        credential = self._make_credential(slot=3, pin="9999")
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
        ):
            await matter_lock_simple.async_set_credential(
                3, credential, name="Carol", source="direct"
            )

        call_kwargs = mock_set_credential.call_args.kwargs
        assert call_kwargs["credential_type"] == "pin"
        assert call_kwargs["credential_data"] == "9999"
        assert call_kwargs["credential_index"] == 3
        assert call_kwargs["user_index"] == 3


# =============================================================================
# async_delete_credential tests
# =============================================================================


class TestDeleteCredential:
    """Test async_delete_credential clears Personal Identification Number credentials."""

    async def test_delete_credential_returns_true(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """Successful clear returns True."""
        mock_clear = AsyncMock(return_value=None)
        ref = CredentialRef(user_id=1, type=CredentialType.PIN, slot=1)
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
        ):
            result = await matter_lock_simple.async_delete_credential(ref)
        assert result is True

    async def test_delete_credential_pushes_empty(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """Successful delete pushes SlotCredential.empty() to coordinator."""
        mock_coordinator = MagicMock()
        matter_lock_simple.coordinator = mock_coordinator
        mock_clear = AsyncMock(return_value=None)
        ref = CredentialRef(user_id=4, type=CredentialType.PIN, slot=4)
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
        ):
            await matter_lock_simple.async_delete_credential(ref)

        mock_coordinator.push_update.assert_called_once_with(
            {4: SlotCredential.empty()}
        )

    async def test_delete_credential_passes_correct_args(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """clear_lock_credential receives credential_type='pin' and credential_index."""
        mock_clear = AsyncMock(return_value=None)
        ref = CredentialRef(user_id=7, type=CredentialType.PIN, slot=7)
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
        ):
            await matter_lock_simple.async_delete_credential(ref)

        call_kwargs = mock_clear.call_args.kwargs
        assert call_kwargs["credential_type"] == "pin"
        assert call_kwargs["credential_index"] == 7

    async def test_delete_credential_transport_error_raises_lock_disconnected(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """A transport HomeAssistantError routes to the retry path (LockDisconnected)."""
        mock_clear = AsyncMock(side_effect=HomeAssistantError("transport down"))
        ref = CredentialRef(user_id=1, type=CredentialType.PIN, slot=1)
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
            pytest.raises(LockDisconnected),
        ):
            await matter_lock_simple.async_delete_credential(ref)

    async def test_delete_credential_validation_error_raises_operation_failed(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """A ServiceValidationError is a reachable-but-failed operation."""
        mock_clear = AsyncMock(side_effect=ServiceValidationError("bad"))
        ref = CredentialRef(user_id=1, type=CredentialType.PIN, slot=1)
        with (
            patch.object(
                matter_lock_simple, "_get_matter_client", return_value=MagicMock()
            ),
            patch.object(
                matter_lock_simple, "_get_matter_node", return_value=MagicMock()
            ),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
            pytest.raises(LockOperationFailed),
        ):
            await matter_lock_simple.async_delete_credential(ref)
