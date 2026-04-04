"""Test the Matter lock provider."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from matter_server.common.models import MatterNodeEvent
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import (
    CONF_ENABLED,
    CONF_LOCKS,
    CONF_NAME,
    CONF_PIN,
    CONF_SLOTS,
    DOMAIN,
)
from custom_components.lock_code_manager.exceptions import (
    LockCodeManagerError,
    LockDisconnected,
)
from custom_components.lock_code_manager.models import SlotCode
from custom_components.lock_code_manager.providers.matter import (
    MATTER_DOMAIN,
    MatterLock,
)

from .service_provider_tests import (
    ServiceProviderConnectionTests,
    ServiceProviderDeviceAvailabilityTests,
    register_mock_service,
)

LOCK_ENTITY_ID = "lock.matter_test_matter_lock"


@pytest.fixture
async def matter_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create a Matter config entry."""
    entry = MockConfigEntry(domain=MATTER_DOMAIN)
    entry.add_to_hass(hass)
    entry._async_set_state(hass, entry.state, None)
    return entry


@pytest.fixture
async def matter_lock(
    hass: HomeAssistant, matter_config_entry: MockConfigEntry
) -> MatterLock:
    """Create a MatterLock instance with a registered lock entity."""
    entity_reg = er.async_get(hass)
    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "matter",
        "test_matter_lock",
        config_entry=matter_config_entry,
    )
    return MatterLock(
        hass,
        dr.async_get(hass),
        entity_reg,
        matter_config_entry,
        lock_entity,
    )


@pytest.fixture
async def lcm_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Create a Lock Code Manager config entry that manages slots 1 and 2."""
    config = {
        CONF_LOCKS: [LOCK_ENTITY_ID],
        CONF_SLOTS: {
            1: {CONF_NAME: "slot1", CONF_PIN: "1234", CONF_ENABLED: True},
            2: {CONF_NAME: "slot2", CONF_PIN: "5678", CONF_ENABLED: True},
        },
    }
    entry = MockConfigEntry(domain=DOMAIN, data=config, unique_id="test_matter_lcm")
    entry.add_to_hass(hass)
    return entry


# --- Alias fixtures for shared test mixins ---


@pytest.fixture
def provider_lock(matter_lock: MatterLock) -> MatterLock:
    """Alias matter_lock for shared test mixins."""
    return matter_lock


@pytest.fixture
def provider_config_entry(matter_config_entry: MockConfigEntry) -> MockConfigEntry:
    """Alias matter_config_entry for shared test mixins."""
    return matter_config_entry


@pytest.fixture
def provider_domain() -> str:
    """Return the provider integration domain."""
    return MATTER_DOMAIN


@pytest.fixture
def provider_lock_class() -> type[MatterLock]:
    """Return the provider lock class."""
    return MatterLock


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


async def test_domain_property(matter_lock: MatterLock) -> None:
    """Test that domain returns 'matter'."""
    assert matter_lock.domain == MATTER_DOMAIN


async def test_supports_code_slot_events(matter_lock: MatterLock) -> None:
    """Test that Matter locks support code slot events via LockOperation."""
    assert matter_lock.supports_code_slot_events is True


async def test_supports_push(matter_lock: MatterLock) -> None:
    """Test that Matter locks support push-based updates."""
    assert matter_lock.supports_push is True


async def test_usercode_scan_interval(matter_lock: MatterLock) -> None:
    """Test that scan interval is 5 minutes."""
    assert matter_lock.usercode_scan_interval == timedelta(minutes=5)


# ---------------------------------------------------------------------------
# Connection and availability tests (shared)
# ---------------------------------------------------------------------------


class TestConnection(ServiceProviderConnectionTests):
    """Connection tests for Matter provider using shared mixin."""


class TestDeviceAvailability(ServiceProviderDeviceAvailabilityTests):
    """Device availability tests for Matter provider using shared mixin."""

    availability_service = "get_lock_info"


# ---------------------------------------------------------------------------
# Setup tests
# ---------------------------------------------------------------------------


async def test_setup(
    hass: HomeAssistant,
    matter_lock: MatterLock,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """Test that setup validates lock supports user management and PIN credentials."""
    mock_response = {
        LOCK_ENTITY_ID: {
            "supports_user_management": True,
            "supported_credential_types": ["pin"],
        },
    }
    handler = AsyncMock(return_value=mock_response)
    register_mock_service(hass, MATTER_DOMAIN, "get_lock_info", handler)

    await matter_lock.async_setup(lcm_config_entry)
    assert handler.call_count == 1


async def test_setup_unsupported_lock(
    hass: HomeAssistant,
    matter_lock: MatterLock,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """Test that setup raises when lock does not support user management."""
    mock_response = {
        LOCK_ENTITY_ID: {
            "supports_user_management": False,
        },
    }
    handler = AsyncMock(return_value=mock_response)
    register_mock_service(hass, MATTER_DOMAIN, "get_lock_info", handler)

    with pytest.raises(LockCodeManagerError, match="does not support user management"):
        await matter_lock.async_setup(lcm_config_entry)


async def test_setup_no_pin_support(
    hass: HomeAssistant,
    matter_lock: MatterLock,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """Test that setup raises when lock supports users but not PIN credentials."""
    mock_response = {
        LOCK_ENTITY_ID: {
            "supports_user_management": True,
            "supported_credential_types": ["rfid"],
        },
    }
    handler = AsyncMock(return_value=mock_response)
    register_mock_service(hass, MATTER_DOMAIN, "get_lock_info", handler)

    with pytest.raises(LockCodeManagerError, match="does not support PIN credentials"):
        await matter_lock.async_setup(lcm_config_entry)


# ---------------------------------------------------------------------------
# get_usercodes tests
# ---------------------------------------------------------------------------


async def test_get_usercodes(
    hass: HomeAssistant,
    matter_lock: MatterLock,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """Test get_usercodes returns UNKNOWN for occupied, EMPTY for cleared slots."""
    mock_response = {
        LOCK_ENTITY_ID: {
            "max_users": 10,
            "users": [
                {
                    "user_index": 1,
                    "user_name": "slot1",
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
    handler = AsyncMock(return_value=mock_response)
    register_mock_service(hass, MATTER_DOMAIN, "get_lock_users", handler)

    codes = await matter_lock.async_get_usercodes()

    assert codes[1] is SlotCode.UNKNOWN
    assert codes[2] is SlotCode.EMPTY


async def test_get_usercodes_no_users(
    hass: HomeAssistant,
    matter_lock: MatterLock,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """Test get_usercodes when no users exist on the lock."""
    mock_response = {
        LOCK_ENTITY_ID: {
            "max_users": 10,
            "users": [],
        },
    }
    handler = AsyncMock(return_value=mock_response)
    register_mock_service(hass, MATTER_DOMAIN, "get_lock_users", handler)

    codes = await matter_lock.async_get_usercodes()

    assert codes[1] is SlotCode.EMPTY
    assert codes[2] is SlotCode.EMPTY


async def test_get_usercodes_no_configured_slots(
    hass: HomeAssistant,
    matter_lock: MatterLock,
) -> None:
    """Test get_usercodes returns empty dict when no slots are configured."""
    codes = await matter_lock.async_get_usercodes()
    assert codes == {}


# ---------------------------------------------------------------------------
# set_usercode tests
# ---------------------------------------------------------------------------


async def test_set_usercode(hass: HomeAssistant, matter_lock: MatterLock) -> None:
    """Test set_usercode calls the correct Matter services."""
    calls: list[dict[str, Any]] = []

    async def _capture_call(call):
        calls.append({"service": call.service, "data": dict(call.data)})
        return {LOCK_ENTITY_ID: {}}

    register_mock_service(
        hass, MATTER_DOMAIN, "set_lock_credential", AsyncMock(side_effect=_capture_call)
    )
    register_mock_service(
        hass, MATTER_DOMAIN, "set_lock_user", AsyncMock(side_effect=_capture_call)
    )

    result = await matter_lock.async_set_usercode(1, "1234", "User One")

    assert result is True
    assert len(calls) == 2
    # First call: set_lock_credential
    assert calls[0]["service"] == "set_lock_credential"
    assert calls[0]["data"]["credential_type"] == "pin"
    assert calls[0]["data"]["credential_data"] == "1234"
    assert calls[0]["data"]["credential_index"] == 1
    # Second call: set_lock_user
    assert calls[1]["service"] == "set_lock_user"
    assert calls[1]["data"]["user_name"] == "User One"


async def test_set_usercode_no_name(
    hass: HomeAssistant, matter_lock: MatterLock
) -> None:
    """Test set_usercode without a name only calls set_lock_credential."""
    calls: list[str] = []

    async def _capture_call(call):
        calls.append(call.service)
        return {LOCK_ENTITY_ID: {}}

    register_mock_service(
        hass, MATTER_DOMAIN, "set_lock_credential", AsyncMock(side_effect=_capture_call)
    )
    register_mock_service(
        hass, MATTER_DOMAIN, "set_lock_user", AsyncMock(side_effect=_capture_call)
    )

    result = await matter_lock.async_set_usercode(3, "9999")

    assert result is True
    assert calls == ["set_lock_credential"]


# ---------------------------------------------------------------------------
# clear_usercode tests
# ---------------------------------------------------------------------------


async def test_clear_usercode(hass: HomeAssistant, matter_lock: MatterLock) -> None:
    """Test clear_usercode calls clear_lock_credential when credential exists."""
    credential_status_response = {
        LOCK_ENTITY_ID: {"credential_exists": True},
    }
    clear_response = {LOCK_ENTITY_ID: {}}

    handler_status = AsyncMock(return_value=credential_status_response)
    handler_clear = AsyncMock(return_value=clear_response)
    register_mock_service(
        hass, MATTER_DOMAIN, "get_lock_credential_status", handler_status
    )
    register_mock_service(hass, MATTER_DOMAIN, "clear_lock_credential", handler_clear)

    result = await matter_lock.async_clear_usercode(1)

    assert result is True
    assert handler_status.call_count == 1
    assert handler_clear.call_count == 1


async def test_clear_usercode_already_empty(
    hass: HomeAssistant, matter_lock: MatterLock
) -> None:
    """Test clear_usercode returns False when the credential does not exist."""
    credential_status_response = {
        LOCK_ENTITY_ID: {"credential_exists": False},
    }
    handler_status = AsyncMock(return_value=credential_status_response)
    handler_clear = AsyncMock(return_value={LOCK_ENTITY_ID: {}})
    register_mock_service(
        hass, MATTER_DOMAIN, "get_lock_credential_status", handler_status
    )
    register_mock_service(hass, MATTER_DOMAIN, "clear_lock_credential", handler_clear)

    result = await matter_lock.async_clear_usercode(2)

    assert result is False
    # Only the credential status check should have been called
    assert handler_status.call_count == 1
    assert handler_clear.call_count == 0


# ---------------------------------------------------------------------------
# hard_refresh_codes tests
# ---------------------------------------------------------------------------


async def test_hard_refresh_codes(
    hass: HomeAssistant,
    matter_lock: MatterLock,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """Test hard_refresh_codes returns same result as get_usercodes."""
    mock_response = {
        LOCK_ENTITY_ID: {
            "max_users": 10,
            "users": [
                {
                    "user_index": 1,
                    "credentials": [
                        {"credential_type": "pin", "credential_index": 2},
                    ],
                }
            ],
        },
    }
    handler = AsyncMock(return_value=mock_response)
    register_mock_service(hass, MATTER_DOMAIN, "get_lock_users", handler)

    codes = await matter_lock.async_hard_refresh_codes()

    assert codes[1] is SlotCode.EMPTY
    assert codes[2] is SlotCode.UNKNOWN


# ---------------------------------------------------------------------------
# Service error tests
# ---------------------------------------------------------------------------


async def test_service_call_failure_raises_lock_disconnected(
    hass: HomeAssistant, matter_lock: MatterLock
) -> None:
    """Test that Matter service failures raise LockDisconnected."""
    handler = AsyncMock(side_effect=HomeAssistantError("connection lost"))
    register_mock_service(hass, MATTER_DOMAIN, "set_lock_credential", handler)

    with pytest.raises(LockDisconnected, match="connection lost"):
        await matter_lock.async_set_usercode(1, "1234")


async def test_get_usercodes_multiple_credential_types(
    hass: HomeAssistant,
    matter_lock: MatterLock,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """Test that only PIN credentials are considered, not other types like RFID."""
    mock_response = {
        LOCK_ENTITY_ID: {
            "max_users": 10,
            "users": [
                {
                    "user_index": 1,
                    "credentials": [
                        {"credential_type": "rfid", "credential_index": 1},
                        {"credential_type": "pin", "credential_index": 2},
                    ],
                },
            ],
        },
    }
    handler = AsyncMock(return_value=mock_response)
    register_mock_service(hass, MATTER_DOMAIN, "get_lock_users", handler)

    codes = await matter_lock.async_get_usercodes()

    # Slot 1 has only RFID credential, not PIN
    assert codes[1] is SlotCode.EMPTY
    # Slot 2 has a PIN credential
    assert codes[2] is SlotCode.UNKNOWN


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

    def test_unlock_with_pin_credential(self, matter_lock: MatterLock) -> None:
        """Unlock with PIN credential fires code slot event."""
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        matter_lock._on_node_event(
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

    def test_lock_with_pin_credential(self, matter_lock: MatterLock) -> None:
        """Lock with PIN credential fires code slot event."""
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        matter_lock._on_node_event(
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

    def test_rfid_credential_ignored(self, matter_lock: MatterLock) -> None:
        """RFID credential does not fire pin_used event."""
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        matter_lock._on_node_event(
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

    def test_fingerprint_credential_ignored(self, matter_lock: MatterLock) -> None:
        """Fingerprint credential does not fire pin_used event."""
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        matter_lock._on_node_event(
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

    def test_wrong_cluster_ignored(self, matter_lock: MatterLock) -> None:
        """Events from non-DoorLock clusters are ignored."""
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        matter_lock._on_node_event(
            None,
            _make_node_event(
                cluster_id=6,  # OnOff cluster
                data={"credentials": [{"credentialType": 1, "credentialIndex": 1}]},
            ),
        )

        assert len(fired) == 0

    def test_wrong_event_id_ignored(self, matter_lock: MatterLock) -> None:
        """Non-LockOperation DoorLock events are ignored."""
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        matter_lock._on_node_event(
            None,
            _make_node_event(
                event_id=3,  # LockOperationError
                data={"credentials": [{"credentialType": 1, "credentialIndex": 1}]},
            ),
        )

        assert len(fired) == 0

    def test_no_credentials_ignored(self, matter_lock: MatterLock) -> None:
        """Event without credentials is ignored."""
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        matter_lock._on_node_event(
            None,
            _make_node_event(data={"lockOperationType": 1}),
        )

        assert len(fired) == 0

    def test_empty_credentials_ignored(self, matter_lock: MatterLock) -> None:
        """Event with empty credentials list is ignored."""
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        matter_lock._on_node_event(
            None,
            _make_node_event(data={"lockOperationType": 1, "credentials": []}),
        )

        assert len(fired) == 0

    def test_no_operation_type(self, matter_lock: MatterLock) -> None:
        """Event without lockOperationType fires with to_locked=None."""
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        matter_lock._on_node_event(
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

    def test_none_data_ignored(self, matter_lock: MatterLock) -> None:
        """Event with None data is ignored (no credentials)."""
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        matter_lock._on_node_event(None, _make_node_event(data=None))

        assert len(fired) == 0


# =============================================================================
# Event subscription lifecycle tests
# =============================================================================


class TestEventSubscription:
    """Test event subscription setup and teardown."""

    def test_matter_node_id_from_device_registry(
        self,
        hass: HomeAssistant,
        matter_lock: MatterLock,
        matter_config_entry: MockConfigEntry,
    ) -> None:
        """Test _matter_node_id resolves from device identifiers."""
        dev_reg = dr.async_get(hass)
        dev_reg.async_get_or_create(
            config_entry_id=matter_config_entry.entry_id,
            identifiers={("matter", "42")},
        )
        # Update the lock's device_entry
        matter_lock._dev_reg = dev_reg
        device = dev_reg.async_get_or_create(
            config_entry_id=matter_config_entry.entry_id,
            identifiers={("matter", "42")},
        )
        matter_lock.device_entry = device

        assert matter_lock._matter_node_id == 42

    def test_matter_node_id_no_device(self, matter_lock: MatterLock) -> None:
        """Test _matter_node_id returns None when no device entry."""
        matter_lock.device_entry = None
        assert matter_lock._matter_node_id is None

    def test_get_matter_client_no_data(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Test _get_matter_client returns None when no Matter data."""
        hass.data.pop("matter", None)
        assert matter_lock._get_matter_client() is None

    def test_setup_push_idempotent(self, matter_lock: MatterLock) -> None:
        """Test setup_push_subscription is a no-op if already subscribed."""
        matter_lock._event_unsub = lambda: None  # already subscribed
        matter_lock.setup_push_subscription()  # should be a no-op
        # If it tried to subscribe again, it would fail (no client)
        assert matter_lock._event_unsub is not None

    def test_setup_push_no_client_raises(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Test setup_push_subscription raises when client unavailable."""
        hass.data.pop("matter", None)
        with pytest.raises(LockDisconnected):
            matter_lock.setup_push_subscription()

    def test_teardown_push_unsubscribes(self, matter_lock: MatterLock) -> None:
        """Test teardown_push_subscription cleans up event subscription."""
        unsub_called = [False]

        def _unsub() -> None:
            unsub_called[0] = True

        matter_lock._event_unsub = _unsub
        matter_lock.teardown_push_subscription()
        assert unsub_called[0]
        assert matter_lock._event_unsub is None

    def test_teardown_push_no_subscription(self, matter_lock: MatterLock) -> None:
        """Test teardown_push_subscription handles no active subscription."""
        matter_lock._event_unsub = None
        matter_lock.teardown_push_subscription()  # should not crash

    def test_get_matter_client_success(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Test _get_matter_client returns client from hass.data."""
        mock_client = MagicMock()
        mock_adapter = MagicMock()
        mock_adapter.matter_client = mock_client
        mock_entry_data = MagicMock()
        mock_entry_data.adapter = mock_adapter
        hass.data["matter"] = {"entry_id": mock_entry_data}
        assert matter_lock._get_matter_client() is mock_client

    def test_setup_push_success(
        self,
        hass: HomeAssistant,
        matter_lock: MatterLock,
        matter_config_entry: MockConfigEntry,
    ) -> None:
        """Test setup_push_subscription subscribes when client and node available."""
        mock_unsub = MagicMock()
        mock_client = MagicMock()
        mock_client.subscribe_events.return_value = mock_unsub
        mock_adapter = MagicMock()
        mock_adapter.matter_client = mock_client
        mock_entry_data = MagicMock()
        mock_entry_data.adapter = mock_adapter
        hass.data["matter"] = {"entry_id": mock_entry_data}

        dev_reg = dr.async_get(hass)
        device = dev_reg.async_get_or_create(
            config_entry_id=matter_config_entry.entry_id,
            identifiers={("matter", "16")},
        )
        matter_lock.device_entry = device

        matter_lock.setup_push_subscription()

        assert matter_lock._event_unsub is mock_unsub
        mock_client.subscribe_events.assert_called_once()

    def test_matter_node_id_invalid_identifier(
        self,
        hass: HomeAssistant,
        matter_lock: MatterLock,
        matter_config_entry: MockConfigEntry,
    ) -> None:
        """Test _matter_node_id returns None for non-numeric identifier."""
        dev_reg = dr.async_get(hass)
        device = dev_reg.async_get_or_create(
            config_entry_id=matter_config_entry.entry_id,
            identifiers={("matter", "not_a_number")},
        )
        matter_lock.device_entry = device
        assert matter_lock._matter_node_id is None

    def test_get_matter_client_empty_data(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Test _get_matter_client returns None for empty matter data dict."""
        hass.data["matter"] = {}
        assert matter_lock._get_matter_client() is None

    def test_get_matter_client_bad_adapter(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Test _get_matter_client returns None when adapter has no matter_client."""
        mock_entry_data = MagicMock(spec=[])  # no attributes
        hass.data["matter"] = {"entry_id": mock_entry_data}
        assert matter_lock._get_matter_client() is None

    def test_setup_push_no_node_id_raises(
        self,
        hass: HomeAssistant,
        matter_lock: MatterLock,
    ) -> None:
        """Test setup_push_subscription raises when node ID is None."""
        mock_client = MagicMock()
        mock_adapter = MagicMock()
        mock_adapter.matter_client = mock_client
        mock_entry_data = MagicMock()
        mock_entry_data.adapter = mock_adapter
        hass.data["matter"] = {"entry_id": mock_entry_data}
        matter_lock.device_entry = None  # no node ID

        with pytest.raises(LockDisconnected):
            matter_lock.setup_push_subscription()

        assert matter_lock._event_unsub is None
        mock_client.subscribe_events.assert_not_called()


# =============================================================================
# LockUserChange event tests
# =============================================================================


class TestLockUserChangeEvent:
    """Test _handle_lock_user_change callback and coordinator push updates."""

    def test_pin_added_pushes_unknown(self, matter_lock: MatterLock) -> None:
        """Adding a PIN credential pushes SlotCode.UNKNOWN to coordinator."""
        mock_coordinator = MagicMock()
        mock_coordinator.data = {3: SlotCode.EMPTY}
        matter_lock.coordinator = mock_coordinator

        matter_lock._on_node_event(
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

        mock_coordinator.push_update.assert_called_once_with({3: SlotCode.UNKNOWN})

    def test_pin_modified_pushes_unknown(self, matter_lock: MatterLock) -> None:
        """Modifying a PIN credential pushes SlotCode.UNKNOWN to coordinator."""
        mock_coordinator = MagicMock()
        mock_coordinator.data = {5: SlotCode.UNKNOWN}
        matter_lock.coordinator = mock_coordinator

        matter_lock._on_node_event(
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

        mock_coordinator.push_update.assert_called_once_with({5: SlotCode.UNKNOWN})

    def test_pin_cleared_pushes_empty(self, matter_lock: MatterLock) -> None:
        """Clearing a PIN credential pushes SlotCode.EMPTY to coordinator."""
        mock_coordinator = MagicMock()
        mock_coordinator.data = {2: SlotCode.UNKNOWN}
        matter_lock.coordinator = mock_coordinator

        matter_lock._on_node_event(
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

    def test_non_pin_data_type_ignored(self, matter_lock: MatterLock) -> None:
        """Non-PIN LockDataType (e.g. RFID=7) is ignored."""
        mock_coordinator = MagicMock()
        matter_lock.coordinator = mock_coordinator

        matter_lock._on_node_event(
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

    def test_missing_data_index_ignored(self, matter_lock: MatterLock) -> None:
        """Event with no dataIndex is ignored."""
        mock_coordinator = MagicMock()
        matter_lock.coordinator = mock_coordinator

        matter_lock._on_node_event(
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

    def test_non_integer_data_index_ignored(self, matter_lock: MatterLock) -> None:
        """Non-integer dataIndex logs warning and is ignored."""
        mock_coordinator = MagicMock()
        matter_lock.coordinator = mock_coordinator

        matter_lock._on_node_event(
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

    def test_coordinator_data_none_no_push(self, matter_lock: MatterLock) -> None:
        """LockUserChange skips push when coordinator.data is None."""
        mock_coordinator = MagicMock()
        mock_coordinator.data = None
        matter_lock.coordinator = mock_coordinator

        matter_lock._on_node_event(
            None,
            _make_node_event(
                event_id=4,
                data={
                    "lockDataType": 6,  # PIN
                    "dataOperationType": 0,  # Add
                    "dataIndex": 1,
                },
            ),
        )

        mock_coordinator.push_update.assert_not_called()

    def test_unknown_operation_type_ignored(self, matter_lock: MatterLock) -> None:
        """Unknown DataOperationType is ignored."""
        mock_coordinator = MagicMock()
        matter_lock.coordinator = mock_coordinator

        matter_lock._on_node_event(
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

    def test_no_coordinator_does_not_crash(self, matter_lock: MatterLock) -> None:
        """LockUserChange with no coordinator attached does not crash."""
        matter_lock.coordinator = None

        matter_lock._on_node_event(
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

    def test_wrong_cluster_ignored(self, matter_lock: MatterLock) -> None:
        """Event from non-DoorLock cluster is ignored."""
        mock_coordinator = MagicMock()
        matter_lock.coordinator = mock_coordinator

        matter_lock._on_node_event(
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
        matter_lock: MatterLock,
    ) -> None:
        """async_set_usercode pushes SlotCode.UNKNOWN after service call."""
        mock_coordinator = MagicMock()
        matter_lock.coordinator = mock_coordinator

        register_mock_service(
            hass,
            MATTER_DOMAIN,
            "set_lock_credential",
            AsyncMock(return_value={LOCK_ENTITY_ID: {}}),
        )

        result = await matter_lock.async_set_usercode(3, "1234")

        assert result is True
        mock_coordinator.push_update.assert_called_once_with({3: SlotCode.UNKNOWN})

    async def test_set_usercode_no_coordinator(
        self,
        hass: HomeAssistant,
        matter_lock: MatterLock,
    ) -> None:
        """async_set_usercode without coordinator does not crash."""
        matter_lock.coordinator = None

        register_mock_service(
            hass,
            MATTER_DOMAIN,
            "set_lock_credential",
            AsyncMock(return_value={LOCK_ENTITY_ID: {}}),
        )

        result = await matter_lock.async_set_usercode(3, "1234")
        assert result is True

    async def test_clear_usercode_pushes_empty(
        self,
        hass: HomeAssistant,
        matter_lock: MatterLock,
    ) -> None:
        """async_clear_usercode pushes SlotCode.EMPTY after clearing."""
        mock_coordinator = MagicMock()
        matter_lock.coordinator = mock_coordinator

        register_mock_service(
            hass,
            MATTER_DOMAIN,
            "get_lock_credential_status",
            AsyncMock(return_value={LOCK_ENTITY_ID: {"credential_exists": True}}),
        )
        register_mock_service(
            hass,
            MATTER_DOMAIN,
            "clear_lock_credential",
            AsyncMock(return_value={LOCK_ENTITY_ID: {}}),
        )

        result = await matter_lock.async_clear_usercode(5)

        assert result is True
        mock_coordinator.push_update.assert_called_once_with({5: SlotCode.EMPTY})

    async def test_clear_empty_slot_no_push(
        self,
        hass: HomeAssistant,
        matter_lock: MatterLock,
    ) -> None:
        """async_clear_usercode on empty slot does not push to coordinator."""
        mock_coordinator = MagicMock()
        matter_lock.coordinator = mock_coordinator

        register_mock_service(
            hass,
            MATTER_DOMAIN,
            "get_lock_credential_status",
            AsyncMock(return_value={LOCK_ENTITY_ID: {"credential_exists": False}}),
        )

        result = await matter_lock.async_clear_usercode(5)

        assert result is False
        mock_coordinator.push_update.assert_not_called()

    async def test_set_usercode_failure_no_push(
        self,
        hass: HomeAssistant,
        matter_lock: MatterLock,
    ) -> None:
        """async_set_usercode does not push when service call fails."""
        mock_coordinator = MagicMock()
        matter_lock.coordinator = mock_coordinator

        register_mock_service(
            hass,
            MATTER_DOMAIN,
            "set_lock_credential",
            AsyncMock(side_effect=HomeAssistantError("timeout")),
        )

        with pytest.raises(LockDisconnected):
            await matter_lock.async_set_usercode(3, "1234")

        mock_coordinator.push_update.assert_not_called()

    async def test_clear_usercode_failure_no_push(
        self,
        hass: HomeAssistant,
        matter_lock: MatterLock,
    ) -> None:
        """async_clear_usercode does not push when clear service fails."""
        mock_coordinator = MagicMock()
        matter_lock.coordinator = mock_coordinator

        register_mock_service(
            hass,
            MATTER_DOMAIN,
            "get_lock_credential_status",
            AsyncMock(return_value={LOCK_ENTITY_ID: {"credential_exists": True}}),
        )
        register_mock_service(
            hass,
            MATTER_DOMAIN,
            "clear_lock_credential",
            AsyncMock(side_effect=HomeAssistantError("timeout")),
        )

        with pytest.raises(LockDisconnected):
            await matter_lock.async_clear_usercode(5)

        mock_coordinator.push_update.assert_not_called()

    async def test_set_usercode_coordinator_data_none_no_push(
        self,
        hass: HomeAssistant,
        matter_lock: MatterLock,
    ) -> None:
        """async_set_usercode skips push when coordinator.data is None."""
        mock_coordinator = MagicMock()
        mock_coordinator.data = None
        matter_lock.coordinator = mock_coordinator

        register_mock_service(
            hass,
            MATTER_DOMAIN,
            "set_lock_credential",
            AsyncMock(return_value={LOCK_ENTITY_ID: {}}),
        )

        result = await matter_lock.async_set_usercode(3, "1234")

        assert result is True
        mock_coordinator.push_update.assert_not_called()
