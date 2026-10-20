"""Test the Matter lock provider."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
import logging
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from chip.clusters import Objects as clusters
from matter_server.client.exceptions import MatterClientException
from matter_server.common.errors import UnknownError
from matter_server.common.models import EventType, MatterNodeEvent
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.matter.helpers import get_node_from_device_entry
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr

from custom_components.lock_code_manager.ha.adapter import ProviderPort, _Coordinator
from custom_components.lock_code_manager.model.credentials import (
    Credential,
    CredentialRef,
    CredentialType,
    CredentialTypeCapability,
    LockCapabilities,
    SetUserResult,
    SlotCredential,
    User,
    WriteResult,
    pin_address,
)
from custom_components.lock_code_manager.model.exceptions import (
    CodeRejectedError,
    DuplicateCodeError,
    LockDisconnected,
    LockOperationFailed,
)
from custom_components.lock_code_manager.providers._util import (
    make_tagged_name,
    parse_tag,
)
from custom_components.lock_code_manager.providers.matter import (
    MatterLock,
    SetCredentialFailedError,
    _lcm_slot_from_raw_users_by_credential_index,
    _raw_user_slot,
)
from tests.providers.helpers import (
    ProviderNativeTransportContractTests,
    ServiceProviderConnectionTests,
)

from .conftest import get_user_answer, get_user_answered_from_listing

# Module path where lock_helpers functions are imported in the provider
_PROVIDER_MODULE = "custom_components.lock_code_manager.providers.matter"


async def _run_now(call: Any) -> Any:
    """Run an exchange at once, as a worker with nothing else queued would."""
    return await call()


def _mock_coordinator() -> MagicMock:
    """Return a stand-in coordinator whose turn runs the exchange at once."""
    coordinator = MagicMock()
    coordinator.async_in_turn = _run_now
    return coordinator


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


async def test_hard_refresh_interval(matter_lock_simple: MatterLock) -> None:
    """Test that hard refresh interval is 1 hour for drift detection."""
    assert matter_lock_simple.hard_refresh_interval == timedelta(hours=1)


# ---------------------------------------------------------------------------
# Connection and availability tests (shared)
# ---------------------------------------------------------------------------


class TestConnection(ServiceProviderConnectionTests):
    """Connection tests for Matter provider using shared mixin."""


class TestNativeTransportContract(ProviderNativeTransportContractTests):
    """Matter routes a native ``MatterClientException`` to LockDisconnected.

    ``InvalidState: Not connected`` (a ``MatterClientException``) is raised by
    the matter-server client before its websocket connects at startup and is
    independent of ``HomeAssistantError`` (issue #1257, PR #1286). The
    provider-specific read sites (get_lock_users / get_lock_info / set / clear)
    are also covered individually below.
    """

    native_transport_exception = MatterClientException("Not connected")

    @pytest.fixture
    def provider_lock(self, matter_lock: MatterLock) -> MatterLock:
        """A lock with a wired client/node so the read reaches ``get_lock_users``.

        ``matter_lock_simple`` has no node, so ``_require_client_and_node``
        raises before the patched SDK call -- a false pass for this contract.
        """
        return matter_lock

    def inject_native_transport_error(self, hass, provider_lock):
        """Patch the lowest read-path SDK call (``get_lock_users``)."""
        return patch(
            f"{_PROVIDER_MODULE}.get_lock_users",
            AsyncMock(side_effect=self.native_transport_exception),
        )


class TestLinkHealth:
    """Thread diagnostics quoted into the slot suspension repair (issue #1397)."""

    @staticmethod
    def _resolve(lock: MatterLock, values: dict[int, Any] | None):
        """Patch node resolution to a node serving the given Thread counters."""
        if values is None:
            return patch.object(lock, "_resolve", return_value=(None, None, None))
        node = MagicMock()
        node.get_attribute_value.side_effect = lambda _endpoint, _cluster, attribute: (
            values.get(attribute)
        )
        return patch.object(lock, "_resolve", return_value=(MagicMock(), node, None))

    async def test_reports_acknowledgements_and_drops(
        self, matter_lock_simple: MatterLock
    ) -> None:
        """Counters taken from a real Aqara U300 on Thread."""
        with self._resolve(matter_lock_simple, {25: 3611401, 26: 3608185, 14: 228}):
            description = matter_lock_simple.describe_link_health()

        assert description is not None
        assert "3608185 of 3611401 transmissions" in description
        assert "dropped off the Thread network 228 times" in description

    async def test_omits_counters_the_lock_does_not_keep(
        self, matter_lock_simple: MatterLock
    ) -> None:
        """
        Every Thread counter is optional, so a lock keeping only some of them
        still describes those rather than reporting blanks.
        """
        with self._resolve(matter_lock_simple, {25: None, 26: None, 14: 228}):
            description = matter_lock_simple.describe_link_health()

        assert description is not None
        assert "dropped off the Thread network 228 times" in description
        assert "transmissions that asked to be" not in description

    async def test_none_when_no_counters_kept(
        self, matter_lock_simple: MatterLock
    ) -> None:
        """A Wi-Fi lock, or one without the counter features, describes nothing."""
        with self._resolve(matter_lock_simple, {25: None, 26: None, 14: None}):
            assert matter_lock_simple.describe_link_health() is None

    async def test_none_when_node_unresolved(
        self, matter_lock_simple: MatterLock
    ) -> None:
        """An unresolved node must not abort the suspension it is describing."""
        with self._resolve(matter_lock_simple, None):
            assert matter_lock_simple.describe_link_health() is None

    @pytest.mark.parametrize(
        "error", [UnknownError("boom"), KeyError(0), HomeAssistantError("boom")]
    )
    async def test_none_when_reading_counters_fails(
        self, matter_lock_simple: MatterLock, error: Exception
    ) -> None:
        """
        A failed diagnostics read must not cost the caller its repair: this runs
        ahead of _suspend_slot, outside the tick's own error handling.
        """
        node = MagicMock()
        node.get_attribute_value.side_effect = error
        with patch.object(
            matter_lock_simple, "_resolve", return_value=(MagicMock(), node, None)
        ):
            assert matter_lock_simple.describe_link_health() is None


class TestDeviceAvailability:
    """Device availability tests for Matter provider."""

    async def test_is_device_available_false_when_entity_unavailable(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """Availability is False when the lock entity is unavailable."""
        hass.states.async_set(matter_lock_simple.lock.entity_id, STATE_UNAVAILABLE)
        assert await matter_lock_simple.async_is_device_available() is False

    async def test_is_device_available_false_when_entity_unknown(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """Availability is False when the lock entity state is unknown."""
        hass.states.async_set(matter_lock_simple.lock.entity_id, STATE_UNKNOWN)
        assert await matter_lock_simple.async_is_device_available() is False

    async def test_is_device_available_false_when_entity_missing(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """Availability is False when the lock entity has no state yet."""
        assert await matter_lock_simple.async_is_device_available() is False

    async def test_is_device_available_ignores_node_resolution(
        self, hass: HomeAssistant, matter_lock_simple: MatterLock
    ) -> None:
        """Availability follows entity state, not node re-derivation (issue #1268).

        During a Matter server reconnect the client wipes and rebuilds its node
        set, so ``get_node_from_device_entry`` transiently returns None while the
        lock entity stays available. The gate must not trip on that transient --
        otherwise the breaker suspends a reachable lock for a full backoff cycle.

        ``matter_lock_simple`` has no device, so node resolution naturally
        returns None -- the same observable state as a mid-reconnect node wipe.
        """
        hass.states.async_set(matter_lock_simple.lock.entity_id, "locked")
        assert await matter_lock_simple.async_is_device_available() is True


# ---------------------------------------------------------------------------
# Setup tests
# ---------------------------------------------------------------------------


async def test_setup_internal_unsupported_lock(
    hass: HomeAssistant,
    matter_lock: MatterLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Base setup degrades when the lock advertises no PIN credential support.

    ``supports_user_management`` alone no longer fails setup (slot-only
    locks are served via the seam's credential-primitive routing); the
    structural requirement is PIN support, which this lock also lacks.
    The failure keeps the lock degraded (coordinator created, setup not
    marked successful) rather than raising, so it stays visible and
    revalidates when the provider integration reloads.
    """
    mock_get_lock_info = AsyncMock(return_value={"supports_user_management": False})
    with patch(f"{_PROVIDER_MODULE}.get_lock_info", mock_get_lock_info):
        await matter_lock.async_setup_internal(
            simple_lcm_config_entry, _Coordinator(ProviderPort(matter_lock))
        )

    assert matter_lock._setup_succeeded is False
    assert matter_lock.coordinator is not None

    await matter_lock.async_unload(False)


async def test_setup_internal_no_pin_support(
    hass: HomeAssistant,
    matter_lock: MatterLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Base setup degrades when the lock supports users but not PIN credentials."""
    mock_get_lock_info = AsyncMock(
        return_value={
            "supports_user_management": True,
            "supported_credential_types": ["rfid"],
        }
    )
    with patch(f"{_PROVIDER_MODULE}.get_lock_info", mock_get_lock_info):
        await matter_lock.async_setup_internal(
            simple_lcm_config_entry, _Coordinator(ProviderPort(matter_lock))
        )

    assert matter_lock._setup_succeeded is False
    assert matter_lock.coordinator is not None

    await matter_lock.async_unload(False)


# ---------------------------------------------------------------------------
# get_usercodes tests
# ---------------------------------------------------------------------------


async def test_get_usercodes_no_users(
    hass: HomeAssistant,
    e2e_matter_lock: MatterLock,
    matter_mock_helpers: dict[str, AsyncMock],
) -> None:
    """Test get_usercodes when no users exist on the lock."""
    matter_mock_helpers["get_lock_users"].return_value = {"max_users": 10, "users": []}
    codes = await e2e_matter_lock.async_get_usercodes()

    assert codes[1] is SlotCredential.empty()
    assert codes[2] is SlotCredential.empty()


async def test_get_usercodes_no_configured_slots(
    hass: HomeAssistant,
    matter_lock: MatterLock,
) -> None:
    """Test get_usercodes returns empty dict when no slots configured and no occupied slots."""
    mock_get_lock_users = AsyncMock(return_value={"users": []})
    with patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users):
        codes = await matter_lock.async_get_usercodes()
    assert codes == {}


async def test_get_usercodes_unmanaged_occupied_slots(
    hass: HomeAssistant,
    matter_lock: MatterLock,
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
    with patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users):
        codes = await matter_lock.async_get_usercodes()
    assert codes == {5: SlotCredential.unreadable(), 8: SlotCredential.unreadable()}


async def test_get_usercodes_none_credential_index_skipped(
    hass: HomeAssistant,
    matter_lock: MatterLock,
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
    with patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users):
        codes = await matter_lock.async_get_usercodes()
    # Only slot 4 should appear; the credential with no index is skipped
    assert codes == {4: SlotCredential.unreadable()}


# ---------------------------------------------------------------------------
# hard_refresh_codes tests
# ---------------------------------------------------------------------------


async def test_hard_refresh_codes(
    hass: HomeAssistant,
    e2e_matter_lock: MatterLock,
    matter_mock_helpers: dict[str, AsyncMock],
) -> None:
    """Test hard_refresh_codes returns same result as get_usercodes."""
    matter_mock_helpers["get_lock_users"].return_value = {
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
    codes = await e2e_matter_lock.async_hard_refresh_codes()

    assert codes[1] is SlotCredential.empty()
    assert codes[2] is SlotCredential.unreadable()


# ---------------------------------------------------------------------------
# Error tests
# ---------------------------------------------------------------------------


async def test_get_usercodes_client_unavailable(
    hass: HomeAssistant, matter_lock_simple: MatterLock
) -> None:
    """Test async_get_usercodes raises LockDisconnected when client/node unavailable.

    ``matter_lock_simple`` has no device, so the owning client never
    resolves -- the natural client-unavailable state.
    """
    with pytest.raises(LockDisconnected, match="Matter client unavailable"):
        await matter_lock_simple.async_get_usercodes()


async def test_get_usercodes_get_lock_users_communication_error(
    hass: HomeAssistant, matter_lock: MatterLock
) -> None:
    """Test async_get_usercodes raises LockDisconnected on HomeAssistantError from get_lock_users."""
    mock_get_lock_users = AsyncMock(side_effect=HomeAssistantError("connection lost"))
    with patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users):
        with pytest.raises(LockDisconnected, match="get_lock_users failed"):
            await matter_lock.async_get_usercodes()


async def test_get_usercodes_multiple_credential_types(
    hass: HomeAssistant,
    e2e_matter_lock: MatterLock,
    matter_mock_helpers: dict[str, AsyncMock],
) -> None:
    """Test that only PIN credentials are considered, not other types like RFID."""
    matter_mock_helpers["get_lock_users"].return_value = {
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
    codes = await e2e_matter_lock.async_get_usercodes()

    # Slot 1 has only RFID credential, not PIN
    assert codes[1] is SlotCredential.empty()
    # Slot 2 has a PIN credential
    assert codes[2] is SlotCredential.unreadable()


async def test_node_resolution_exception_surfaces_disconnected(
    hass: HomeAssistant, matter_lock: MatterLock
) -> None:
    """Node resolution swallows resolution errors and surfaces as unavailable.

    A failure inside the owning-client lookup is swallowed to None, so the
    public ``_require_client_and_node`` gate reports the lock disconnected
    rather than letting the exception escape.
    """
    with (
        patch.object(matter_lock, "_fresh_device_entry", return_value=MagicMock()),
        patch.object(
            matter_lock,
            "_owning_matter_client",
            side_effect=Exception("node lookup failed"),
        ),
        pytest.raises(LockDisconnected),
    ):
        matter_lock._require_client_and_node()


async def test_setup_internal_client_unavailable(
    hass: HomeAssistant,
    matter_lock_simple: MatterLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Transient client/node unavailability logs and continues without raising.

    ``matter_lock_simple`` has no device, so the owning client never
    resolves -- the natural client-unavailable state.
    """
    await matter_lock_simple.async_setup_internal(
        simple_lcm_config_entry, _Coordinator(ProviderPort(matter_lock_simple))
    )
    assert matter_lock_simple._capabilities_cache is None


async def test_setup_internal_caps_service_validation_error(
    hass: HomeAssistant,
    matter_lock: MatterLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """ServiceValidationError from get_lock_info logs and continues."""
    mock_get_lock_info = AsyncMock(side_effect=ServiceValidationError("bad input"))
    with patch(f"{_PROVIDER_MODULE}.get_lock_info", mock_get_lock_info):
        await matter_lock.async_setup_internal(
            simple_lcm_config_entry, _Coordinator(ProviderPort(matter_lock))
        )
    assert matter_lock._capabilities_cache is None
    # A failed setup is retried: let it go before the test ends.
    await matter_lock.async_unload(False)
    assert matter_lock.coordinator is not None


async def test_setup_internal_caps_communication_error(
    hass: HomeAssistant,
    matter_lock: MatterLock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Base setup logs and continues on HomeAssistantError from get_lock_info."""
    mock_get_lock_info = AsyncMock(side_effect=HomeAssistantError("connection lost"))
    with patch(f"{_PROVIDER_MODULE}.get_lock_info", mock_get_lock_info):
        # async_get_capabilities maps HomeAssistantError → LockDisconnected;
        # async_setup_internal catches that and logs a warning so the
        # coordinator can be created and retry once the lock comes online.
        await matter_lock.async_setup_internal(
            simple_lcm_config_entry, _Coordinator(ProviderPort(matter_lock))
        )
    assert matter_lock._capabilities_cache is None
    # A failed setup is retried: let it go before the test ends.
    await matter_lock.async_unload(False)
    assert matter_lock.coordinator is not None


async def test_require_client_and_node_no_client(
    hass: HomeAssistant, matter_lock_simple: MatterLock
) -> None:
    """Test _require_client_and_node raises LockDisconnected when client is None.

    ``matter_lock_simple`` has no device, so the owning client never
    resolves.
    """
    with pytest.raises(LockDisconnected, match="Matter client unavailable"):
        matter_lock_simple._require_client_and_node()


async def test_require_client_and_node_no_node(
    hass: HomeAssistant, matter_lock: MatterLock, matter_client: MagicMock
) -> None:
    """Test _require_client_and_node raises LockDisconnected when node is None.

    The client resolves (server_info present) but its node set is empty, so
    the device cannot be matched -- the "node not found" branch.
    """
    matter_client.get_nodes.return_value = []
    with pytest.raises(LockDisconnected, match="Matter node not found"):
        matter_lock._require_client_and_node()


def test_match_node_returns_none_without_matching_device_identifier(
    matter_lock: MatterLock, matter_client: MagicMock
) -> None:
    """_match_node returns None when the device has no Matter deviceid_ identifier.

    ``_resolve`` calls this for every device it looks up; a device whose
    identifiers don't include a ``deviceid_``-prefixed Matter identifier
    (e.g. a stale or foreign device) must resolve to "no node" rather than
    raising, so the caller reports it via the ordinary node-not-found path.
    """
    device = MagicMock()
    device.identifiers = {("not_matter", "some_other_id")}

    assert matter_lock._match_node(matter_client, device) is None


async def test_log_node_resolution_failure_handles_get_nodes_exception(
    hass: HomeAssistant,
    matter_lock: MatterLock,
    matter_client: MagicMock,
) -> None:
    """The node-count diagnostic degrades to -1 when get_nodes() itself fails.

    ``_require_client_and_node`` resolves once via ``_resolve`` (which
    succeeds with an empty node list here) and then calls ``get_nodes()``
    again purely for the failure-diagnostic log. If the client's node list
    becomes unavailable between those two calls (e.g. a disconnect mid
    resolution), the diagnostic log must still complete -- with node_count
    reported as -1 -- rather than letting the logging failure mask the
    real "node not found" error.
    """
    matter_client.get_nodes.side_effect = [[], RuntimeError("nodes unavailable")]

    with pytest.raises(LockDisconnected, match="Matter node not found"):
        matter_lock._require_client_and_node()


async def test_log_node_resolution_failure_falls_back_when_diagnostics_raise(
    hass: HomeAssistant,
    matter_lock: MatterLock,
    matter_client: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Diagnostic logging itself is best-effort and never escapes the caller.

    ``_log_node_resolution_failure`` is purely informational (issue #1268
    triage data). If collecting that diagnostic data raises for any reason
    (e.g. the config entries API failing), the fallback debug log fires
    instead of the exception propagating -- the caller still surfaces the
    ordinary "node not found" LockDisconnected.
    """
    matter_client.get_nodes.return_value = []
    caplog.set_level(logging.DEBUG, logger=_PROVIDER_MODULE)

    with (
        patch.object(
            hass.config_entries,
            "async_loaded_entries",
            side_effect=RuntimeError("registry unavailable"),
        ),
        pytest.raises(LockDisconnected, match="Matter node not found"),
    ):
        matter_lock._require_client_and_node()

    assert "diagnostics unavailable" in caplog.text


# =============================================================================
# Tag resolver helpers (pure functions over the raw user list)
# =============================================================================


class TestLcmSlotResolvers:
    """Unit tests for ``_lcm_slot_from_raw_users_by_*`` helpers."""

    _USERS: list[dict[str, Any]] = [
        {
            "user_index": 1,
            "user_name": "lcm:5:Alice",
            "credentials": [{"type": "pin", "index": 7}],
        },
        {
            "user_index": 2,
            "user_name": "External",  # untagged
            "credentials": [{"type": "pin", "index": 3}],
        },
        {
            "user_index": 3,
            "user_name": "lcm:8:Bob",
            "credentials": [],  # no credentials -- persistent anchor
        },
    ]

    @pytest.mark.parametrize(
        ("name", "creator", "fabric", "slot"),
        [
            ("lcm:3:Tim", 9, 2, 3),
            ("lcm3", 2, 2, 3),
            ("3", 2, 2, 3),
            ("lcm3", 9, 2, None),
            ("3", 9, 2, None),
            ("3", None, 2, None),
            ("3", 2, None, None),
            ("03", 2, 2, None),
        ],
        ids=[
            "a_full_tag_whoever_made_it",
            "compact_on_ha_s_user",
            "slot_only_on_ha_s_user",
            "compact_on_another_fabric_s",
            "slot_only_on_another_fabric_s",
            "slot_only_made_at_the_keypad",
            "fabric_unknown",
            "not_a_name_lcm_writes",
        ],
    )
    def test_a_short_name_is_lcm_s_only_on_a_user_its_fabric_made(
        self, name: str, creator: int | None, fabric: int | None, slot: int | None
    ) -> None:
        """``lcm3`` and ``3`` are LCM's names only where its fabric made the user (#1729)."""
        users = [
            {
                "user_index": 1,
                "user_name": name,
                "creator_fabric_index": creator,
                "credentials": [{"type": "pin", "index": 4}],
            }
        ]
        assert _raw_user_slot(users[0], fabric) == slot
        assert _lcm_slot_from_raw_users_by_credential_index(users, 4, fabric) == slot

    def test_a_tagged_user_s_slot(self) -> None:
        assert [_raw_user_slot(user, None) for user in self._USERS] == [5, None, 8]

    def test_by_credential_index_resolves_tagged(self) -> None:
        assert _lcm_slot_from_raw_users_by_credential_index(self._USERS, 7, None) == 5

    def test_by_credential_index_returns_none_for_untagged_owner(self) -> None:
        # credential index 3 belongs to "External" (untagged) -- resolves to None.
        assert (
            _lcm_slot_from_raw_users_by_credential_index(self._USERS, 3, None) is None
        )

    def test_by_credential_index_returns_none_for_unknown(self) -> None:
        assert (
            _lcm_slot_from_raw_users_by_credential_index(self._USERS, 99, None) is None
        )
        assert (
            _lcm_slot_from_raw_users_by_credential_index(self._USERS, None, None)
            is None
        )


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


@contextmanager
def _patch_user_reads(lock: MatterLock, users: list[dict]) -> Iterator[None]:
    """
    Answer the provider's user reads from ``users``.

    Both of them: the walk over every user (``get_lock_users``) and the
    GetUser for the user an event names, answered as the lock would for
    the same users.
    """
    client, _node = lock._require_client_and_node()
    with (
        patch(
            f"{_PROVIDER_MODULE}.get_lock_users",
            AsyncMock(return_value={"max_users": 10, "users": users}),
        ),
        get_user_answered_from_listing(client),
    ):
        yield


@contextmanager
def _patch_user_reads_failing(lock: MatterLock, error: Exception) -> Iterator[None]:
    """Fail both of the provider's user reads with ``error``, as a lock out of reach does."""
    client, _node = lock._require_client_and_node()
    with (
        patch(f"{_PROVIDER_MODULE}.get_lock_users", AsyncMock(side_effect=error)),
        patch.object(client, "send_device_command", AsyncMock(side_effect=error)),
    ):
        yield


class TestLockOperationEvent:
    """Test _handle_lock_operation callback and code-slot event firing.

    These tests use ``matter_lock`` (the real Matter integration) so node-event
    dispatch resolves a live client+node, sends GetUser for the user an event
    names (``_raw_lock_user``), and lists every user (``_raw_lock_users``)
    when it names none the lock can hold, or an empty one.
    """

    _patch_users = staticmethod(_patch_user_reads)

    async def test_unlock_with_pin_credential(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Unlock with PIN credential fires a code slot event.

        Resolution: ``userIndex`` -> tagged user.name -> LCM slot. The
        Matter ``credentialIndex`` may differ from the LCM slot and must
        not be used as the code slot directly.
        """
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        with self._patch_users(
            matter_lock,
            [
                {
                    "user_index": 9,
                    "user_name": "lcm:2:Alice",
                    "credentials": [{"type": "pin", "index": 7}],
                },
            ],
        ):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    data={
                        "lockOperationType": 1,  # kUnlock
                        "userIndex": 9,
                        "credentials": [
                            # Matter credential index is opaque; the LCM slot
                            # is resolved from the owning user's tag.
                            {"credentialType": 1, "credentialIndex": 7},
                        ],
                    }
                ),
            )
            await hass.async_block_till_done()

        assert len(fired) == 1
        assert fired[0]["code_slot"] == 2
        assert fired[0]["to_locked"] is False

    async def test_lock_with_pin_credential(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Lock with PIN credential fires a code slot event."""
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        with self._patch_users(
            matter_lock,
            [
                {
                    "user_index": 4,
                    "user_name": "lcm:5:Bob",
                    "credentials": [{"type": "pin", "index": 2}],
                },
            ],
        ):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    data={
                        "lockOperationType": 0,  # kLock
                        "userIndex": 4,
                        "credentials": [
                            {"credentialType": 1, "credentialIndex": 2},
                        ],
                    }
                ),
            )
            await hass.async_block_till_done()

        assert len(fired) == 1
        assert fired[0]["code_slot"] == 5
        assert fired[0]["to_locked"] is True

    async def test_projects_lcm_slot_from_tag_when_credential_index_differs(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """The regression: Matter-auto-allocated credential index must not become the LCM slot.

        Pre-PR-B, LCM pinned ``credentialIndex == LCM slot``, so firing
        ``code_slot=credentialIndex`` was correct. Under the user-tag
        model the Matter credential index is opaque and auto-allocated.
        Here LCM slot 3 owns Matter credential index 11; the event must
        fire on slot 3 (from the owning user's tag), NOT slot 11.
        """
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        with self._patch_users(
            matter_lock,
            [
                {
                    "user_index": 1,
                    "user_name": "lcm:3:Carol",
                    "credentials": [{"type": "pin", "index": 11}],
                },
            ],
        ):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    data={
                        "lockOperationType": 1,
                        "userIndex": 1,
                        "credentials": [
                            {"credentialType": 1, "credentialIndex": 11},
                        ],
                    }
                ),
            )
            await hass.async_block_till_done()

        assert len(fired) == 1
        assert fired[0]["code_slot"] == 3

    async def test_preserves_slot_zero_resolved_by_user_index(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """
        Slot 0 on the named user is reported, and no credentialIndex search runs.

        A search runs only for a user index that reads empty; slot 0 is
        falsy, so a check on truth rather than on ``None`` would search and
        report the decoy's slot 4 instead.
        """
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        with self._patch_users(
            matter_lock,
            [
                # The user the event names, tagged for LCM slot 0.
                {
                    "user_index": 1,
                    "user_name": "lcm:0:Zero",
                    "credentials": [{"type": "pin", "index": 5}],
                },
                # The only holder of the event's credentialIndex 9, tagged
                # for another slot: a search would yield slot 4.
                {
                    "user_index": 2,
                    "user_name": "lcm:4:Decoy",
                    "credentials": [{"type": "pin", "index": 9}],
                },
            ],
        ):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    data={
                        "lockOperationType": 1,
                        "userIndex": 1,
                        "credentials": [
                            {"credentialType": 1, "credentialIndex": 9},
                        ],
                    }
                ),
            )
            await hass.async_block_till_done()

        assert len(fired) == 1
        assert fired[0]["code_slot"] == 0

    async def test_credential_index_as_string_is_coerced(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Some Matter implementations send ``credentialIndex`` as a string.

        The fallback walker compares against the raw user list's int
        ``index`` field; without ``parse_slot_num`` coercion, the
        comparison would never match and the event would silently drop
        when ``userIndex`` is absent.
        """
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        with self._patch_users(
            matter_lock,
            [
                {
                    "user_index": 50,
                    "user_name": "lcm:7:Dave",
                    "credentials": [{"type": "pin", "index": 4}],
                },
            ],
        ):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    data={
                        "lockOperationType": 1,
                        # userIndex omitted to force the credentialIndex walk;
                        # credentialIndex sent as a string.
                        "credentials": [
                            {"credentialType": 1, "credentialIndex": "4"},
                        ],
                    }
                ),
            )
            await hass.async_block_till_done()

        assert len(fired) == 1
        assert fired[0]["code_slot"] == 7

    async def test_falls_back_to_credential_index_walk_when_user_index_missing(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Matter spec allows ``userIndex`` to be omitted on LockOperation.

        When it's absent we walk the user list to find the PIN credential at
        ``credentialIndex`` and parse the owning user's tag. Without this
        fallback the event would silently drop when a lock implementation
        elides ``userIndex`` from PIN operations.
        """
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        with self._patch_users(
            matter_lock,
            [
                {
                    "user_index": 50,
                    "user_name": "lcm:7:Dave",
                    "credentials": [{"type": "pin", "index": 4}],
                },
            ],
        ):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    data={
                        "lockOperationType": 1,
                        # userIndex omitted -- fallback to credentialIndex walk.
                        "credentials": [
                            {"credentialType": 1, "credentialIndex": 4},
                        ],
                    }
                ),
            )
            await hass.async_block_till_done()

        assert len(fired) == 1
        assert fired[0]["code_slot"] == 7

    async def test_lock_op_for_untagged_user_ignored(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """LockOperation on a non-LCM-tagged user fires no code slot event.

        Another controller (or keypad-enrolled user) using a PIN must not
        push a code slot event into LCM; firing with the wrong slot is
        worse than not firing.
        """
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        with self._patch_users(
            matter_lock,
            [
                {
                    "user_index": 9,
                    "user_name": "Visitor",  # untagged
                    "credentials": [{"type": "pin", "index": 9}],
                },
            ],
        ):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    data={
                        "lockOperationType": 1,
                        "userIndex": 9,
                        "credentials": [
                            {"credentialType": 1, "credentialIndex": 9},
                        ],
                    }
                ),
            )
            await hass.async_block_till_done()

        assert len(fired) == 0

    async def test_lock_op_with_no_resolvable_owner_ignored(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Neither ``userIndex`` nor ``credentialIndex`` matches a known user."""
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        with self._patch_users(matter_lock, []):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    data={
                        "lockOperationType": 1,
                        "userIndex": 4,
                        "credentials": [
                            {"credentialType": 1, "credentialIndex": 4},
                        ],
                    }
                ),
            )
            await hass.async_block_till_done()

        assert len(fired) == 0

    async def test_lock_op_dispatch_swallows_lock_disconnected(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Transport failure during owner lookup logs and drops the event."""
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        with _patch_user_reads_failing(
            matter_lock, HomeAssistantError("transport down")
        ):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    data={
                        "lockOperationType": 1,
                        "userIndex": 1,
                        "credentials": [
                            {"credentialType": 1, "credentialIndex": 1},
                        ],
                    }
                ),
            )
            await hass.async_block_till_done()

        assert len(fired) == 0

    @pytest.mark.parametrize(
        "credential_type",
        [
            pytest.param(0, id="programming_pin"),
            pytest.param(6, id="aliro"),
        ],
    )
    async def test_an_unreported_credential_is_ignored(
        self, hass: HomeAssistant, matter_lock: MatterLock, credential_type: int
    ) -> None:
        """
        The admin code and an Aliro key fire nothing, even from an LCM user.

        Neither has a ``CredentialType`` that describes it honestly, so a
        use of one is left unreported rather than named as something else.
        """
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        with self._patch_users(
            matter_lock,
            [{"user_index": 1, "user_name": "lcm:1:Alice", "credentials": []}],
        ):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    data={
                        "lockOperationType": 1,
                        "userIndex": 1,
                        "credentials": [
                            {"credentialType": credential_type, "credentialIndex": 3},
                        ],
                    }
                ),
            )
            await hass.async_block_till_done()

        assert fired == []

    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            pytest.param(
                {
                    "lockOperationType": 1,
                    "operationSource": 3,
                    "userIndex": 4,
                    "fabricIndex": 1,
                    "sourceNode": None,
                    "credentials": [{"credentialType": 3, "credentialIndex": 3}],
                },
                [
                    {
                        "code_slot": 4,
                        "to_locked": False,
                        "credential_type": CredentialType.PIN,
                    }
                ],
                id="keypad",
            ),
            pytest.param(
                {
                    "lockOperationType": 1,
                    "operationSource": 10,
                    "userIndex": 4,
                    "fabricIndex": 1,
                    "sourceNode": None,
                    "credentials": [{"credentialType": 10, "credentialIndex": 1}],
                },
                [],
                id="home_key",
            ),
        ],
    )
    async def test_a_lock_that_reports_the_source_as_the_type(
        self,
        hass: HomeAssistant,
        matter_lock: MatterLock,
        data: dict[str, Any],
        expected: list[dict[str, Any]],
    ) -> None:
        """
        The Aqara U400's keypad PIN is a PIN, and its Home Key is still unreported.

        Its firmware (3.1.1.0) writes ``operationSource`` into
        ``credentialType``. Payloads as captured from the lock by the Matter
        server (#1745); the Home Key's ``userIndex`` is set to the LCM user's
        so only the credential decides.
        """
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        with self._patch_users(
            matter_lock,
            [{"user_index": 4, "user_name": "lcm:4:Gue", "credentials": []}],
        ):
            matter_lock._on_node_event(None, _make_node_event(data=data))
            await hass.async_block_till_done()

        assert fired == expected

    async def test_a_card_is_not_placed_by_a_pin_at_its_index(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """
        Without a userIndex, a card at index 4 is not the user with PIN 4.

        The fallback walks PIN credentials, and card and PIN indices are
        separate spaces on the lock.
        """
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        with self._patch_users(
            matter_lock,
            [
                {
                    "user_index": 1,
                    "user_name": "lcm:1:Alice",
                    "credentials": [{"type": "pin", "index": 4}],
                }
            ],
        ):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    data={
                        "lockOperationType": 1,
                        "credentials": [{"credentialType": 2, "credentialIndex": 4}],
                    }
                ),
            )
            await hass.async_block_till_done()

        assert fired == []

    async def test_a_pin_presented_with_a_fingerprint_is_reported(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """
        A PIN-and-fingerprint use without a userIndex still finds its user.

        The PIN is the credential the fallback can place, so it is the one
        reported, wherever it sits in the list.
        """
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        with self._patch_users(
            matter_lock,
            [
                {
                    "user_index": 1,
                    "user_name": "lcm:1:Alice",
                    "credentials": [{"type": "pin", "index": 4}],
                }
            ],
        ):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    data={
                        "lockOperationType": 1,
                        "credentials": [
                            {"credentialType": 3, "credentialIndex": 9},
                            {"credentialType": 1, "credentialIndex": 4},
                        ],
                    }
                ),
            )
            await hass.async_block_till_done()

        assert fired == [
            {"code_slot": 1, "to_locked": False, "credential_type": CredentialType.PIN}
        ]

    @pytest.mark.parametrize(
        ("source", "credential_type", "expected"),
        [
            # The source written into credentialType, as the Aqara U400 does.
            pytest.param(2, 2, None, id="app_reported_as_rfid"),
            pytest.param(4, 4, None, id="auto_reported_as_finger_vein"),
            pytest.param(5, 5, None, id="button_reported_as_face"),
            pytest.param(1, 1, None, id="manual_reported_as_pin"),
            pytest.param(8, 8, CredentialType.RFID, id="rfid_reported_as_aliro"),
            pytest.param(9, 9, None, id="biometric_reported_as_its_source"),
            # A source that names no user credential outranks even a PIN type.
            pytest.param(6, 1, None, id="schedule_naming_a_pin"),
            pytest.param(10, 1, None, id="aliro_naming_a_pin"),
            pytest.param(8, 2, CredentialType.RFID, id="rfid"),
            pytest.param(9, 3, CredentialType.FINGERPRINT, id="fingerprint"),
            pytest.param(9, 4, CredentialType.FINGERPRINT, id="finger_vein"),
            pytest.param(9, 5, CredentialType.FACE, id="face"),
            pytest.param(9, 1, None, id="biometric_naming_a_pin"),
            # A source that does not say leaves the kind to credentialType.
            pytest.param(7, 1, CredentialType.PIN, id="remote_pin"),
            pytest.param(7, 2, CredentialType.RFID, id="remote_rfid"),
            pytest.param(0, 5, CredentialType.FACE, id="unspecified_face"),
            pytest.param(200, 3, CredentialType.FINGERPRINT, id="vendor_source"),
            pytest.param(None, 2, CredentialType.RFID, id="no_source"),
            # The lock's admin code is no user's use, whichever reader took it.
            pytest.param(3, 0, None, id="keypad_programming_pin"),
            pytest.param(8, 0, None, id="rfid_reader_programming_pin"),
        ],
    )
    async def test_the_source_names_the_kind_ahead_of_the_type(
        self,
        hass: HomeAssistant,
        matter_lock: MatterLock,
        source: int | None,
        credential_type: int,
        expected: CredentialType | None,
    ) -> None:
        """
        A use is named by the reader it came from, and only then by its type.

        The Aqara U400 (#1745) writes ``operationSource`` into
        ``credentialType``; the reporter captured only its keypad and Home Key
        payloads, so the app, auto, button, manual, RFID and biometric cases
        here extrapolate that pattern to the other sources. On such firmware
        an app unlock reads as a card and spends a Slot Usage Limiter use.
        """
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)
        data: dict[str, Any] = {
            "lockOperationType": 1,
            "userIndex": 4,
            "credentials": [{"credentialType": credential_type, "credentialIndex": 3}],
        }
        if source is not None:
            data["operationSource"] = source

        with self._patch_users(
            matter_lock,
            [{"user_index": 4, "user_name": "lcm:4:Gue", "credentials": []}],
        ):
            matter_lock._on_node_event(None, _make_node_event(data=data))
            await hass.async_block_till_done()

        assert fired == (
            []
            if expected is None
            else [{"code_slot": 4, "to_locked": False, "credential_type": expected}]
        )

    @pytest.mark.parametrize(
        "credential",
        [
            pytest.param({"credentialType": 2}, id="missing"),
            pytest.param({"credentialType": 2, "credentialIndex": None}, id="none"),
            pytest.param({"credentialType": 2, "credentialIndex": "card"}, id="text"),
        ],
    )
    async def test_a_use_naming_its_user_needs_no_credential_index(
        self, hass: HomeAssistant, matter_lock: MatterLock, credential: dict[str, Any]
    ) -> None:
        """
        A card use naming its user is that user's, whatever its index reads.

        Only the fallback that places a PIN by its index reads the index; the
        userIndex alone names the user.
        """
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        with self._patch_users(
            matter_lock,
            [{"user_index": 4, "user_name": "lcm:4:Gue", "credentials": []}],
        ):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    data={
                        "lockOperationType": 1,
                        "operationSource": 8,
                        "userIndex": 4,
                        "credentials": [credential],
                    }
                ),
            )
            await hass.async_block_till_done()

        assert fired == [
            {"code_slot": 4, "to_locked": False, "credential_type": CredentialType.RFID}
        ]

    @pytest.mark.parametrize(
        ("source", "credential"),
        [
            pytest.param(8, {"credentialType": 2}, id="card_missing"),
            pytest.param(
                8, {"credentialType": 2, "credentialIndex": None}, id="card_none"
            ),
            pytest.param(
                8, {"credentialType": 2, "credentialIndex": "card"}, id="card_text"
            ),
            pytest.param(3, {"credentialType": 1}, id="pin_missing"),
            pytest.param(
                3, {"credentialType": 1, "credentialIndex": "four"}, id="pin_text"
            ),
        ],
    )
    async def test_a_use_naming_no_user_and_no_pin_index_is_dropped_unread(
        self,
        hass: HomeAssistant,
        matter_lock: MatterLock,
        source: int,
        credential: dict[str, Any],
    ) -> None:
        """
        Without a userIndex or a readable PIN index, nothing names the user.

        So the use is dropped without reading the lock's user list, which on
        a real lock is a walk of every user.
        """
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)
        get_users = AsyncMock(
            return_value={
                "max_users": 10,
                "users": [
                    {
                        "user_index": 4,
                        "user_name": "lcm:4:Gue",
                        "credentials": [{"type": "pin", "index": 4}],
                    }
                ],
            }
        )

        with patch(f"{_PROVIDER_MODULE}.get_lock_users", get_users):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    data={
                        "lockOperationType": 1,
                        "operationSource": source,
                        "credentials": [credential],
                    }
                ),
            )
            await hass.async_block_till_done()

        assert fired == []
        get_users.assert_not_awaited()

    @pytest.mark.parametrize(
        ("source", "credentials", "expected"),
        [
            pytest.param(
                3,
                [{"credentialType": 1, "credentialIndex": 4}],
                [
                    {
                        "code_slot": 1,
                        "to_locked": False,
                        "credential_type": CredentialType.PIN,
                    }
                ],
                id="keypad_pin",
            ),
            pytest.param(
                3,
                [
                    {"credentialType": 2, "credentialIndex": 9},
                    {"credentialType": 1, "credentialIndex": 4},
                ],
                [
                    {
                        "code_slot": 1,
                        "to_locked": False,
                        "credential_type": CredentialType.PIN,
                    }
                ],
                id="keypad_card_then_pin",
            ),
            pytest.param(
                3, [{"credentialType": 2, "credentialIndex": 4}], [], id="keypad_card"
            ),
            pytest.param(
                3,
                [{"credentialType": 3, "credentialIndex": 4}],
                [],
                id="keypad_reported_as_fingerprint",
            ),
            pytest.param(
                8,
                [{"credentialType": 1, "credentialIndex": 4}],
                [],
                id="rfid_reader_naming_a_pin",
            ),
        ],
    )
    @pytest.mark.parametrize(
        "user_index",
        [None, 2, 11],
        ids=["no_user", "an_empty_user", "past_the_lock_s_users"],
    )
    async def test_only_a_pin_the_lock_typed_is_placed_by_its_index(
        self,
        hass: HomeAssistant,
        matter_lock: MatterLock,
        source: int,
        credentials: list[dict[str, Any]],
        expected: list[dict[str, Any]],
        user_index: int | None,
    ) -> None:
        """
        Naming no user, PIN 4's holder owns only a PIN the lock typed at 4.

        A keypad use is reported as a PIN whatever its ``credentialType``,
        but the index is in the space of the type the lock gave it: a card
        at 4 -- or the Aqara U400's keypad credential, typed 3 -- says
        nothing about who holds PIN 4. An event names no user when it
        carries no ``userIndex``, names one that reads empty, or names one
        past the users the lock holds (10).
        """
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)
        named = {} if user_index is None else {"userIndex": user_index}

        with self._patch_users(
            matter_lock,
            [
                {
                    "user_index": 1,
                    "user_name": "lcm:1:Alice",
                    "credentials": [{"type": "pin", "index": 4}],
                }
            ],
        ):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    data={
                        "lockOperationType": 1,
                        "operationSource": source,
                        "credentials": credentials,
                    }
                    | named
                ),
            )
            await hass.async_block_till_done()

        assert fired == expected

    async def test_wrong_cluster_ignored(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Events from non-DoorLock clusters are ignored upstream of dispatch."""
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        with self._patch_users(matter_lock, []):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    cluster_id=6,  # OnOff cluster
                    data={"credentials": [{"credentialType": 1, "credentialIndex": 1}]},
                ),
            )
            await hass.async_block_till_done()

        assert len(fired) == 0

    async def test_wrong_event_id_ignored(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Non-LockOperation DoorLock events are ignored upstream of dispatch."""
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        with self._patch_users(matter_lock, []):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    event_id=3,  # LockOperationError
                    data={"credentials": [{"credentialType": 1, "credentialIndex": 1}]},
                ),
            )
            await hass.async_block_till_done()

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

    async def test_no_operation_type(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Event without ``lockOperationType`` fires with ``to_locked=None``.

        Matter spec permits unknown / vendor-specific operation types; the
        handler falls through to ``to_locked=None``
        rather than dropping the event so downstream automations still see
        the PIN use.
        """
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        with self._patch_users(
            matter_lock,
            [
                {
                    "user_index": 1,
                    "user_name": "lcm:3:Eve",
                    "credentials": [{"type": "pin", "index": 3}],
                },
            ],
        ):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    data={
                        "userIndex": 1,
                        "credentials": [
                            {"credentialType": 1, "credentialIndex": 3},
                        ],
                    }
                ),
            )
            await hass.async_block_till_done()

        assert len(fired) == 1
        assert fired[0]["code_slot"] == 3
        assert fired[0]["to_locked"] is None

    async def test_none_data_ignored(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Event with ``None`` data is ignored (no credentials)."""
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)

        matter_lock._on_node_event(None, _make_node_event(data=None))
        await hass.async_block_till_done()

        assert len(fired) == 0


# =============================================================================
# Event subscription lifecycle tests
# =============================================================================


class TestEventSubscription:
    """Test event subscription setup and teardown."""

    async def test_client_unresolvable_when_no_matter_data(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Resolution reports client unavailable when no Matter integration data.

        Unloading the Matter entry drops its loaded runtime data, so the
        owning-client lookup finds nothing even though the device exists.
        """
        for entry in hass.config_entries.async_entries("matter"):
            assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        with pytest.raises(LockDisconnected, match="Matter client unavailable"):
            matter_lock._require_client_and_node()

    def test_setup_push_idempotent(self, matter_lock_simple: MatterLock) -> None:
        """Test setup_push_subscription is a no-op if already subscribed."""
        matter_lock_simple._push_unsubs.append(lambda: None)  # already subscribed
        matter_lock_simple.setup_push_subscription()  # should be a no-op
        # If it tried to subscribe again, it would fail (no client)
        assert matter_lock_simple._push_unsubs

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

    def test_node_resolves(self, matter_lock: MatterLock) -> None:
        """Test node resolves from real Matter integration device."""
        _client, node = matter_lock._require_client_and_node()
        assert node is not None
        assert node.node_id == 16  # from mock_door_lock.json

    def test_resolution_with_multiple_fabrics_uses_device_owner(
        self, hass: HomeAssistant, matter_lock: MatterLock, matter_client: MagicMock
    ) -> None:
        """With more than one Matter fabric loaded, resolution follows the device's
        owning entry, not an arbitrary first entry (#1268).

        Adds a second loaded Matter entry whose client does not have this lock;
        resolution must still return the owning fabric's client and node.
        """
        other = MockConfigEntry(domain="matter", title="other fabric")
        other.add_to_hass(hass)
        other._async_set_state(hass, ConfigEntryState.LOADED, None)
        other.runtime_data = MagicMock()
        other.runtime_data.adapter.matter_client = MagicMock(name="other_fabric")

        client, node, _device = matter_lock._resolve()
        assert client is matter_client
        assert node is not None
        assert node.node_id == 16

    def test_owning_matter_client_reads_only_config_entry_id(
        self, matter_lock: MatterLock, matter_client: MagicMock
    ) -> None:
        """The owning entry comes from ``config_entry_id`` alone.

        ``DeviceEntry.primary_config_entry`` (and ``config_entries``) are
        deprecated shims that Home Assistant reports when a custom
        integration reads them; a device belongs to a single config entry.
        """
        device = matter_lock._fresh_device_entry()
        assert device is not None
        with (
            patch.object(
                dr.DeviceEntry,
                "primary_config_entry",
                new_callable=PropertyMock,
                side_effect=AssertionError("deprecated"),
            ),
            patch.object(
                dr.DeviceEntry,
                "config_entries",
                new_callable=PropertyMock,
                side_effect=AssertionError("deprecated"),
            ),
        ):
            assert matter_lock._owning_matter_client(device) is matter_client
            # A double carrying only the supported attribute resolves too.
            only_id = SimpleNamespace(config_entry_id=device.config_entry_id)
            assert matter_lock._owning_matter_client(only_id) is matter_client

    def test_owning_matter_client_none_for_non_matter_entry(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """A device whose entry is not a loaded Matter entry has no owning client."""
        other = MockConfigEntry(domain="zha")
        other.add_to_hass(hass)
        assert (
            matter_lock._owning_matter_client(
                SimpleNamespace(config_entry_id=other.entry_id)
            )
            is None
        )
        assert (
            matter_lock._owning_matter_client(SimpleNamespace(config_entry_id=None))
            is None
        )

    def test_match_node_resolves_via_real_get_device_id(
        self, matter_lock: MatterLock, matter_client: MagicMock
    ) -> None:
        """_match_node finds the node in the owning client using the real matcher (#1268)."""
        device = matter_lock._fresh_device_entry()
        node = matter_lock._match_node(matter_client, device)
        assert node is not None
        assert node.node_id == 16

    def test_node_matches_integration_resolver(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Drift guard: for a single fabric, LCM resolves the same node object as
        the Matter integration's own get_node_from_device_entry. If upstream
        changes its matching contract, this fails instead of the field silently
        going to LockDisconnected (#1268).
        """
        device = matter_lock._fresh_device_entry()
        _client, node = matter_lock._require_client_and_node()
        assert node is get_node_from_device_entry(hass, device)

    def test_require_client_and_node_not_ready_without_server_info(
        self, matter_lock: MatterLock, matter_client: MagicMock
    ) -> None:
        """A client with no server_info yet reports 'not ready', not 'node not found'.

        Mid-reconnect the client is present but cannot resolve any node; that
        transient must not be misdiagnosed as a device/fabric mismatch (#1268).
        """
        matter_client.server_info = None
        with pytest.raises(LockDisconnected, match="is not ready"):
            matter_lock._require_client_and_node()

    def test_fresh_device_entry_falls_back_when_entity_detached(
        self, matter_lock: MatterLock
    ) -> None:
        """If the live registry entry has no device_id, fall back to the lock's own
        device_id rather than stranding the lock (#1268 review)."""
        detached = MagicMock()
        detached.device_id = None
        with patch.object(matter_lock.ent_reg, "async_get", return_value=detached):
            device = matter_lock._fresh_device_entry()
        assert device is not None
        assert device.id == matter_lock.lock.device_id

    def test_client_from_integration(
        self, matter_lock: MatterLock, matter_client: MagicMock
    ) -> None:
        """Test resolution returns the client from the real integration."""
        client, _node = matter_lock._require_client_and_node()
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

    def test_client_unavailable_when_owning_client_is_none(
        self, matter_lock: MatterLock
    ) -> None:
        """Resolution reports client unavailable when the owning entry yields no client.

        The device exists, but its owning Matter entry exposes no usable
        ``matter_client`` (e.g. an adapter missing the attribute), so the
        resolution gate surfaces a disconnect rather than proceeding.
        """
        with (
            patch.object(matter_lock, "_owning_matter_client", return_value=None),
            pytest.raises(LockDisconnected, match="Matter client unavailable"),
        ):
            matter_lock._require_client_and_node()

    def test_setup_push_unresolvable_raises(
        self,
        matter_lock_simple: MatterLock,
    ) -> None:
        """setup_push_subscription raises when client/node can't be resolved.

        ``matter_lock_simple`` has no device, so neither the owning client nor
        the node resolves; no subscription is registered.
        """
        with pytest.raises(LockDisconnected):
            matter_lock_simple.setup_push_subscription()

        assert not matter_lock_simple._push_unsubs


# =============================================================================
# LockUserChange event tests
# =============================================================================


def _cleared_event(user_index: int, data_type: int = 2) -> MatterNodeEvent:
    """A LockUserChange CLEAR for ``user_index`` (a user record by default)."""
    return _make_node_event(
        event_id=4,
        data={
            "lockDataType": data_type,
            "dataOperationType": 1,
            "dataIndex": user_index,
            "userIndex": user_index,
        },
    )


class TestLockUserChangeEvent:
    """Test _handle_lock_user_change callback and coordinator push updates.

    These tests use ``matter_lock`` (the real Matter integration) so node-event
    dispatch resolves a live client+node and sends GetUser for the user an
    event names (``_raw_lock_user``); it never lists every user.
    """

    _patch_users = staticmethod(_patch_user_reads)

    async def test_pin_added_pushes_unknown(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Adding a PIN credential pushes SlotCredential.unreadable() to the slot the user owns.

        Resolution: the event's userIndex points at a user whose name carries
        ``lcm:<slot>:``; the push targets that LCM slot, NOT the credential
        index in dataIndex (which is now Matter-auto-allocated and opaque).
        """
        mock_coordinator = _mock_coordinator()
        mock_coordinator.data = {pin_address(3): SlotCredential.empty()}
        matter_lock.coordinator = mock_coordinator

        with self._patch_users(
            matter_lock,
            [
                {
                    "user_index": 6,
                    "user_name": "lcm:3:Carol",
                    "credentials": [{"type": "pin", "index": 7}],
                },
            ],
        ):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    event_id=4,
                    data={
                        "lockDataType": 6,  # PIN
                        "dataOperationType": 0,  # Add
                        "dataIndex": 7,  # Matter-auto-allocated credential index
                        "userIndex": 6,
                    },
                ),
            )
            await hass.async_block_till_done()

        mock_coordinator.observe_push.assert_called_once_with(
            pin_address(3), SlotCredential.unreadable()
        )

    async def test_pin_modified_pushes_unknown(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Modifying a PIN credential pushes SlotCredential.unreadable() to the LCM slot."""
        mock_coordinator = _mock_coordinator()
        mock_coordinator.data = {pin_address(5): SlotCredential.unreadable()}
        matter_lock.coordinator = mock_coordinator

        with self._patch_users(
            matter_lock,
            [
                {
                    "user_index": 4,
                    "user_name": "lcm:5:Bob",
                    "credentials": [{"type": "pin", "index": 2}],
                },
            ],
        ):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    event_id=4,
                    data={
                        "lockDataType": 6,  # PIN
                        "dataOperationType": 2,  # Modify
                        "dataIndex": 2,
                        "userIndex": 4,
                    },
                ),
            )
            await hass.async_block_till_done()

        mock_coordinator.observe_push.assert_called_once_with(
            pin_address(5), SlotCredential.unreadable()
        )

    async def test_pin_cleared_pushes_empty(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Clearing a PIN credential pushes SlotCredential.empty() to the LCM slot."""
        mock_coordinator = _mock_coordinator()
        mock_coordinator.data = {pin_address(2): SlotCredential.unreadable()}
        matter_lock.coordinator = mock_coordinator

        with self._patch_users(
            matter_lock,
            [
                {
                    "user_index": 8,
                    "user_name": "lcm:2:Alice",
                    "credentials": [],
                },
            ],
        ):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    event_id=4,
                    data={
                        "lockDataType": 6,  # PIN
                        "dataOperationType": 1,  # Clear
                        "dataIndex": 11,
                        "userIndex": 8,
                    },
                ),
            )
            await hass.async_block_till_done()

        mock_coordinator.observe_push.assert_called_once_with(
            pin_address(2), SlotCredential.empty()
        )

    async def test_pin_event_for_untagged_user_ignored(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Events on users without an ``lcm:<slot>:`` tag are ignored.

        Out-of-band PIN changes on non-LCM users (e.g. another controller's
        credentials, or keypad-enrolled credentials on a different user) must
        not push spurious updates into LCM's coordinator.
        """
        mock_coordinator = _mock_coordinator()
        matter_lock.coordinator = mock_coordinator

        with self._patch_users(
            matter_lock,
            [
                {
                    "user_index": 5,
                    "user_name": "External Alice",  # untagged
                    "credentials": [{"type": "pin", "index": 5}],
                },
            ],
        ):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    event_id=4,
                    data={
                        "lockDataType": 6,  # PIN
                        "dataOperationType": 0,  # Add
                        "dataIndex": 5,
                        "userIndex": 5,
                    },
                ),
            )
            await hass.async_block_till_done()

        mock_coordinator.observe_push.assert_not_called()

    async def test_pin_event_with_unknown_operation_ignored(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """LockUserChange with an unrecognized dataOperationType is dropped.

        Defends against a future Matter spec adding a data operation LCM
        doesn't model yet; we ignore rather than push a stale resolved
        state with the wrong semantics.
        """
        mock_coordinator = _mock_coordinator()
        matter_lock.coordinator = mock_coordinator

        matter_lock._on_node_event(
            None,
            _make_node_event(
                event_id=4,
                data={
                    "lockDataType": 6,  # PIN
                    "dataOperationType": 99,  # unknown
                    "dataIndex": 3,
                    "userIndex": 1,
                },
            ),
        )
        await hass.async_block_till_done()

        mock_coordinator.observe_push.assert_not_called()

    async def test_pin_event_with_missing_data_index_still_fires(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Events with no ``dataIndex`` still push if ``userIndex`` resolves.

        ``dataIndex`` (the Matter credential index) is no longer the LCM
        slot under the user-tag model; it's only kept for log context.
        Hard-rejecting events where ``dataIndex`` is absent or malformed
        would silently lose otherwise-resolvable state updates -- the LCM
        slot comes from ``userIndex`` -> tag.
        """
        mock_coordinator = _mock_coordinator()
        mock_coordinator.data = {pin_address(3): SlotCredential.empty()}
        matter_lock.coordinator = mock_coordinator

        with self._patch_users(
            matter_lock,
            [
                {
                    "user_index": 6,
                    "user_name": "lcm:3:Carol",
                    "credentials": [{"type": "pin", "index": 7}],
                },
            ],
        ):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    event_id=4,
                    data={
                        "lockDataType": 6,
                        "dataOperationType": 0,
                        # dataIndex omitted -- previously would hard-reject.
                        "userIndex": 6,
                    },
                ),
            )
            await hass.async_block_till_done()

        mock_coordinator.observe_push.assert_called_once_with(
            pin_address(3), SlotCredential.unreadable()
        )

    async def test_pin_event_dispatch_swallows_lock_disconnected(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """If the owner lookup fails (LockDisconnected), the dispatch logs and returns.

        The handler runs as a fire-and-forget task; a transport failure
        during the userIndex -> tag lookup must not propagate out of the
        task. The coordinator update is dropped; the next refresh will
        surface the change.
        """
        mock_coordinator = _mock_coordinator()
        matter_lock.coordinator = mock_coordinator

        with _patch_user_reads_failing(
            matter_lock, HomeAssistantError("transport down")
        ):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    event_id=4,
                    data={
                        "lockDataType": 6,
                        "dataOperationType": 0,
                        "dataIndex": 3,
                        "userIndex": 6,
                    },
                ),
            )
            await hass.async_block_till_done()

        mock_coordinator.observe_push.assert_not_called()

    async def test_pin_event_for_user_with_no_name_ignored(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """A nameless user-record yields no LCM tag -> ignore.

        Some Matter locks return user records with a missing or empty
        ``user_name`` field. There's no way to parse a tag without a name,
        so the event is dropped (untagged-user contract).
        """
        mock_coordinator = _mock_coordinator()
        matter_lock.coordinator = mock_coordinator

        with self._patch_users(
            matter_lock,
            [
                {
                    "user_index": 7,
                    "user_name": None,  # lock returned an empty/None name
                    "credentials": [{"type": "pin", "index": 2}],
                },
            ],
        ):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    event_id=4,
                    data={
                        "lockDataType": 6,
                        "dataOperationType": 0,
                        "dataIndex": 2,
                        "userIndex": 7,
                    },
                ),
            )
            await hass.async_block_till_done()

        mock_coordinator.observe_push.assert_not_called()

    def test_non_pin_data_type_ignored(self, matter_lock: MatterLock) -> None:
        """Non-PIN LockDataType (e.g. RFID=7) is ignored."""
        mock_coordinator = _mock_coordinator()
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

        mock_coordinator.observe_push.assert_not_called()

    async def test_pin_event_without_user_index_ignored(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """A LockUserChange with no ``userIndex`` is dropped, even when its
        ``dataIndex`` names a tagged user's credential.

        Unlike LockOperation, LockUserChange has no credential-index fallback:
        ``dataIndex`` is log context only, so the event resolves to no slot.
        Carol's user index equals her credential index, so reading
        ``dataIndex`` in place of the missing ``userIndex`` would resolve her.
        """
        mock_coordinator = _mock_coordinator()
        mock_coordinator.data = {pin_address(3): SlotCredential.empty()}
        matter_lock.coordinator = mock_coordinator

        with self._patch_users(
            matter_lock,
            [
                {
                    "user_index": 7,
                    "user_name": "lcm:3:Carol",
                    "credentials": [{"type": "pin", "index": 7}],
                },
            ],
        ):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    event_id=4,
                    data={
                        "lockDataType": 6,  # PIN
                        "dataOperationType": 0,  # Add
                        "dataIndex": 7,  # Carol's credential, but no userIndex
                    },
                ),
            )
            await hass.async_block_till_done()

        mock_coordinator.observe_push.assert_not_called()

    @pytest.mark.parametrize(
        "user_index", [0, -1, 11], ids=["zero", "negative", "past_the_lock_s"]
    )
    async def test_pin_event_for_a_user_index_the_lock_cannot_hold_reads_nothing(
        self, hass: HomeAssistant, matter_lock: MatterLock, user_index: int
    ) -> None:
        """
        A LockUserChange naming an index outside the lock's users names nobody.

        Matter numbers users from 1 to the Door Lock's
        NumberOfTotalUsersSupported, 10 on this node. The event is dropped
        without a read, even with the stand-in lock answering a tagged user
        at that index.
        """
        mock_coordinator = _mock_coordinator()
        mock_coordinator.data = {pin_address(3): SlotCredential.empty()}
        matter_lock.coordinator = mock_coordinator
        listing = AsyncMock(
            return_value={
                "max_users": 10,
                "users": [
                    {
                        "user_index": user_index,
                        "user_name": "lcm:3:Carol",
                        "credentials": [{"type": "pin", "index": 7}],
                    },
                ],
            }
        )

        client, _node = matter_lock._require_client_and_node()

        with (
            patch(f"{_PROVIDER_MODULE}.get_lock_users", listing),
            get_user_answered_from_listing(client),
        ):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    event_id=4,
                    data={
                        "lockDataType": 6,  # PIN
                        "dataOperationType": 0,  # Add
                        "dataIndex": 7,
                        "userIndex": user_index,
                    },
                ),
            )
            await hass.async_block_till_done()
            client.send_device_command.assert_not_awaited()

        listing.assert_not_awaited()
        mock_coordinator.observe_push.assert_not_called()

    async def test_a_lock_that_does_not_say_how_many_users_it_holds_holds_none(
        self,
        hass: HomeAssistant,
        matter_lock: MatterLock,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """
        Without NumberOfTotalUsersSupported, no index names a user: nothing is read.

        Home Assistant's ``get_lock_users`` lists no user on such a lock, so
        the single read agrees with it, even with the stand-in lock
        answering a tagged user at the index.
        """
        client, node = matter_lock._require_client_and_node()
        door_lock = node.endpoints[1].get_cluster(clusters.DoorLock)
        monkeypatch.setattr(door_lock, "numberOfTotalUsersSupported", None)
        mock_coordinator = _mock_coordinator()
        mock_coordinator.data = {pin_address(3): SlotCredential.empty()}
        matter_lock.coordinator = mock_coordinator

        with self._patch_users(
            matter_lock,
            [
                {
                    "user_index": 3,
                    "user_name": "lcm:3:Carol",
                    "credentials": [{"type": "pin", "index": 7}],
                },
            ],
        ):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    event_id=4,
                    data={
                        "lockDataType": 6,  # PIN
                        "dataOperationType": 0,  # Add
                        "dataIndex": 7,
                        "userIndex": 3,
                    },
                ),
            )
            await hass.async_block_till_done()
            client.send_device_command.assert_not_awaited()

        mock_coordinator.observe_push.assert_not_called()

    async def test_user_cleared_resolves_through_the_last_read(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Deleting the user empties the slot it anchored (issue #1538).

        Home Assistant's Manage access deletion sends ClearUser, which removes
        the user with its credentials, so the event cannot be resolved by
        reading the lock: the user is gone. The slot comes from what the last
        read said that user index anchored.
        """
        mock_coordinator = _mock_coordinator()
        matter_lock.coordinator = mock_coordinator
        with self._patch_users(
            matter_lock,
            [
                {
                    "user_index": 6,
                    "user_name": "lcm:3:Carol",
                    "credentials": [{"type": "pin", "index": 7}],
                }
            ],
        ):
            await matter_lock.async_get_users()

        with self._patch_users(matter_lock, []):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    event_id=4,
                    data={
                        "lockDataType": 2,  # a user record
                        "dataOperationType": 1,  # Clear
                        "dataIndex": 6,
                        "userIndex": 6,
                    },
                ),
            )
            await hass.async_block_till_done()

        mock_coordinator.observe_push.assert_called_once_with(
            pin_address(3), SlotCredential.empty()
        )
        # Forgotten once used: a later event for a reused index is not ours.
        with self._patch_users(matter_lock, []):
            matter_lock._on_node_event(None, _cleared_event(6))
            await hass.async_block_till_done()
        mock_coordinator.observe_push.assert_called_once()

    async def test_pin_cleared_for_a_vanished_user_resolves_through_the_last_read(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """The PIN-level CLEAR that follows a ClearUser resolves the same way."""
        mock_coordinator = _mock_coordinator()
        matter_lock.coordinator = mock_coordinator
        with self._patch_users(
            matter_lock,
            [
                {
                    "user_index": 8,
                    "user_name": "lcm:2:Alice",
                    "credentials": [{"type": "pin", "index": 11}],
                }
            ],
        ):
            await matter_lock.async_get_users()

        with self._patch_users(matter_lock, []):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    event_id=4,
                    data={
                        "lockDataType": 6,  # PIN
                        "dataOperationType": 1,  # Clear
                        "dataIndex": 11,
                        "userIndex": 8,
                    },
                ),
            )
            await hass.async_block_till_done()

        mock_coordinator.observe_push.assert_called_once_with(
            pin_address(2), SlotCredential.empty()
        )

    @pytest.mark.parametrize("operation", [0, 2], ids=["add", "modify"])
    async def test_user_record_added_or_renamed_is_ignored(
        self, hass: HomeAssistant, matter_lock: MatterLock, operation: int
    ) -> None:
        """A user record appearing or changing says nothing about its PIN."""
        mock_coordinator = _mock_coordinator()
        matter_lock.coordinator = mock_coordinator
        with self._patch_users(
            matter_lock,
            [
                {
                    "user_index": 6,
                    "user_name": "lcm:3:Carol",
                    "credentials": [{"type": "pin", "index": 7}],
                }
            ],
        ):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    event_id=4,
                    data={
                        "lockDataType": 2,
                        "dataOperationType": operation,
                        "dataIndex": 6,
                        "userIndex": 6,
                    },
                ),
            )
            await hass.async_block_till_done()

        mock_coordinator.observe_push.assert_not_called()

    async def test_pin_added_to_a_vanished_user_is_not_resolved_from_the_map(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Only a CLEAR may resolve through the last read.

        An add or modify for a user the lock no longer lists cannot be
        believed: resolving it through the map would show a deleted user's
        slot as occupied.
        """
        mock_coordinator = _mock_coordinator()
        matter_lock.coordinator = mock_coordinator
        with self._patch_users(
            matter_lock,
            [
                {
                    "user_index": 6,
                    "user_name": "lcm:3:Carol",
                    "credentials": [{"type": "pin", "index": 7}],
                }
            ],
        ):
            await matter_lock.async_get_users()

        with self._patch_users(matter_lock, []):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    event_id=4,
                    data={
                        "lockDataType": 6,
                        "dataOperationType": 0,  # Add
                        "dataIndex": 7,
                        "userIndex": 6,
                    },
                ),
            )
            await hass.async_block_till_done()

        mock_coordinator.observe_push.assert_not_called()
        # ...and the anchor is still there for the deletion that follows.
        with self._patch_users(matter_lock, []):
            matter_lock._on_node_event(None, _cleared_event(6))
            await hass.async_block_till_done()
        mock_coordinator.observe_push.assert_called_once_with(
            pin_address(3), SlotCredential.empty()
        )

    async def test_a_read_without_the_user_keeps_its_anchor_for_the_clear_that_follows(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """A read that no longer lists the user must not swallow its deletion event.

        Reads run for every write on any slot; one landing between the
        out-of-band deletion and its event must leave the anchor in place.
        """
        mock_coordinator = _mock_coordinator()
        matter_lock.coordinator = mock_coordinator
        with self._patch_users(
            matter_lock,
            [
                {
                    "user_index": 6,
                    "user_name": "lcm:3:Carol",
                    "credentials": [{"type": "pin", "index": 7}],
                }
            ],
        ):
            await matter_lock.async_get_users()
        with self._patch_users(matter_lock, []):
            await matter_lock.async_get_users()  # the user is already gone
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    event_id=4,
                    data={
                        "lockDataType": 2,
                        "dataOperationType": 1,
                        "dataIndex": 6,
                        "userIndex": 6,
                    },
                ),
            )
            await hass.async_block_till_done()

        mock_coordinator.observe_push.assert_called_once_with(
            pin_address(3), SlotCredential.empty()
        )

    async def test_a_read_showing_the_user_renamed_off_lcm_drops_its_anchor(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """A user someone renamed to an untagged name is no longer ours to empty."""
        mock_coordinator = _mock_coordinator()
        matter_lock.coordinator = mock_coordinator
        with self._patch_users(
            matter_lock,
            [
                {
                    "user_index": 6,
                    "user_name": "lcm:3:Carol",
                    "credentials": [{"type": "pin", "index": 7}],
                }
            ],
        ):
            await matter_lock.async_get_users()
        with self._patch_users(
            matter_lock,
            [
                {
                    "user_index": 6,
                    "user_name": "Carol at the vendor app",
                    "credentials": [{"type": "pin", "index": 7}],
                }
            ],
        ):
            await matter_lock.async_get_users()
        with self._patch_users(matter_lock, []):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    event_id=4,
                    data={
                        "lockDataType": 2,
                        "dataOperationType": 1,
                        "dataIndex": 6,
                        "userIndex": 6,
                    },
                ),
            )
            await hass.async_block_till_done()

        mock_coordinator.observe_push.assert_not_called()

    async def test_clear_for_a_user_index_never_read_is_ignored(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """A cleared user LCM never saw anchors no slot of ours."""
        mock_coordinator = _mock_coordinator()
        matter_lock.coordinator = mock_coordinator
        with self._patch_users(matter_lock, []):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    event_id=4,
                    data={
                        "lockDataType": 2,
                        "dataOperationType": 1,
                        "dataIndex": 10,
                        "userIndex": 10,
                    },
                ),
            )
            await hass.async_block_till_done()

        mock_coordinator.observe_push.assert_not_called()

    async def test_writes_and_deletes_keep_the_anchor_map_current(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """A user LCM creates is known before any read; a user it deletes is forgotten."""
        mock_coordinator = _mock_coordinator()
        matter_lock.coordinator = mock_coordinator
        with (
            self._patch_users(matter_lock, []),
            patch(
                f"{_PROVIDER_MODULE}.set_lock_user",
                AsyncMock(return_value={"user_index": 9}),
            ),
        ):
            await matter_lock.async_set_user(User(user_id=3, name="lcm:3:Carol"))
            matter_lock._on_node_event(None, _cleared_event(9))
            await hass.async_block_till_done()
        mock_coordinator.observe_push.assert_called_once_with(
            pin_address(3), SlotCredential.empty()
        )

        with (
            self._patch_users(matter_lock, []),
            patch(
                f"{_PROVIDER_MODULE}.set_lock_user",
                AsyncMock(return_value={"user_index": 9}),
            ),
            patch(f"{_PROVIDER_MODULE}.clear_lock_user", AsyncMock(return_value=None)),
        ):
            await matter_lock.async_set_user(User(user_id=3, name="lcm:3:Carol"))
            await matter_lock.async_delete_user(9)
            matter_lock._on_node_event(None, _cleared_event(9))
            await hass.async_block_till_done()
        mock_coordinator.observe_push.assert_called_once()  # no second resolution

    async def test_adopting_an_untagged_user_records_the_index_it_anchors(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Legacy adoption is a write, not a read: the map learns the index from it."""
        mock_coordinator = _mock_coordinator()
        matter_lock.coordinator = mock_coordinator
        untagged_holder = [
            {
                "user_index": 5,
                "user_name": "Legacy Carol",
                "credentials": [{"type": "pin", "index": 3}],
            }
        ]
        with (
            self._patch_users(matter_lock, untagged_holder),
            patch(
                f"{_PROVIDER_MODULE}.set_lock_user",
                AsyncMock(return_value={"user_index": 5}),
            ),
        ):
            await matter_lock.async_set_user(User(user_id=3, name="lcm:3:Carol"))
        with self._patch_users(matter_lock, []):
            matter_lock._on_node_event(None, _cleared_event(5))
            await hass.async_block_till_done()
        mock_coordinator.observe_push.assert_called_once_with(
            pin_address(3), SlotCredential.empty()
        )

    async def test_a_read_that_fails_during_a_clear_still_resolves_from_the_map(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """A flaky read at the moment of deletion must not lose the deletion."""
        mock_coordinator = _mock_coordinator()
        matter_lock.coordinator = mock_coordinator
        with self._patch_users(
            matter_lock,
            [
                {
                    "user_index": 6,
                    "user_name": "lcm:3:Carol",
                    "credentials": [{"type": "pin", "index": 7}],
                }
            ],
        ):
            await matter_lock.async_get_users()
        with _patch_user_reads_failing(matter_lock, HomeAssistantError("offline")):
            matter_lock._on_node_event(None, _cleared_event(6))
            await hass.async_block_till_done()
        mock_coordinator.observe_push.assert_called_once_with(
            pin_address(3), SlotCredential.empty()
        )

    async def test_a_user_now_untagged_on_the_lock_is_not_ours_to_empty(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """The lock reusing an index for a vendor-app user drops the old anchor.

        Without this the vendor user's own later deletion would empty the
        slot the index anchored for LCM long ago.
        """
        mock_coordinator = _mock_coordinator()
        matter_lock.coordinator = mock_coordinator
        with self._patch_users(
            matter_lock,
            [
                {
                    "user_index": 6,
                    "user_name": "lcm:3:Carol",
                    "credentials": [{"type": "pin", "index": 7}],
                }
            ],
        ):
            await matter_lock.async_get_users()
        vendor_user = [
            {"user_index": 6, "user_name": "Vendor app user", "credentials": []}
        ]
        with self._patch_users(matter_lock, vendor_user):
            matter_lock._on_node_event(None, _cleared_event(6, data_type=6))
            await hass.async_block_till_done()
        mock_coordinator.observe_push.assert_not_called()
        # And the anchor is gone: the vendor user's own deletion resolves nothing.
        with self._patch_users(matter_lock, []):
            matter_lock._on_node_event(None, _cleared_event(6))
            await hass.async_block_till_done()
        mock_coordinator.observe_push.assert_not_called()

    async def test_the_lock_as_read_now_beats_what_the_map_remembers(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """A user re-tagged to another slot since the last read empties that slot."""
        mock_coordinator = _mock_coordinator()
        matter_lock.coordinator = mock_coordinator
        with self._patch_users(
            matter_lock,
            [
                {
                    "user_index": 6,
                    "user_name": "lcm:3:Carol",
                    "credentials": [{"type": "pin", "index": 7}],
                }
            ],
        ):
            await matter_lock.async_get_users()
        with self._patch_users(
            matter_lock,
            [{"user_index": 6, "user_name": "lcm:4:Carol", "credentials": []}],
        ):
            matter_lock._on_node_event(None, _cleared_event(6, data_type=6))
            await hass.async_block_till_done()
        mock_coordinator.observe_push.assert_called_once_with(
            pin_address(4), SlotCredential.empty()
        )


# =============================================================================
# supports_native_users tests
# =============================================================================


async def test_supports_native_users(matter_lock: MatterLock) -> None:
    """Test that Matter locks report native user support."""
    assert matter_lock.supports_native_users is True


async def test_credential_index_does_not_follow_slot(matter_lock: MatterLock) -> None:
    """
    Matter opts out of the slot capacity check (issue #1398).

    ``async_set_credential`` lets the lock allocate the credential index, so a
    slot number above the lock's credential count is legal here and must not be
    range-checked the way the Z-Wave path is.
    """
    assert matter_lock.credential_index_follows_slot is False


# =============================================================================
# async_get_users tests
# =============================================================================


class TestGetUsers:
    """Test async_get_users projects lock users onto User domain objects."""

    async def test_get_users_empty(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Returns empty list when lock has no users."""
        mock_get_lock_users = AsyncMock(return_value={"max_users": 10, "users": []})
        with (
            patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users),
        ):
            users = await matter_lock.async_get_users()
        assert users == []

    async def test_get_users_skips_raw_user_without_user_index(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """A raw lock user with no user_index is skipped (cannot be projected)."""
        mock_get_lock_users = AsyncMock(
            return_value={
                "max_users": 10,
                "users": [
                    {"user_index": None, "credentials": [{"type": "pin", "index": 1}]},
                    {
                        "user_index": 2,
                        "user_name": "Bob",
                        "credentials": [{"type": "pin", "index": 2}],
                    },
                ],
            }
        )
        with (
            patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users),
        ):
            users = await matter_lock.async_get_users()

        assert [user.user_id for user in users] == [2]

    async def test_get_users_pin_credentials_become_unreadable(
        self, hass: HomeAssistant, matter_lock: MatterLock
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
            patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users),
        ):
            users = await matter_lock.async_get_users()

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

    async def test_get_users_projects_lcm_slot_from_tag_when_credential_index_differs(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Tagged users carry their LCM slot in the name; Credential.slot is the LCM slot.

        Regression for #1239 review. Under the new model the Matter credential
        index is auto-allocated and no longer pinned to the LCM slot. The
        projection must surface the LCM slot from the ``lcm:<slot>:`` tag --
        otherwise the base seam's ``_project_users_to_slots`` (which keys
        by ``credential.slot``) would attribute the credential to the wrong
        LCM slot.
        """
        mock_get_lock_users = AsyncMock(
            return_value={
                "max_users": 10,
                "users": [
                    {
                        "user_index": 42,
                        "user_name": "lcm:5:Alice",
                        # Matter-auto-allocated credential index 7, LCM slot 5.
                        "credentials": [{"type": "pin", "index": 7}],
                    },
                ],
            }
        )
        with (
            patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users),
        ):
            users = await matter_lock.async_get_users()

        assert len(users) == 1
        cred = users[0].credentials[0]
        assert cred.slot == 5

    async def test_get_users_untagged_user_falls_back_to_credential_index(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Untagged users keep ``credential_index == slot`` (the legacy invariant).

        Pre-PR-B Matter LCM pinned ``credential_index`` to the LCM slot, so
        untagged users (legacy LCM 2.0 installs that haven't migrated) get
        their credential surfaced with ``Credential.slot = credential_index``
        -- the projection is identity for them, which preserves continuity.
        """
        mock_get_lock_users = AsyncMock(
            return_value={
                "max_users": 10,
                "users": [
                    {
                        "user_index": 1,
                        "user_name": "Alice",  # untagged, legacy LCM 2.0
                        "credentials": [{"type": "pin", "index": 5}],
                    },
                ],
            }
        )
        with (
            patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users),
        ):
            users = await matter_lock.async_get_users()

        assert len(users) == 1
        cred = users[0].credentials[0]
        assert cred.slot == 5

    async def test_scoped_read_surfaces_a_user_created_on_the_keypad(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """A wide scope still reports users this integration never wrote.

        Matter is the one provider where the slot number is not the device's
        credential index, so what comes back is the Lock Code Manager slot --
        recovered from the tag, or the credential index itself for an
        untagged user, which is exactly what a code added on the keypad looks
        like. Allocation knows not to read a device index into that; it still
        must not hand the number out twice.
        """
        mock_get_lock_users = AsyncMock(
            return_value={
                "max_users": 10,
                "users": [
                    {
                        "user_index": 1,
                        "user_name": "Added at the door",
                        "credentials": [{"type": "pin", "index": 4}],
                    },
                ],
            }
        )
        with patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users):
            codes = await matter_lock.async_get_usercodes(range(1, 6))

        assert codes[4].is_present
        assert codes[1].is_empty

    async def test_get_users_non_pin_credentials_excluded(
        self, hass: HomeAssistant, matter_lock: MatterLock
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
            patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users),
        ):
            users = await matter_lock.async_get_users()

        assert len(users) == 1
        user = users[0]
        assert len(user.credentials) == 1
        assert user.credentials[0].type is CredentialType.PIN

    async def test_get_users_multiple_users(
        self, hass: HomeAssistant, matter_lock: MatterLock
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
            patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users),
        ):
            users = await matter_lock.async_get_users()

        assert len(users) == 2
        assert users[0].user_id == 1
        assert users[1].user_id == 3

    async def test_get_users_service_validation_error(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """ServiceValidationError from get_lock_users raises LockOperationFailed."""
        mock_get_lock_users = AsyncMock(side_effect=ServiceValidationError("bad input"))
        with (
            patch(f"{_PROVIDER_MODULE}.get_lock_users", mock_get_lock_users),
            pytest.raises(LockOperationFailed, match="rejected input"),
        ):
            await matter_lock.async_get_users()


# =============================================================================
# async_get_capabilities tests
# =============================================================================


class TestGetCapabilities:
    """Test async_get_capabilities maps lock_info to LockCapabilities."""

    async def test_get_capabilities_full(
        self, hass: HomeAssistant, matter_lock: MatterLock
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
            patch(f"{_PROVIDER_MODULE}.get_lock_info", mock_get_lock_info),
        ):
            caps = await matter_lock.async_get_capabilities()

        assert isinstance(caps, LockCapabilities)
        assert caps.supports_user_management is True
        assert caps.max_users == 20
        # 9, not the Matter maximum of 10: the Aqara U400 fails GetUser for a
        # name of exactly 10.
        assert caps.max_user_name_length == 9
        pin_cap = caps.capability_for(CredentialType.PIN)
        assert pin_cap is not None
        assert pin_cap.num_slots == 15
        assert pin_cap.min_length == 4
        assert pin_cap.max_length == 8
        assert pin_cap.supports_learn is False

    async def test_tagged_name_stops_short_of_ten_characters(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """
        A label is cut so the user's name is 9 characters, never 10.

        An Aqara U400 accepts a 10-character name but then fails every
        GetUser walk while that user exists, so reading the lock's users and
        releasing a slot both fail until the user is cleared by hand. The
        canonical tag survives the cut: it is how the slot is found again.
        """
        mock_get_lock_info = AsyncMock(
            return_value={
                "supports_user_management": True,
                "supported_credential_types": ["pin"],
                "max_users": 20,
                "max_pin_users": 15,
            }
        )
        with patch(f"{_PROVIDER_MODULE}.get_lock_info", mock_get_lock_info):
            name = await matter_lock._build_tagged_user_name(6, "Abcdef")

        assert name == "lcm:6:Abc"

    @pytest.mark.parametrize(
        ("slot", "display", "expected"),
        [
            # "lcm:1:Zoë" is 9 characters but 10 bytes: the U400's wedge.
            pytest.param(1, "Zoë", "lcm:1:Zo", id="two-byte"),
            pytest.param(6, "日本", "lcm:6:日", id="three-byte-fits"),
            pytest.param(123, "ë", "lcm:123:", id="no-room-for-the-whole-char"),
        ],
    )
    async def test_tagged_name_stops_short_of_ten_bytes(
        self,
        hass: HomeAssistant,
        matter_lock: MatterLock,
        slot: int,
        display: str,
        expected: str,
    ) -> None:
        """
        Matter limits a user's name in UTF-8 bytes, so a label is cut to 9 of them.

        Never through the middle of a character, and never into the tag.
        """
        mock_get_lock_info = AsyncMock(
            return_value={
                "supports_user_management": True,
                "supported_credential_types": ["pin"],
                "max_users": 20,
                "max_pin_users": 15,
            }
        )
        with patch(f"{_PROVIDER_MODULE}.get_lock_info", mock_get_lock_info):
            name = await matter_lock._build_tagged_user_name(slot, display)

        assert name == expected
        assert len(name.encode()) <= 9
        assert parse_tag(name)[0] == slot
        assert make_tagged_name(slot, display).startswith(name)

    async def test_get_capabilities_none_fields_default_to_zero(
        self, hass: HomeAssistant, matter_lock: MatterLock
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
            patch(f"{_PROVIDER_MODULE}.get_lock_info", mock_get_lock_info),
        ):
            caps = await matter_lock.async_get_capabilities()

        assert caps.max_users == 0
        pin_cap = caps.capability_for(CredentialType.PIN)
        assert pin_cap is not None
        assert pin_cap.num_slots == 0
        assert pin_cap.min_length == 0
        assert pin_cap.max_length == 0

    async def test_get_capabilities_advertises_every_kind_the_lock_supports(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """
        Every kind in the feature map is advertised, with what limits it has.

        RFID has capacity and length attributes; a finger and a face have
        none, so theirs are unknown. A string that is no ``CredentialType``
        is not a kind this integration can name, and is left out.
        """
        mock_get_lock_info = AsyncMock(
            return_value={
                "supports_user_management": True,
                "supported_credential_types": [
                    "pin",
                    "rfid",
                    "fingerprint",
                    "face",
                    "finger_vein",
                ],
                "max_users": 20,
                "max_pin_users": 15,
                "min_pin_length": 4,
                "max_pin_length": 8,
                "max_rfid_users": 12,
                "min_rfid_length": 10,
                "max_rfid_length": 74,
            }
        )
        with patch(f"{_PROVIDER_MODULE}.get_lock_info", mock_get_lock_info):
            caps = await matter_lock.async_get_capabilities()

        assert dict(caps.credential_types) == {
            CredentialType.PIN: CredentialTypeCapability(
                num_slots=15, min_length=4, max_length=8, supports_learn=False
            ),
            CredentialType.RFID: CredentialTypeCapability(
                num_slots=12, min_length=10, max_length=74, supports_learn=False
            ),
            CredentialType.FINGERPRINT: CredentialTypeCapability(
                num_slots=0, min_length=0, max_length=0, supports_learn=False
            ),
            CredentialType.FACE: CredentialTypeCapability(
                num_slots=0, min_length=0, max_length=0, supports_learn=False
            ),
        }

    async def test_get_capabilities_no_pin_support_advertises_no_pin(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """A lock with a card reader and no keypad advertises the reader alone."""
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
            patch(f"{_PROVIDER_MODULE}.get_lock_info", mock_get_lock_info),
        ):
            caps = await matter_lock.async_get_capabilities()

        assert caps.capability_for(CredentialType.PIN) is None
        assert not caps.supports(CredentialType.PIN)
        assert list(caps.credential_types) == [CredentialType.RFID]

    async def test_get_capabilities_disconnected(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """HomeAssistantError from get_lock_info raises LockDisconnected."""
        mock_get_lock_info = AsyncMock(side_effect=HomeAssistantError("offline"))
        with (
            patch(f"{_PROVIDER_MODULE}.get_lock_info", mock_get_lock_info),
            pytest.raises(LockDisconnected),
        ):
            await matter_lock.async_get_capabilities()

    async def test_get_capabilities_service_validation_error(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """ServiceValidationError from get_lock_info raises LockOperationFailed."""
        mock_get_lock_info = AsyncMock(side_effect=ServiceValidationError("bad input"))
        with (
            patch(f"{_PROVIDER_MODULE}.get_lock_info", mock_get_lock_info),
            pytest.raises(LockOperationFailed, match="rejected input"),
        ):
            await matter_lock.async_get_capabilities()

    async def test_get_capabilities_client_exception(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """``MatterClientException`` from get_lock_info raises LockDisconnected.

        Independent of ``HomeAssistantError`` (issue #1257): a startup
        ``InvalidState: Not connected`` must route to retry, not escape
        the read site and suspend the slot.
        """
        mock_get_lock_info = AsyncMock(
            side_effect=MatterClientException("Not connected")
        )
        with (
            patch(f"{_PROVIDER_MODULE}.get_lock_info", mock_get_lock_info),
            pytest.raises(LockDisconnected, match="get_lock_info failed"),
        ):
            await matter_lock.async_get_capabilities()


# =============================================================================
# async_set_user tests
# =============================================================================


class TestSetUser:
    """async_set_user find-or-create-by-tag (with legacy adoption fallback)."""

    def _patch_users(self, users: list[dict]) -> Any:
        """Patch get_lock_users to return the given user list."""
        return patch(
            f"{_PROVIDER_MODULE}.get_lock_users",
            AsyncMock(return_value={"max_users": 10, "users": users}),
        )

    async def test_set_user_creates_when_no_tagged_or_legacy_user(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """No LCM-owned user exists for the slot -> auto-allocate."""
        mock_set_user = AsyncMock(return_value={"user_index": 1})
        # Seam passes user.name already tagged.
        user = User(user_id=1, name="lcm:1:Alice")
        with (
            self._patch_users([]),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", mock_set_user),
        ):
            result = await matter_lock.async_set_user(user)

        assert result == SetUserResult(user_id=1, created=True)
        call_kwargs = mock_set_user.call_args.kwargs
        assert call_kwargs["user_index"] is None  # auto-allocate
        assert call_kwargs["user_name"] == "lcm:1:Alice"

    async def test_set_user_updates_when_tagged_user_already_exists(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """An lcm:<slot>: tagged user is found -> UPDATE that user_index."""
        mock_set_user = AsyncMock(return_value={"user_index": 42})
        user = User(user_id=2, name="lcm:2:Bob-rename")
        with (
            self._patch_users(
                [
                    {
                        "user_index": 42,
                        "user_name": "lcm:2:Bob",
                        "credentials": [{"type": "pin", "index": 7}],
                    },
                ]
            ),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", mock_set_user),
        ):
            result = await matter_lock.async_set_user(user)

        assert result == SetUserResult(user_id=42, created=False)
        call_kwargs = mock_set_user.call_args.kwargs
        assert call_kwargs["user_index"] == 42
        assert call_kwargs["user_name"] == "lcm:2:Bob-rename"

    async def test_set_user_falls_back_to_user_id_when_name_is_untagged(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """``_slot_from_seam_user`` falls back to user.user_id for untagged names.

        Defensive path -- the seam always passes a tagged name, but a
        direct caller (test setup, future refactor) might pass a User
        with an untagged name. The helper uses user.user_id as the slot
        in that case so the rest of the logic still has a slot to work
        with.
        """
        mock_set_user = AsyncMock(return_value={"user_index": 5})
        # user.user_id = 9 is the LCM slot fallback; name has no lcm:<slot>: tag.
        user = User(user_id=9, name="Unparseable name")
        with (
            self._patch_users([]),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", mock_set_user),
        ):
            result = await matter_lock.async_set_user(user)

        # CREATE path runs because no canonical/legacy match for slot 9.
        # The verbatim un-tagged name flows through to set_lock_user.
        assert result == SetUserResult(user_id=5, created=True)
        call_kwargs = mock_set_user.call_args.kwargs
        assert call_kwargs["user_index"] is None
        assert call_kwargs["user_name"] == "Unparseable name"

    async def test_set_user_legacy_pass_skips_users_tagged_for_other_slots(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """A tagged user whose Matter credential index happens to equal our slot is NOT adopted.

        Regression for #1239 review: under the new model the Matter credential
        index is auto-allocated. A user tagged ``lcm:3:`` can legitimately
        own a PIN at credential_index 7. ``_find_user_index_for_slot(7)``
        must NOT match that user via the legacy fallback (it belongs to
        slot 3) -- doing so would cause us to rename slot-3's user and
        write slot-7's PIN onto slot-3's user record.
        """
        mock_set_user = AsyncMock(return_value={"user_index": 100})
        user = User(user_id=7, name="lcm:7:Eve")
        with (
            self._patch_users(
                [
                    # User tagged for slot 3, but its PIN credential happens
                    # to live at Matter-auto-allocated index 7. Must not be
                    # adopted as slot 7's anchor.
                    {
                        "user_index": 42,
                        "user_name": "lcm:3:Alice",
                        "credentials": [{"type": "pin", "index": 7}],
                    },
                ]
            ),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", mock_set_user),
        ):
            result = await matter_lock.async_set_user(user)

        # CREATE (auto-allocate) because no canonical match for slot 7
        # and the legacy pass correctly skipped the slot-3 user.
        assert result == SetUserResult(user_id=100, created=True)
        call_kwargs = mock_set_user.call_args.kwargs
        assert call_kwargs["user_index"] is None
        assert call_kwargs["user_name"] == "lcm:7:Eve"

    async def test_set_user_create_auto_allocates_and_returns_allocated_index(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """set_lock_user returns the Matter-assigned user_index on CREATE."""
        mock_set_user = AsyncMock(return_value={"user_index": 5})
        user = User(user_id=3, name="lcm:3:Eve")
        with (
            self._patch_users([]),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", mock_set_user),
        ):
            result = await matter_lock.async_set_user(user)

        # The LCM slot is 3 but Matter allocated user_index=5; the
        # provider returns the lock-side identifier so the seam can
        # thread it through to async_set_credential.
        assert result.user_id == 5
        assert result.created is True

    async def test_set_user_create_raises_lock_disconnected_on_ha_error(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """HomeAssistantError from set_lock_user on CREATE -> LockDisconnected."""
        mock_set_user = AsyncMock(side_effect=HomeAssistantError("offline"))
        user = User(user_id=1, name="lcm:1:Alice")
        with (
            self._patch_users([]),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", mock_set_user),
            pytest.raises(LockDisconnected),
        ):
            await matter_lock.async_set_user(user)

    async def test_set_user_create_raises_operation_failed_on_validation(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """ServiceValidationError from set_lock_user on CREATE -> LockOperationFailed."""
        mock_set_user = AsyncMock(side_effect=ServiceValidationError("bad name"))
        user = User(user_id=1, name="lcm:1:Mallory")
        with (
            self._patch_users([]),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", mock_set_user),
            pytest.raises(LockOperationFailed, match="rejected input"),
        ):
            await matter_lock.async_set_user(user)

    async def test_set_user_update_tolerates_name_set_failure(
        self, hass: HomeAssistant, matter_lock: MatterLock, caplog
    ) -> None:
        """UPDATE name-set failure is logged; the user_index is still returned.

        Preserves the historical contract from PR #1077 -- the DoorLock
        SetUser command on an existing user is a metadata-only update,
        and a transient 500 or a rejected name should not block the
        subsequent credential write.

        ``HomeAssistantError`` short-circuits the cascade (the helper
        raises ``LockDisconnected`` immediately because transport
        failures aren't charset-recoverable); UPDATE swallows that
        per the historical contract.
        """
        mock_set_user = AsyncMock(side_effect=HomeAssistantError("500"))
        user = User(user_id=2, name="lcm:2:Updated Name")
        with (
            self._patch_users(
                [
                    {
                        "user_index": 7,
                        "user_name": "lcm:2:Original",
                        "credentials": [{"type": "pin", "index": 3}],
                    },
                ]
            ),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", mock_set_user),
        ):
            result = await matter_lock.async_set_user(user)

        assert result == SetUserResult(user_id=7, created=False)
        # HomeAssistantError short-circuits the cascade -- only one
        # attempt is made before LockDisconnected is raised + tolerated.
        assert mock_set_user.call_count == 1
        assert "failed to update user name" in caplog.text

    async def test_set_user_update_tolerates_matter_sdk_error(
        self, hass: HomeAssistant, matter_lock: MatterLock, caplog
    ) -> None:
        """UPDATE swallows ``matter_server.common.errors.MatterError``.

        Production regression: ``set_lock_user`` on an existing user
        sometimes throws ``UnknownError: InteractionModelError:
        InvalidCommand (0x85)`` (a ``MatterError`` subclass that is NOT a
        ``HomeAssistantError`` subclass). The historical contract is to
        tolerate any name-set failure on UPDATE so the credential write
        still proceeds; the old code only caught ``HomeAssistantError``
        and let MatterError bubble past, causing the seam's catchall to
        spuriously suspend the slot and create a repair issue.
        """
        # All cascade candidates (canonical, compact, slot-only) raise
        # the same MatterError here; the test exercises the
        # "all candidates fail -> tolerate" branch for a MatterError
        # specifically. A dedicated test
        # (test_set_user_update_falls_back_to_compact_on_charset_rejection)
        # covers the success-on-fallback case.
        mock_set_user = AsyncMock(
            side_effect=UnknownError("InteractionModelError: InvalidCommand (0x85)")
        )
        user = User(user_id=2, name="lcm:2:Updated Name")
        with (
            self._patch_users(
                [
                    {
                        "user_index": 7,
                        "user_name": "lcm:2:Original",
                        "credentials": [{"type": "pin", "index": 3}],
                    },
                ]
            ),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", mock_set_user),
        ):
            result = await matter_lock.async_set_user(user)

        # The user_index is still returned so the seam can route the
        # subsequent credential write to the right user; only the name
        # update was dropped.
        assert result == SetUserResult(user_id=7, created=False)
        # MatterError falls through all 3 candidates: canonical -> compact -> slot-only.
        assert mock_set_user.call_count == 3
        assert "failed to update user name" in caplog.text
        assert "InvalidCommand (0x85)" in caplog.text

    async def test_set_user_create_falls_back_to_compact_on_charset_rejection(
        self, hass: HomeAssistant, matter_lock: MatterLock, caplog
    ) -> None:
        """CREATE retries with the compact ``lcm<slot>`` fallback on canonical rejection.

        Some Matter lock firmwares restrict ``userName`` to alphanumeric
        and reject the colons in ``lcm:<slot>:``. The cascade tries the
        compact ``lcm<slot>`` form first (alphanumeric AND keeps the
        ownership marker) before falling all the way back to bare
        ``str(slot)``.
        """
        # First call (canonical) fails; second call (compact) succeeds.
        mock_set_user = AsyncMock(
            side_effect=[
                UnknownError("InteractionModelError: InvalidCommand (0x85)"),
                {"user_index": 4},
            ]
        )
        user = User(user_id=1, name="lcm:1:Alice")
        # The success-on-fallback diagnostic log is at DEBUG (intentionally
        # quiet for locks that always need the fallback). Enable DEBUG
        # capture for this provider so the test asserts on the message
        # without depending on global pytest logging configuration.
        caplog.set_level(logging.DEBUG, logger=_PROVIDER_MODULE)
        with (
            self._patch_users([]),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", mock_set_user),
        ):
            result = await matter_lock.async_set_user(user)

        assert result == SetUserResult(user_id=4, created=True)
        assert mock_set_user.call_count == 2
        first_call_kwargs = mock_set_user.call_args_list[0].kwargs
        second_call_kwargs = mock_set_user.call_args_list[1].kwargs
        assert first_call_kwargs["user_name"] == "lcm:1:Alice"
        # Compact tier wins; we don't fall all the way to slot-only.
        assert second_call_kwargs["user_name"] == "lcm1"
        assert "rejected" in caplog.text and "lcm:1:Alice" in caplog.text

    async def test_set_user_create_falls_back_to_slot_only_when_compact_also_rejected(
        self, hass: HomeAssistant, matter_lock: MatterLock, caplog
    ) -> None:
        """When the compact tier also fails, the cascade falls through to slot-only.

        A lock that rejects both the colons and the ``lcm`` prefix
        characters (very restrictive firmware) still gets a working
        write via the bare digit fallback. The slot binding is still
        recoverable via the tolerant parser's digit-only arm.
        """
        mock_set_user = AsyncMock(
            side_effect=[
                UnknownError("colons rejected"),
                UnknownError("lcm prefix rejected"),
                {"user_index": 4},
            ]
        )
        user = User(user_id=1, name="lcm:1:Alice")
        caplog.set_level(logging.DEBUG, logger=_PROVIDER_MODULE)
        with (
            self._patch_users([]),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", mock_set_user),
        ):
            result = await matter_lock.async_set_user(user)

        assert result == SetUserResult(user_id=4, created=True)
        assert mock_set_user.call_count == 3
        names_tried = [c.kwargs["user_name"] for c in mock_set_user.call_args_list]
        assert names_tried == ["lcm:1:Alice", "lcm1", "1"]

    async def test_set_user_create_raises_when_all_attempts_fail(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """CREATE surfaces ``LockOperationFailed`` only after every name fails.

        If the lock rejects canonical, compact, AND slot-only, the
        failure is more than a charset issue. Surfacing as
        ``LockOperationFailed`` lets the seam route through the normal
        retry path rather than the catchall suspend.
        """
        mock_set_user = AsyncMock(
            side_effect=[
                UnknownError("canonical rejected"),
                UnknownError("compact rejected"),
                UnknownError("slot-only rejected"),
            ]
        )
        user = User(user_id=1, name="lcm:1:Alice")
        with (
            self._patch_users([]),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", mock_set_user),
            pytest.raises(LockOperationFailed, match="all fallback names"),
        ):
            await matter_lock.async_set_user(user)

        assert mock_set_user.call_count == 3

    async def test_set_user_create_routes_validation_error_during_fallback(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """A ``ServiceValidationError`` during the cascade gets typed routing.

        The helper raises typed seam exceptions immediately on
        ``ServiceValidationError`` and ``HomeAssistantError`` because
        those aren't charset-recoverable; trying the next candidate
        would hit the same wall. Routing them through
        ``LockOperationFailed`` / ``LockDisconnected`` keeps the seam's
        retry path in play instead of dropping into the catchall
        suspend.
        """
        # Canonical hits MatterError -> falls through; compact hits
        # ServiceValidationError -> immediate LockOperationFailed.
        mock_set_user = AsyncMock(
            side_effect=[
                UnknownError("InvalidCommand (0x85)"),
                ServiceValidationError("validation"),
            ]
        )
        user = User(user_id=1, name="lcm:1:Alice")
        with (
            self._patch_users([]),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", mock_set_user),
            pytest.raises(LockOperationFailed, match="rejected input"),
        ):
            await matter_lock.async_set_user(user)

        assert mock_set_user.call_count == 2

    async def test_set_user_create_routes_disconnect_during_fallback(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """A ``HomeAssistantError`` during the cascade maps to ``LockDisconnected``."""
        mock_set_user = AsyncMock(
            side_effect=[
                UnknownError("InvalidCommand (0x85)"),
                HomeAssistantError("transport closed"),
            ]
        )
        user = User(user_id=1, name="lcm:1:Alice")
        with (
            self._patch_users([]),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", mock_set_user),
            pytest.raises(LockDisconnected, match="transport closed"),
        ):
            await matter_lock.async_set_user(user)

        assert mock_set_user.call_count == 2

    async def test_set_user_create_routes_client_exception_during_fallback(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """A ``MatterClientException`` during the cascade maps to ``LockDisconnected``.

        ``InvalidState: Not connected`` is a transport failure, not a
        charset rejection, so it short-circuits the cascade (trying the
        next candidate name would hit the same closed connection) and
        routes to retry rather than escaping to the catchall suspend
        (issue #1257). It is independent of ``HomeAssistantError``, so
        the prior fix that only added ``MatterError`` did not cover it.
        """
        mock_set_user = AsyncMock(
            side_effect=[
                UnknownError("InvalidCommand (0x85)"),
                MatterClientException("Not connected"),
            ]
        )
        user = User(user_id=1, name="lcm:1:Alice")
        with (
            self._patch_users([]),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", mock_set_user),
            pytest.raises(LockDisconnected, match="Not connected"),
        ):
            await matter_lock.async_set_user(user)

        assert mock_set_user.call_count == 2

    async def test_set_user_update_falls_back_to_compact_on_charset_rejection(
        self, hass: HomeAssistant, matter_lock: MatterLock, caplog
    ) -> None:
        """UPDATE prefers the compact ``lcm<slot>`` tier over bare slot-only.

        UPDATE's contract is "tolerate name-set failures," but a
        successful compact rename preserves both the slot binding AND
        the LCM ownership marker -- strictly better than bare digits.
        """
        mock_set_user = AsyncMock(
            side_effect=[
                UnknownError("InvalidCommand (0x85)"),
                None,
            ]
        )
        user = User(user_id=2, name="lcm:2:Updated Name")
        # Same as the CREATE variant: the success-on-fallback log is at
        # DEBUG, so enable DEBUG capture explicitly.
        caplog.set_level(logging.DEBUG, logger=_PROVIDER_MODULE)
        with (
            self._patch_users(
                [
                    {
                        "user_index": 7,
                        "user_name": "lcm:2:Original",
                        "credentials": [{"type": "pin", "index": 3}],
                    },
                ]
            ),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", mock_set_user),
        ):
            result = await matter_lock.async_set_user(user)

        assert result == SetUserResult(user_id=7, created=False)
        assert mock_set_user.call_count == 2
        # Compact tier (lcm2), not bare slot-only (2).
        assert mock_set_user.call_args_list[1].kwargs["user_name"] == "lcm2"
        assert "rejected" in caplog.text and "lcm:2:Updated Name" in caplog.text

    async def test_set_user_update_tolerates_when_all_attempts_fail(
        self, hass: HomeAssistant, matter_lock: MatterLock, caplog
    ) -> None:
        """UPDATE tolerates the failure when every cascade candidate fails.

        The user record at ``existing_user_index`` is still valid; only
        the rename was lost. The subsequent credential write can still
        proceed, so we return the resolved user_id and log a single
        warning carrying the last error and the list of tried names.
        """
        mock_set_user = AsyncMock(
            side_effect=[
                UnknownError("canonical rejected"),
                UnknownError("compact rejected"),
                UnknownError("slot-only rejected"),
            ]
        )
        user = User(user_id=2, name="lcm:2:Bob")
        with (
            self._patch_users(
                [
                    {
                        "user_index": 9,
                        "user_name": "lcm:2:Original",
                        "credentials": [{"type": "pin", "index": 1}],
                    },
                ]
            ),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", mock_set_user),
        ):
            result = await matter_lock.async_set_user(user)

        assert result == SetUserResult(user_id=9, created=False)
        assert mock_set_user.call_count == 3
        assert "failed to update user name" in caplog.text
        # The warning carries the LAST error message + the list of names.
        assert "slot-only rejected" in caplog.text

    @pytest.mark.parametrize(
        ("error", "raised"),
        [
            (UnknownError("InvalidCommand (0x85)"), LockOperationFailed),
            (HomeAssistantError("500"), LockDisconnected),
        ],
    )
    async def test_set_user_refuses_an_adopted_user_it_cannot_tag(
        self,
        hass: HomeAssistant,
        matter_lock: MatterLock,
        error: Exception,
        raised: type[Exception],
    ) -> None:
        """
        An untagged user the rename cannot tag is not written to (#1518).

        A clear finds only tagged users, so a PIN written onto a user that
        stays untagged could never be cleared: the slot turned off would
        leave it opening the door. The tolerance for a failed rename is for
        a user already tagged.
        """
        mock_set_user = AsyncMock(side_effect=error)
        with (
            self._patch_users(
                [
                    {
                        "user_index": 9,
                        "user_name": "Legacy Carol",
                        "credentials": [{"type": "pin", "index": 2}],
                    },
                ]
            ),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", mock_set_user),
            pytest.raises(raised),
        ):
            await matter_lock.async_set_user(User(user_id=2, name="lcm:2:Bob"))


# =============================================================================
# async_delete_user tests
# =============================================================================


class TestDeleteUser:
    """Test async_delete_user removes lock users."""

    async def test_delete_user_calls_clear_lock_user(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """clear_lock_user is called with the correct user_index."""
        mock_clear_user = AsyncMock(return_value=None)
        with (
            patch(f"{_PROVIDER_MODULE}.clear_lock_user", mock_clear_user),
        ):
            await matter_lock.async_delete_user(3)

        mock_clear_user.assert_called_once()
        # The first positional arg after client/node is user_index
        call_args = mock_clear_user.call_args
        assert 3 in call_args.args or call_args.kwargs.get("user_index") == 3

    async def test_delete_user_disconnected(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """HomeAssistantError from clear_lock_user raises LockDisconnected."""
        mock_clear_user = AsyncMock(side_effect=HomeAssistantError("offline"))
        with (
            patch(f"{_PROVIDER_MODULE}.clear_lock_user", mock_clear_user),
            pytest.raises(LockDisconnected),
        ):
            await matter_lock.async_delete_user(3)

    async def test_delete_user_service_validation_error(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """ServiceValidationError from clear_lock_user raises LockOperationFailed."""
        mock_clear_user = AsyncMock(side_effect=ServiceValidationError("bad user"))
        with (
            patch(f"{_PROVIDER_MODULE}.clear_lock_user", mock_clear_user),
            pytest.raises(LockOperationFailed, match="rejected input"),
        ):
            await matter_lock.async_delete_user(3)

    async def test_delete_user_client_exception(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """``MatterClientException`` from clear_lock_user raises LockDisconnected.

        This is the exact path behind the original #1257 report:
        ``Unexpected error during clear usercode ... InvalidState: Not
        connected``. The clear-user site only caught ``HomeAssistantError``,
        so the client exception escaped to the sync catchall and
        suspended the slot instead of being retried.
        """
        mock_clear_user = AsyncMock(side_effect=MatterClientException("Not connected"))
        with (
            patch(f"{_PROVIDER_MODULE}.clear_lock_user", mock_clear_user),
            pytest.raises(LockDisconnected, match="clear_lock_user failed"),
        ):
            await matter_lock.async_delete_user(3)


# =============================================================================
# async_release_managed_slot tests
# =============================================================================


class TestReleaseManagedSlot:
    """async_release_managed_slot tears down the user anchoring a removed slot."""

    _patch_users = staticmethod(_patch_user_reads)

    # The mock_door_lock node's CurrentFabricIndex (0/62/5).
    _HA_FABRIC = 1

    @staticmethod
    def _patch_status(**status: Any) -> Any:
        """Patch get_lock_credential_status to answer ``status``."""
        return patch(
            f"{_PROVIDER_MODULE}.get_lock_credential_status",
            AsyncMock(
                return_value={"credential_exists": True, "user_index": 42} | status
            ),
        )

    _CAROL = {
        "user_index": 42,
        "user_name": "lcm:5:Carol",
        "credentials": [{"type": "pin", "index": 7}],
    }

    async def test_release_deletes_tagged_user(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Finds the lcm:<slot>: user and clears it via clear_lock_user."""
        mock_clear_user = AsyncMock(return_value=None)
        with (
            self._patch_users(matter_lock, [self._CAROL]),
            self._patch_status(creator_fabric_index=self._HA_FABRIC),
            patch(f"{_PROVIDER_MODULE}.clear_lock_user", mock_clear_user),
        ):
            await matter_lock.async_release_managed_slot(5)

        mock_clear_user.assert_called_once()
        call = mock_clear_user.call_args
        assert 42 in call.args or call.kwargs.get("user_index") == 42

    @pytest.mark.parametrize(
        "fabric", [None, 0, KeyError], ids=["null", "no_fabric", "no_root_endpoint"]
    )
    async def test_an_unknown_fabric_removes_nothing(
        self, hass: HomeAssistant, matter_lock: MatterLock, fabric: Any
    ) -> None:
        """
        A node that does not say which fabric is Home Assistant's: raised, to retry.

        Whose the PIN is cannot be told, so neither ClearUser nor a status
        read goes out.
        """
        clear_user = AsyncMock(return_value=None)
        status = AsyncMock()
        _client, node = matter_lock._require_client_and_node()
        with (
            self._patch_users(matter_lock, [self._CAROL]),
            patch(f"{_PROVIDER_MODULE}.get_lock_credential_status", status),
            patch(f"{_PROVIDER_MODULE}.clear_lock_user", clear_user),
            patch.object(
                node,
                "get_attribute_value",
                side_effect=fabric if fabric is KeyError else None,
                return_value=fabric,
            ),
            pytest.raises(LockDisconnected, match="fabric index"),
        ):
            await matter_lock.async_release_managed_slot(5)

        clear_user.assert_not_called()
        status.assert_not_called()

    async def test_a_status_read_that_fails_removes_nothing(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Whose the PIN is went unanswered: raised, to retry, and nothing cleared."""
        clear_user = AsyncMock(return_value=None)
        with (
            self._patch_users(matter_lock, [self._CAROL]),
            patch(
                f"{_PROVIDER_MODULE}.get_lock_credential_status",
                AsyncMock(side_effect=HomeAssistantError("timed out")),
            ),
            patch(f"{_PROVIDER_MODULE}.clear_lock_user", clear_user),
            pytest.raises(LockDisconnected),
        ):
            await matter_lock.async_release_managed_slot(5)

        clear_user.assert_not_called()

    @pytest.mark.parametrize(
        ("credentials", "deleted"),
        [([{"type": "rfid", "index": 1}], False), ([], True)],
        ids=["a_card_only", "nothing"],
    )
    async def test_a_user_holding_no_pin_asks_no_status(
        self,
        hass: HomeAssistant,
        matter_lock: MatterLock,
        credentials: list[dict[str, Any]],
        deleted: bool,
    ) -> None:
        """
        No PIN on the user: no status read, and the fabric is not needed.

        A card keeps the user, only without LCM's tag; a user holding
        nothing at all is deleted.
        """
        status = AsyncMock()
        clear_user = AsyncMock(return_value=None)
        set_user = AsyncMock(return_value=None)
        _client, node = matter_lock._require_client_and_node()
        with (
            self._patch_users(
                matter_lock, [self._CAROL | {"credentials": credentials}]
            ),
            patch(f"{_PROVIDER_MODULE}.get_lock_credential_status", status),
            patch(f"{_PROVIDER_MODULE}.clear_lock_user", clear_user),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", set_user),
            patch.object(node, "get_attribute_value", return_value=None),
        ):
            await matter_lock.async_release_managed_slot(5)

        status.assert_not_called()
        assert clear_user.called is deleted
        assert set_user.called is not deleted

    @pytest.mark.parametrize(
        "status",
        [{"credential_exists": False}, {"user_index": 41}],
        ids=["gone", "another_user_s"],
    )
    async def test_a_pin_no_longer_on_the_user_is_not_cleared(
        self, hass: HomeAssistant, matter_lock: MatterLock, status: dict[str, Any]
    ) -> None:
        """
        A listed PIN the lock says is gone, or is another user's, is not the user's.

        Though Home Assistant's fabric made it, it is not cleared by its
        index: the user keeps its card and only loses LCM's tag.
        """
        carol = self._CAROL | {
            "credentials": [{"type": "pin", "index": 7}, {"type": "rfid", "index": 1}]
        }
        clear_user = AsyncMock(return_value=None)
        clear_credential = AsyncMock(return_value=None)
        set_user = AsyncMock(return_value=None)
        with (
            self._patch_users(matter_lock, [carol]),
            self._patch_status(creator_fabric_index=self._HA_FABRIC, **status),
            patch(f"{_PROVIDER_MODULE}.clear_lock_user", clear_user),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", clear_credential),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", set_user),
        ):
            await matter_lock.async_release_managed_slot(5)

        clear_user.assert_not_called()
        clear_credential.assert_not_called()
        assert set_user.call_args.kwargs["user_name"] == "Carol"

    # -- A lock that cannot list its users (#1689) ----------------------------

    @staticmethod
    def _patch_unlisted() -> Any:
        """Patch get_lock_users to fail, as the Aqara U400 in #1689 did."""
        return patch(
            f"{_PROVIDER_MODULE}.get_lock_users",
            AsyncMock(side_effect=HomeAssistantError("Failure (code 1)")),
        )

    @staticmethod
    def _patch_get_user(matter_lock: MatterLock, answer: Any) -> Any:
        """Patch the single GetUser to answer ``answer``, or raise it."""
        client, _node = matter_lock._require_client_and_node()
        mock = (
            AsyncMock(side_effect=answer)
            if isinstance(answer, BaseException)
            else AsyncMock(return_value=answer)
        )
        return patch.object(client, "send_device_command", mock)

    @staticmethod
    def _get_user_answer(name: str, credentials: list[tuple[int, int]]) -> dict:
        """A GetUser answer for user 42 (credential type 1 is a PIN, 2 a card)."""
        return {
            "userIndex": 42,
            "userName": name,
            "userStatus": 1,
            "userType": 0,
            "credentialRule": 0,
            "credentials": [
                {"credentialType": kind, "credentialIndex": index}
                for kind, index in credentials
            ],
            "creatorFabricIndex": 1,
            "lastModifiedFabricIndex": 1,
            "nextUserIndex": None,
        }

    @staticmethod
    def _patch_statuses(by_index: dict[int, dict[str, Any]]) -> Any:
        """Patch get_lock_credential_status to answer per credential index."""

        async def _status(*_args: Any, credential_index: int, **_kw: Any) -> dict:
            return by_index[credential_index]

        return patch(
            f"{_PROVIDER_MODULE}.get_lock_credential_status",
            AsyncMock(side_effect=_status),
        )

    @pytest.mark.parametrize(
        ("creator", "released"),
        [(_HA_FABRIC, True), (_HA_FABRIC + 1, False)],
        ids=["lcm_s_own", "the_homeowner_s"],
    )
    async def test_unlisted_release_of_a_user_named_with_the_slot(
        self,
        hass: HomeAssistant,
        matter_lock: MatterLock,
        caplog: pytest.LogCaptureFixture,
        creator: int,
        released: bool,
    ) -> None:
        """
        The user read alone, named ``5``, is slot 5's only if Home Assistant's fabric made it.

        LCM falls back to the bare number on a lock that refuses its tag, and
        only on a user it made: the lock lets no other fabric rename one. A
        user another fabric made, now at the recorded index, is a homeowner's
        named ``5`` (#1729), and is left as it is.
        """
        matter_lock._slot_by_user_index[42] = 5
        matter_lock._pins_by_user_index[42] = {7}
        answer = self._get_user_answer("5", [(1, 7)]) | {"creatorFabricIndex": creator}
        clear_user = AsyncMock(return_value=None)
        clear_credential = AsyncMock(return_value=None)
        set_user = AsyncMock(return_value=None)
        with (
            self._patch_unlisted(),
            self._patch_get_user(matter_lock, answer),
            self._patch_statuses(
                {
                    7: {
                        "credential_exists": True,
                        "user_index": 42,
                        "creator_fabric_index": creator,
                    }
                }
            ),
            patch(f"{_PROVIDER_MODULE}.clear_lock_user", clear_user),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", clear_credential),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", set_user),
        ):
            await matter_lock.async_release_managed_slot(5, written=True)

        assert clear_user.called is released
        clear_credential.assert_not_called()
        set_user.assert_not_called()
        assert matter_lock._slot_by_user_index == {}
        assert ("no user on the lock carries LCM's tag" in caplog.text) is not released

    async def test_unlisted_release_clears_only_lcm_s_recorded_pin(
        self,
        hass: HomeAssistant,
        matter_lock: MatterLock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """
        The listing and the user's own read fail: LCM's recorded PIN is cleared by its index.

        Of two PINs recorded on the user, only the one Home Assistant's fabric
        made is cleared. The user is never cleared or renamed -- what else it
        holds cannot be seen -- and the warning says it may remain.
        """
        matter_lock._slot_by_user_index[42] = 5
        matter_lock._pins_by_user_index[42] = {7, 8}
        clear_user = AsyncMock(return_value=None)
        clear_credential = AsyncMock(return_value=None)
        set_user = AsyncMock(return_value=None)
        with (
            self._patch_unlisted(),
            self._patch_get_user(matter_lock, UnknownError("Failure")),
            self._patch_statuses(
                {
                    7: {
                        "credential_exists": True,
                        "user_index": 42,
                        "creator_fabric_index": self._HA_FABRIC,
                    },
                    8: {
                        "credential_exists": True,
                        "user_index": 42,
                        "creator_fabric_index": self._HA_FABRIC + 1,
                    },
                }
            ),
            patch(f"{_PROVIDER_MODULE}.clear_lock_user", clear_user),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", clear_credential),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", set_user),
        ):
            await matter_lock.async_release_managed_slot(5)

        clear_credential.assert_called_once()
        assert clear_credential.call_args.kwargs["credential_index"] == 7
        clear_user.assert_not_called()
        set_user.assert_not_called()
        assert matter_lock._slot_by_user_index == {42: 5}
        assert matter_lock._pins_by_user_index == {42: {8}}
        assert "index(es) 7 on user 42" in caplog.text
        assert "remove it in the lock vendor's app" in caplog.text

    @pytest.mark.parametrize(
        "status",
        [
            {"creator_fabric_index": 2},
            {"user_index": 41},
            {"credential_exists": False},
        ],
        ids=["another_fabric_s", "another_user_s", "gone"],
    )
    @pytest.mark.parametrize("written", [True, False], ids=["written", "unwritten"])
    async def test_unlisted_release_leaves_a_pin_that_is_not_lcm_s(
        self,
        hass: HomeAssistant,
        matter_lock: MatterLock,
        caplog: pytest.LogCaptureFixture,
        status: dict[str, Any],
        written: bool,
    ) -> None:
        """
        A recorded index the lock says is not LCM's PIN on this user is left alone.

        Said at WARNING only if LCM had written a PIN for the slot.
        """
        matter_lock._slot_by_user_index[42] = 5
        matter_lock._pins_by_user_index[42] = {7}
        clear_user = AsyncMock(return_value=None)
        clear_credential = AsyncMock(return_value=None)
        with (
            self._patch_unlisted(),
            self._patch_get_user(matter_lock, UnknownError("Failure")),
            self._patch_statuses(
                {
                    7: {
                        "credential_exists": True,
                        "user_index": 42,
                        "creator_fabric_index": self._HA_FABRIC,
                    }
                    | status
                }
            ),
            patch(f"{_PROVIDER_MODULE}.clear_lock_user", clear_user),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", clear_credential),
        ):
            await matter_lock.async_release_managed_slot(5, written=written)

        clear_credential.assert_not_called()
        clear_user.assert_not_called()
        assert matter_lock._pins_by_user_index == {42: {7}}
        assert ("nothing was removed" in caplog.text) is written

    @pytest.mark.parametrize(
        ("slots", "pins"),
        [({}, {}), ({42: 5, 43: 5}, {42: {7}, 43: {8}}), ({42: 5}, {42: set()})],
        ids=["nothing_recorded", "two_users_recorded", "no_pin_recorded"],
    )
    async def test_unlisted_release_does_not_guess(
        self,
        hass: HomeAssistant,
        matter_lock: MatterLock,
        slots: dict[int, int],
        pins: dict[int, set[int]],
    ) -> None:
        """
        No single recorded user with a recorded PIN: the listing's failure is raised.

        Nothing is asked of the lock and nothing is cleared; the release is
        retried.
        """
        matter_lock._slot_by_user_index.update(slots)
        matter_lock._pins_by_user_index.update(pins)
        status = AsyncMock()
        clear_user = AsyncMock(return_value=None)
        clear_credential = AsyncMock(return_value=None)
        with (
            self._patch_unlisted(),
            self._patch_get_user(matter_lock, UnknownError("Failure")),
            patch(f"{_PROVIDER_MODULE}.get_lock_credential_status", status),
            patch(f"{_PROVIDER_MODULE}.clear_lock_user", clear_user),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", clear_credential),
            pytest.raises(LockDisconnected, match="get_lock_users"),
        ):
            await matter_lock.async_release_managed_slot(5)

        status.assert_not_called()
        clear_credential.assert_not_called()
        clear_user.assert_not_called()

    async def test_unlisted_release_with_an_unknown_fabric_removes_nothing(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Whose a recorded PIN is cannot be told: raised, to retry."""
        matter_lock._slot_by_user_index[42] = 5
        matter_lock._pins_by_user_index[42] = {7}
        status = AsyncMock()
        clear_credential = AsyncMock(return_value=None)
        _client, node = matter_lock._require_client_and_node()
        with (
            self._patch_unlisted(),
            self._patch_get_user(matter_lock, UnknownError("Failure")),
            patch(f"{_PROVIDER_MODULE}.get_lock_credential_status", status),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", clear_credential),
            patch.object(node, "get_attribute_value", return_value=None),
            pytest.raises(LockDisconnected, match="fabric index"),
        ):
            await matter_lock.async_release_managed_slot(5)

        status.assert_not_called()
        clear_credential.assert_not_called()

    async def test_unlisted_release_with_a_failed_status_read_removes_nothing(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """A recorded PIN's status went unanswered: raised, to retry."""
        matter_lock._slot_by_user_index[42] = 5
        matter_lock._pins_by_user_index[42] = {7}
        clear_credential = AsyncMock(return_value=None)
        with (
            self._patch_unlisted(),
            self._patch_get_user(matter_lock, UnknownError("Failure")),
            patch(
                f"{_PROVIDER_MODULE}.get_lock_credential_status",
                AsyncMock(side_effect=HomeAssistantError("timed out")),
            ),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", clear_credential),
            pytest.raises(LockDisconnected, match="get_lock_credential_status"),
        ):
            await matter_lock.async_release_managed_slot(5)

        clear_credential.assert_not_called()
        assert matter_lock._pins_by_user_index == {42: {7}}

    @pytest.mark.parametrize(
        ("credentials", "deleted"),
        [([(1, 7)], True), ([(1, 7), (2, 1)], False)],
        ids=["only_lcm_s_pin", "a_card_too"],
    )
    async def test_unlisted_release_reads_the_recorded_user(
        self,
        hass: HomeAssistant,
        matter_lock: MatterLock,
        credentials: list[tuple[int, int]],
        deleted: bool,
    ) -> None:
        """
        The listing fails, but the recorded user reads: it is released as listed.

        Holding only LCM's PIN, it is cleared with ClearUser; holding a card
        too, LCM's PIN is cleared and the user renamed without the tag.
        """
        matter_lock._slot_by_user_index[42] = 5
        clear_user = AsyncMock(return_value=None)
        clear_credential = AsyncMock(return_value=None)
        set_user = AsyncMock(return_value=None)
        with (
            self._patch_unlisted(),
            self._patch_get_user(
                matter_lock, self._get_user_answer("lcm:5:Carol", credentials)
            ),
            self._patch_status(creator_fabric_index=self._HA_FABRIC),
            patch(f"{_PROVIDER_MODULE}.clear_lock_user", clear_user),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", clear_credential),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", set_user),
        ):
            await matter_lock.async_release_managed_slot(5)

        if deleted:
            clear_user.assert_called_once()
            clear_credential.assert_not_called()
            set_user.assert_not_called()
        else:
            clear_user.assert_not_called()
            assert clear_credential.call_args.kwargs["credential_index"] == 7
            assert set_user.call_args.kwargs["user_name"] == "Carol"
        assert 42 not in matter_lock._slot_by_user_index

    @pytest.mark.parametrize(
        "answer",
        [
            {"userName": "Guest"},
            {"userName": "lcm:6:Dan"},
            {"userName": None, "userStatus": None, "credentials": None},
        ],
        ids=["untagged", "another_slot_s", "empty"],
    )
    @pytest.mark.parametrize("written", [True, False], ids=["written", "unwritten"])
    async def test_unlisted_release_of_a_user_no_longer_lcm_s_removes_nothing(
        self,
        hass: HomeAssistant,
        matter_lock: MatterLock,
        caplog: pytest.LogCaptureFixture,
        answer: dict[str, Any],
        written: bool,
    ) -> None:
        """The recorded user reads, but without this slot's tag: nothing is removed."""
        matter_lock._slot_by_user_index[42] = 5
        matter_lock._pins_by_user_index[42] = {7}
        status = AsyncMock()
        clear_user = AsyncMock(return_value=None)
        clear_credential = AsyncMock(return_value=None)
        set_user = AsyncMock(return_value=None)
        with (
            self._patch_unlisted(),
            self._patch_get_user(
                matter_lock, self._get_user_answer("lcm:5:Carol", [(1, 7)]) | answer
            ),
            patch(f"{_PROVIDER_MODULE}.get_lock_credential_status", status),
            patch(f"{_PROVIDER_MODULE}.clear_lock_user", clear_user),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", clear_credential),
            patch(f"{_PROVIDER_MODULE}.set_lock_user", set_user),
        ):
            await matter_lock.async_release_managed_slot(5, written=written)

        status.assert_not_called()
        clear_user.assert_not_called()
        clear_credential.assert_not_called()
        set_user.assert_not_called()
        assert matter_lock._slot_by_user_index == {}
        assert matter_lock._pins_by_user_index == {}
        assert ("nothing was removed" in caplog.text) is written

    async def test_release_prefers_the_listing_over_the_record(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """When the lock can list its users, a stale record is never acted on."""
        matter_lock._slot_by_user_index[99] = 5  # stale: the index was reused
        matter_lock._pins_by_user_index[99] = {3}
        clear_user = AsyncMock(return_value=None)
        clear_credential = AsyncMock(return_value=None)
        with (
            self._patch_users(matter_lock, [self._CAROL]),
            self._patch_status(creator_fabric_index=self._HA_FABRIC),
            patch(f"{_PROVIDER_MODULE}.clear_lock_user", clear_user),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", clear_credential),
        ):
            await matter_lock.async_release_managed_slot(5)

        clear_user.assert_called_once()
        call = clear_user.call_args
        assert 42 in call.args or call.kwargs.get("user_index") == 42
        clear_credential.assert_not_called()

    async def test_the_pin_record_follows_reads_writes_and_deletes(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """
        A read records a tagged user's PINs, a write adds the index it wrote, a delete drops them.

        An untagged user is never recorded.
        """
        stranger = {
            "user_index": 3,
            "user_name": "Guest",
            "credentials": [{"type": "pin", "index": 1}],
        }
        with self._patch_users(matter_lock, [self._CAROL, stranger]):
            await matter_lock.async_get_users()
        assert matter_lock._pins_by_user_index == {42: {7}}

        with (
            self._patch_users(matter_lock, [self._CAROL]),
            self._patch_status(),
            patch(
                f"{_PROVIDER_MODULE}.set_lock_credential",
                AsyncMock(return_value={"credential_index": 9, "user_index": 42}),
            ),
        ):
            await matter_lock.async_set_credential(
                42,
                Credential(
                    type=CredentialType.PIN,
                    slot=5,
                    state=SlotCredential.known("1234"),
                ),
                "1234",
                name="Carol",
                source="direct",
            )
        assert matter_lock._pins_by_user_index == {42: {7, 9}}

        with patch(f"{_PROVIDER_MODULE}.clear_lock_user", AsyncMock()):
            await matter_lock.async_delete_user(42)
        assert 42 not in matter_lock._pins_by_user_index
        assert 42 not in matter_lock._slot_by_user_index


class TestGetUserRead:
    """``_raw_lock_user``: one GetUser, sent and parsed by the provider itself."""

    @staticmethod
    def _answering(matter_lock: MatterLock, answer: Any) -> Any:
        """Patch the client's ``send_device_command`` to answer ``answer``."""
        client, _node = matter_lock._require_client_and_node()
        return patch.object(
            client, "send_device_command", AsyncMock(return_value=answer)
        )

    async def test_a_user_reads_as_the_listing_lists_it(
        self, matter_lock: MatterLock
    ) -> None:
        """
        The answer keeps what the provider reads, under ``get_lock_users``'s names.

        The command goes to the node's Door Lock endpoint. A credential that
        is not a Personal Identification Number is kept, its type named as
        Home Assistant names it.
        """
        answer = get_user_answer(
            3,
            [
                {
                    "user_index": 3,
                    "user_name": "lcm:5:Carol",
                    "creator_fabric_index": 2,
                    "credentials": [
                        {"type": "pin", "index": 7},
                        {"type": "rfid", "index": 2},
                    ],
                }
            ],
        )
        with self._answering(matter_lock, answer) as send:
            user = await matter_lock._raw_lock_user(matter_lock._require_door_lock(), 3)

        assert user == {
            "user_index": 3,
            "user_name": "lcm:5:Carol",
            "creator_fabric_index": 2,
            "credentials": [
                {"type": "pin", "index": 7},
                {"type": "rfid", "index": 2},
            ],
        }
        _client, node = matter_lock._require_client_and_node()
        send.assert_awaited_once()
        assert send.call_args.kwargs["node_id"] == node.node_id
        assert send.call_args.kwargs["endpoint_id"] == 1
        assert send.call_args.kwargs["command"] == (
            clusters.DoorLock.Commands.GetUser(userIndex=3)
        )

    async def test_an_empty_index_reads_as_no_user(
        self, matter_lock: MatterLock
    ) -> None:
        """An index the lock holds no user at answers no ``userStatus``."""
        with self._answering(matter_lock, get_user_answer(3, [])):
            assert (
                await matter_lock._raw_lock_user(matter_lock._require_door_lock(), 3)
                is None
            )

    @pytest.mark.parametrize("name", [None, 5], ids=["null", "not_text"])
    async def test_a_name_that_is_not_text_reads_as_no_name(
        self, matter_lock: MatterLock, name: Any
    ) -> None:
        """The user is still read, its credentials with it: it has no name."""
        answer = get_user_answer(
            3,
            [
                {
                    "user_index": 3,
                    "user_name": "lcm:5:Carol",
                    "credentials": [{"type": "pin", "index": 7}],
                }
            ],
        ) | {"userName": name}
        with self._answering(matter_lock, answer):
            user = await matter_lock._raw_lock_user(matter_lock._require_door_lock(), 3)

        assert user is not None
        assert user["user_name"] is None
        assert user["credentials"] == [{"type": "pin", "index": 7}]

    async def test_a_credential_without_its_index_does_not_hide_the_pin(
        self, matter_lock: MatterLock
    ) -> None:
        """
        A credential the lock gives no index is kept without one, and skipped.

        The user's Personal Identification Number beside it is still found,
        as it is in Home Assistant's listing.
        """
        answer = get_user_answer(
            3,
            [
                {
                    "user_index": 3,
                    "user_name": "lcm:5:Carol",
                    "credentials": [
                        {"type": "pin", "index": 2},
                        {"type": "pin", "index": 7},
                    ],
                }
            ],
        )
        answer["credentials"][0]["credentialIndex"] = None
        with self._answering(matter_lock, answer):
            user = await matter_lock._raw_lock_user(matter_lock._require_door_lock(), 3)
            index = await matter_lock._find_pin_credential_index_for_user(3)

        assert user is not None
        assert user["credentials"] == [
            {"type": "pin", "index": None},
            {"type": "pin", "index": 7},
        ]
        assert index == 7

    @pytest.mark.parametrize(
        "answer",
        [
            None,
            [],
            get_user_answer(4, [{"user_index": 4, "user_name": "lcm:5:Carol"}]),
            get_user_answer(3, [{"user_index": 3, "user_name": "lcm:5:Carol"}])
            | {"credentials": {"credentialType": 1, "credentialIndex": 7}},
            get_user_answer(3, [{"user_index": 3, "user_name": "lcm:5:Carol"}])
            | {"credentials": [7]},
        ],
        ids=[
            "nothing",
            "not_a_record",
            "another_user_s",
            "credentials_not_a_list",
            "a_credential_not_a_record",
        ],
    )
    async def test_an_answer_that_is_not_a_user_record_fails_the_operation(
        self, matter_lock: MatterLock, answer: Any
    ) -> None:
        """
        The lock answered, but not with a user: ``LockOperationFailed``, never a raw error.

        Every caller handles ``LockOperationError``; anything else escaped them.
        """
        with (
            self._answering(matter_lock, answer),
            pytest.raises(LockOperationFailed, match="GetUser answered"),
        ):
            await matter_lock._raw_lock_user(matter_lock._require_door_lock(), 3)

    async def test_a_use_whose_user_reads_malformed_reads_nothing_more(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """
        A LockOperation whose named user answers malformed goes unannounced.

        Nobody is searched for the credential used: the user was named, and
        the lock answered for it.
        """
        fired: list[dict[str, Any]] = []
        matter_lock.async_fire_code_slot_event = lambda **kw: fired.append(kw)
        listing = AsyncMock(return_value={"max_users": 10, "users": []})
        with (
            self._answering(
                matter_lock, {"userIndex": 3, "userStatus": 1, "credentials": 7}
            ),
            patch(f"{_PROVIDER_MODULE}.get_lock_users", listing),
        ):
            matter_lock._on_node_event(
                None,
                _make_node_event(
                    event_id=2,
                    data={
                        "lockOperationType": 1,
                        "userIndex": 3,
                        "credentials": [{"credentialType": 1, "credentialIndex": 7}],
                    },
                ),
            )
            await hass.async_block_till_done()

        assert fired == []
        listing.assert_not_awaited()

    async def test_a_node_without_its_door_lock_endpoint_is_disconnected(
        self, matter_lock: MatterLock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """
        A node without its Door Lock endpoint (mid re-interview) reads as disconnected.

        Nothing is sent: there is no endpoint to send GetUser to.
        """
        _client, node = matter_lock._require_client_and_node()
        monkeypatch.setattr(node, "endpoints", {0: node.endpoints[0]})

        with (
            self._answering(matter_lock, None) as send,
            pytest.raises(LockDisconnected, match="no Door Lock endpoint"),
        ):
            await matter_lock._find_pin_credential_index_for_user(1)
        send.assert_not_awaited()


# =============================================================================
# async_set_credential tests
# =============================================================================


class TestSetCredential:
    """Test async_set_credential writes Personal Identification Number credentials."""

    @pytest.fixture(autouse=True)
    def _empty_user_list(self, matter_client: MagicMock) -> AsyncMock:
        """Default get_lock_users to an empty list so the new CREATE path runs.

        async_set_credential now looks up the user's existing PIN credential
        index via async_get_users -> get_lock_users to decide MODIFY vs CREATE.
        Tests that want to exercise the MODIFY path can override this patch
        in their own context manager.
        """
        with (
            get_user_answered_from_listing(matter_client),
            patch(
                f"{_PROVIDER_MODULE}.get_lock_users",
                AsyncMock(return_value={"max_users": 10, "users": []}),
            ) as mock,
            # The pairing check asks the lock who holds the written credential;
            # an unknown owner is not a mismatch.
            patch(
                f"{_PROVIDER_MODULE}.get_lock_credential_status",
                AsyncMock(return_value={"credential_exists": True, "user_index": None}),
            ),
        ):
            yield mock

    def _make_credential(self, slot: int = 1, pin: str = "1234") -> Credential:
        """Build a readable PIN credential for the given slot."""
        return Credential(
            type=CredentialType.PIN,
            slot=slot,
            state=SlotCredential.known(pin),
        )

    async def test_set_credential_success_returns_true(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Successful set_lock_credential returns True."""
        mock_set_credential = AsyncMock(
            return_value={"credential_index": 1, "user_index": 1}
        )
        credential = self._make_credential(slot=1, pin="1234")
        with (
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
        ):
            result = await matter_lock.async_set_credential(
                1,
                credential,
                "1234",
                name="Alice",
                source="direct",
            )
        assert result is WriteResult.CONFIRMED

    async def test_set_credential_pushes_unreadable_optimistically(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """async_set_credential pushes SlotCredential.unreadable() on success."""
        mock_coordinator = _mock_coordinator()
        matter_lock.coordinator = mock_coordinator
        mock_set_credential = AsyncMock(
            return_value={"credential_index": 2, "user_index": 2}
        )
        credential = self._make_credential(slot=2, pin="5678")
        with (
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
        ):
            await matter_lock.async_set_credential(
                2, credential, "5678", name=None, source="direct"
            )

        mock_coordinator.push_update.assert_called_once_with(
            {2: SlotCredential.unreadable()}
        )

    async def test_set_credential_duplicate_direct_raises(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Duplicate status from lock raises DuplicateCodeError for direct source."""
        mock_set_credential = AsyncMock(
            side_effect=_make_set_credential_failed_error("duplicate")
        )
        credential = self._make_credential(slot=1, pin="1234")
        with (
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
            pytest.raises(DuplicateCodeError),
        ):
            await matter_lock.async_set_credential(
                1, credential, "1234", name=None, source="direct"
            )

    @staticmethod
    def _patch_user_with_pin(user_id: int, credential_index: int) -> Any:
        """Override the autouse empty-user fixture: seed a user owning a PIN.

        Sync-duplicate tests require an existing PIN credential so the
        clear-and-retry path runs (the new model only retries when LCM
        owns the duplicate -- otherwise the duplicate is external and
        surfaces immediately).
        """
        return patch(
            f"{_PROVIDER_MODULE}.get_lock_users",
            AsyncMock(
                return_value={
                    "max_users": 10,
                    "users": [
                        {
                            "user_index": user_id,
                            "user_name": f"lcm:{user_id}:test",
                            "credentials": [
                                {"type": "pin", "index": credential_index},
                            ],
                        },
                    ],
                },
            ),
        )

    async def test_set_credential_duplicate_sync_retries_and_succeeds(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Duplicate on sync source clears the existing credential and retries once."""
        mock_set_credential = AsyncMock(
            side_effect=[
                _make_set_credential_failed_error("duplicate"),
                {"credential_index": 1, "user_index": 1},
            ]
        )
        mock_clear = AsyncMock(return_value={})
        credential = self._make_credential(slot=1, pin="1234")
        with (
            self._patch_user_with_pin(user_id=1, credential_index=10),
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
        ):
            result = await matter_lock.async_set_credential(
                1, credential, "1234", name=None, source="sync"
            )

        assert result is WriteResult.CONFIRMED
        assert mock_set_credential.call_count == 2
        assert mock_clear.call_count == 1
        # First call uses the existing credential_index (MODIFY); retry uses
        # None (CREATE) because we just cleared the old one.
        first_call = mock_set_credential.call_args_list[0]
        retry_call = mock_set_credential.call_args_list[1]
        assert first_call.kwargs["credential_index"] == 10
        assert retry_call.kwargs["credential_index"] is None
        assert mock_clear.call_args.kwargs["credential_index"] == 10

    async def test_set_credential_duplicate_sync_persistent_raises(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Persistent duplicate on sync source raises DuplicateCodeError after retry."""
        mock_set_credential = AsyncMock(
            side_effect=_make_set_credential_failed_error("duplicate")
        )
        mock_clear = AsyncMock(return_value={})
        credential = self._make_credential(slot=1, pin="1234")
        with (
            self._patch_user_with_pin(user_id=1, credential_index=10),
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
            pytest.raises(DuplicateCodeError),
        ):
            await matter_lock.async_set_credential(
                1, credential, "1234", name=None, source="sync"
            )

        assert mock_set_credential.call_count == 2
        assert mock_clear.call_count == 1

    async def test_set_credential_duplicate_sync_retry_transient_routes_to_retry(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """A transient status on the post-clear retry routes to retry (#1257).

        The sync-duplicate path clears the existing credential and retries;
        if that retry returns an unmapped ``unknown(N)`` status (lock not
        ready), it must route to LockDisconnected rather than permanently
        disabling the slot.
        """
        mock_set_credential = AsyncMock(
            side_effect=[
                _make_set_credential_failed_error("duplicate"),
                _make_set_credential_failed_error("unknown(133)"),
            ]
        )
        mock_clear = AsyncMock(return_value={})
        credential = self._make_credential(slot=1, pin="1234")
        with (
            self._patch_user_with_pin(user_id=1, credential_index=10),
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
            pytest.raises(LockDisconnected),
        ):
            await matter_lock.async_set_credential(
                1, credential, "1234", name=None, source="sync"
            )

        assert mock_set_credential.call_count == 2
        assert mock_clear.call_count == 1

    async def test_set_credential_duplicate_sync_retry_definitive_rejection_raises(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """A definitive rejection on the post-clear retry raises CodeRejectedError.

        Distinct from the transient (``unknown(N)``) retry, a recognized
        rejection such as ``occupied`` is permanent, so it surfaces as a
        code rejection rather than routing to retry.
        """
        mock_set_credential = AsyncMock(
            side_effect=[
                _make_set_credential_failed_error("duplicate"),
                _make_set_credential_failed_error("occupied"),
            ]
        )
        mock_clear = AsyncMock(return_value={})
        credential = self._make_credential(slot=1, pin="1234")
        with (
            self._patch_user_with_pin(user_id=1, credential_index=10),
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
            pytest.raises(CodeRejectedError),
        ):
            await matter_lock.async_set_credential(
                1, credential, "1234", name=None, source="sync"
            )

        assert mock_set_credential.call_count == 2
        assert mock_clear.call_count == 1

    async def test_set_credential_sync_duplicate_external_surfaces_immediately(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """When LCM owns no PIN for the user, the duplicate must be external.

        Clearing it would step on another controller's credential, so
        DuplicateCodeError surfaces immediately without retry.
        """
        mock_set_credential = AsyncMock(
            side_effect=_make_set_credential_failed_error("duplicate")
        )
        mock_clear = AsyncMock(return_value={})
        credential = self._make_credential(slot=1, pin="1234")
        with (
            # autouse empty-user fixture: user has no existing PIN credential.
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
            pytest.raises(DuplicateCodeError),
        ):
            await matter_lock.async_set_credential(
                1, credential, "1234", name=None, source="sync"
            )
        # No retry, no clear -- the duplicate is external and out of scope.
        assert mock_set_credential.call_count == 1
        mock_clear.assert_not_called()

    async def test_set_credential_sync_duplicate_clear_disconnected(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """clear_lock_credential transport failure during retry maps to LockDisconnected."""
        mock_set_credential = AsyncMock(
            side_effect=_make_set_credential_failed_error("duplicate")
        )
        mock_clear = AsyncMock(side_effect=HomeAssistantError("connection lost"))
        credential = self._make_credential(slot=1, pin="1234")
        with (
            self._patch_user_with_pin(user_id=1, credential_index=10),
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
            pytest.raises(LockDisconnected, match="sync-duplicate retry"),
        ):
            await matter_lock.async_set_credential(
                1, credential, "1234", name=None, source="sync"
            )
        # First set raised duplicate; clear failed; no retry attempt is made.
        assert mock_set_credential.call_count == 1

    async def test_set_credential_sync_duplicate_clear_matter_client_exception(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """A MatterClientException during the duplicate-retry clear routes to retry.

        Regression guard (#1257): this clear path is independent of the main
        clear and both set paths; a MatterClientException here must map to
        LockDisconnected rather than escape to the generic handler and suspend
        the slot.
        """
        mock_set_credential = AsyncMock(
            side_effect=_make_set_credential_failed_error("duplicate")
        )
        mock_clear = AsyncMock(side_effect=MatterClientException("Not connected"))
        credential = self._make_credential(slot=1, pin="1234")
        with (
            self._patch_user_with_pin(user_id=1, credential_index=10),
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
            pytest.raises(LockDisconnected, match="sync-duplicate retry"),
        ):
            await matter_lock.async_set_credential(
                1, credential, "1234", name=None, source="sync"
            )
        assert mock_set_credential.call_count == 1

    async def test_set_credential_sync_duplicate_clear_service_validation_error(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """clear_lock_credential rejection during retry maps to LockOperationFailed."""
        mock_set_credential = AsyncMock(
            side_effect=_make_set_credential_failed_error("duplicate")
        )
        mock_clear = AsyncMock(side_effect=ServiceValidationError("bad slot"))
        credential = self._make_credential(slot=1, pin="1234")
        with (
            self._patch_user_with_pin(user_id=1, credential_index=10),
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
            pytest.raises(LockOperationFailed, match="sync-duplicate retry"),
        ):
            await matter_lock.async_set_credential(
                1, credential, "1234", name=None, source="sync"
            )
        assert mock_set_credential.call_count == 1

    async def test_set_credential_non_duplicate_failure_raises_code_rejected(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Non-duplicate SetCredentialFailedError raises CodeRejectedError."""
        mock_set_credential = AsyncMock(
            side_effect=_make_set_credential_failed_error("occupied")
        )
        credential = self._make_credential(slot=1, pin="1234")
        with (
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
            pytest.raises(CodeRejectedError),
        ):
            await matter_lock.async_set_credential(
                1, credential, "1234", name=None, source="direct"
            )

    async def test_set_credential_ha_error_raises_code_rejected(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """ServiceValidationError from helper raises CodeRejectedError."""
        mock_set_credential = AsyncMock(
            side_effect=ServiceValidationError("bad PIN length")
        )
        credential = self._make_credential(slot=1, pin="1")
        with (
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
            pytest.raises(CodeRejectedError),
        ):
            await matter_lock.async_set_credential(
                1, credential, "1", name=None, source="direct"
            )

    async def test_set_credential_transport_error_raises_lock_disconnected(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """A non-validation HomeAssistantError (transport) routes to retry path."""
        mock_set_credential = AsyncMock(side_effect=HomeAssistantError("endpoint gone"))
        credential = self._make_credential(slot=1, pin="1234")
        with (
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
            pytest.raises(LockDisconnected),
        ):
            await matter_lock.async_set_credential(
                1, credential, "1234", name=None, source="direct"
            )

    async def test_set_credential_unknown_status_routes_to_retry(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """An unmapped ``unknown(N)`` status routes to retry, not disable (#1257).

        Seen when a lock is not fully ready right after startup: the write
        must retry on a later tick rather than permanently disabling the slot.
        """
        mock_set_credential = AsyncMock(
            side_effect=_make_set_credential_failed_error("unknown(133)")
        )
        credential = self._make_credential(slot=1, pin="1234")
        with (
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
            pytest.raises(LockDisconnected),
        ):
            await matter_lock.async_set_credential(
                1, credential, "1234", name=None, source="sync"
            )

    async def test_set_credential_matter_client_exception_routes_to_retry(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """``InvalidState: Not connected`` at startup routes to retry (#1257).

        MatterClientException is independent of HomeAssistantError, so without
        an explicit catch it would escape to the generic handler and suspend
        the slot instead of retrying.
        """
        mock_set_credential = AsyncMock(
            side_effect=MatterClientException("Not connected")
        )
        credential = self._make_credential(slot=1, pin="1234")
        with (
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
            pytest.raises(LockDisconnected),
        ):
            await matter_lock.async_set_credential(
                1, credential, "1234", name=None, source="sync"
            )

    async def test_set_credential_create_passes_credential_index_none(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """When the user has no PIN yet, set_lock_credential gets credential_index=None.

        The CREATE path passes ``credential_index=None`` so Matter auto-allocates
        the next free slot; the previous slot=credential_index invariant is gone.
        """
        mock_set_credential = AsyncMock(
            return_value={"credential_index": 7, "user_index": 3}
        )
        credential = self._make_credential(slot=3, pin="9999")
        # _empty_user_list autouse fixture already gives no users -> no PIN
        # for user_id 3, so the CREATE path runs.
        with (
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
        ):
            await matter_lock.async_set_credential(
                3,
                credential,
                "9999",
                name="Carol",
                source="direct",
            )

        call_kwargs = mock_set_credential.call_args.kwargs
        assert call_kwargs["credential_type"] == "pin"
        assert call_kwargs["credential_data"] == "9999"
        assert call_kwargs["credential_index"] is None
        assert call_kwargs["user_index"] == 3

    async def test_set_credential_modify_passes_existing_credential_index(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """When the user already owns a PIN, set_lock_credential MODIFY'es at that index."""
        mock_set_credential = AsyncMock(
            return_value={"credential_index": 11, "user_index": 3}
        )
        credential = self._make_credential(slot=3, pin="9999")
        with (
            # Override the autouse empty-user fixture: user 3 already owns
            # a PIN at credential_index 11.
            patch(
                f"{_PROVIDER_MODULE}.get_lock_users",
                AsyncMock(
                    return_value={
                        "max_users": 10,
                        "users": [
                            {
                                "user_index": 3,
                                "user_name": "lcm:3:Carol",
                                "credentials": [{"type": "pin", "index": 11}],
                            },
                        ],
                    },
                ),
            ),
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
        ):
            await matter_lock.async_set_credential(
                3, credential, "9999", name="Carol", source="direct"
            )

        call_kwargs = mock_set_credential.call_args.kwargs
        assert call_kwargs["credential_index"] == 11
        assert call_kwargs["user_index"] == 3

    # =============================================================================
    # async_delete_credential tests
    # =============================================================================

    @staticmethod
    def _patch_status(**status: Any) -> Any:
        """Answer the pairing check's GetCredentialStatus."""
        return patch(
            f"{_PROVIDER_MODULE}.get_lock_credential_status",
            AsyncMock(return_value={"credential_exists": True, **status}),
        )

    async def test_set_credential_filed_under_another_user_is_cleared_and_failed(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """A PIN the lock filed under another user opens the door as somebody else.

        The lock's own credential status names user 7 for the index the write
        went to, although the write was for user 1. The credential is cleared
        again and the write fails for the slot breaker; nothing is pushed as
        a success (issue #1538).
        """
        mock_coordinator = _mock_coordinator()
        matter_lock.coordinator = mock_coordinator
        mock_clear = AsyncMock(return_value={})
        with (
            patch(
                f"{_PROVIDER_MODULE}.set_lock_credential",
                AsyncMock(return_value={"credential_index": 4, "user_index": 1}),
            ),
            self._patch_status(user_index=7),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
            pytest.raises(LockOperationFailed, match="under user 7, not user 1"),
        ):
            await matter_lock.async_set_credential(
                1, self._make_credential(slot=1), "1234", name=None, source="sync"
            )

        mock_clear.assert_awaited_once()
        assert mock_clear.await_args.kwargs["credential_index"] == 4
        mock_coordinator.push_update.assert_not_called()

    async def test_set_credential_misfiled_pin_that_cannot_be_cleared_still_fails(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Failing to clear the misfiled credential does not turn it into a success."""
        with (
            patch(
                f"{_PROVIDER_MODULE}.set_lock_credential",
                AsyncMock(return_value={"credential_index": 4, "user_index": 1}),
            ),
            self._patch_status(user_index=7),
            patch(
                f"{_PROVIDER_MODULE}.clear_lock_credential",
                AsyncMock(side_effect=HomeAssistantError("offline")),
            ),
            pytest.raises(LockOperationFailed, match="under user 7"),
        ):
            await matter_lock.async_set_credential(
                1, self._make_credential(slot=1), "1234", name=None, source="sync"
            )

    async def test_set_credential_filed_under_the_right_user_is_confirmed(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """The lock naming the intended user confirms the write."""
        mock_coordinator = _mock_coordinator()
        matter_lock.coordinator = mock_coordinator
        with (
            patch(
                f"{_PROVIDER_MODULE}.set_lock_credential",
                AsyncMock(return_value={"credential_index": 4, "user_index": 1}),
            ),
            self._patch_status(user_index=1),
        ):
            result = await matter_lock.async_set_credential(
                1, self._make_credential(slot=1), "1234", name=None, source="sync"
            )
        assert result is WriteResult.CONFIRMED
        mock_coordinator.push_update.assert_called_once_with(
            {1: SlotCredential.unreadable()}
        )

    async def test_set_credential_unanswered_status_leaves_the_write_standing(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """A status read that fails proves nothing; the landed write is not undone.

        Raising here would make the seam roll a just-created user back,
        deleting the PIN that just landed and charging the lock breaker for a
        write that succeeded.
        """
        mock_coordinator = _mock_coordinator()
        matter_lock.coordinator = mock_coordinator
        with (
            patch(
                f"{_PROVIDER_MODULE}.set_lock_credential",
                AsyncMock(return_value={"credential_index": 4, "user_index": 1}),
            ),
            patch(
                f"{_PROVIDER_MODULE}.get_lock_credential_status",
                AsyncMock(side_effect=HomeAssistantError("offline")),
            ),
        ):
            result = await matter_lock.async_set_credential(
                1, self._make_credential(slot=1), "1234", name=None, source="sync"
            )
        assert result is WriteResult.CONFIRMED
        mock_coordinator.push_update.assert_called_once_with(
            {1: SlotCredential.unreadable()}
        )

    async def test_set_credential_answer_without_an_index_asks_nothing(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """With no credential index in the answer there is nothing to look up."""
        with (
            patch(
                f"{_PROVIDER_MODULE}.set_lock_credential",
                AsyncMock(return_value={"user_index": 1}),
            ),
            self._patch_status(user_index=7) as status,
        ):
            result = await matter_lock.async_set_credential(
                1, self._make_credential(slot=1), "1234", name=None, source="sync"
            )
        assert result is WriteResult.CONFIRMED
        status.assert_not_awaited()

    async def test_set_credential_duplicate_retry_answer_is_verified_too(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """The write that lands after a duplicate clear is checked like any other."""
        mock_coordinator = _mock_coordinator()
        matter_lock.coordinator = mock_coordinator
        mock_set_credential = AsyncMock(
            side_effect=[
                _make_set_credential_failed_error("duplicate"),
                {"credential_index": 4, "user_index": 1},
            ]
        )
        mock_clear = AsyncMock(return_value={})
        with (
            self._patch_user_with_pin(user_id=1, credential_index=10),
            patch(f"{_PROVIDER_MODULE}.set_lock_credential", mock_set_credential),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
            self._patch_status(user_index=7),
            pytest.raises(LockOperationFailed, match="under user 7"),
        ):
            await matter_lock.async_set_credential(
                1, self._make_credential(slot=1), "1234", name=None, source="sync"
            )
        cleared = [
            call.kwargs["credential_index"] for call in mock_clear.await_args_list
        ]
        assert cleared == [10, 4]  # the duplicate, then the misfiled retry
        mock_coordinator.push_update.assert_not_called()


class TestDeleteCredential:
    """Test async_delete_credential clears Personal Identification Number credentials."""

    @pytest.fixture(autouse=True)
    def _user_owning_slot_pin(self, matter_client: MagicMock) -> AsyncMock:
        """Default the lock to a single user owning a PIN at the slot the tests address.

        async_delete_credential now rediscovers the Matter credential index
        by walking the owning user's credentials; tests that don't override
        this fixture get a baseline lock state where ref.user_id=1 owns a
        PIN whose Matter credential index is also 1 -- enough for the basic
        positive-path tests. Tests that need different lock state can patch
        get_lock_users in their own context manager.
        """
        with (
            get_user_answered_from_listing(matter_client),
            patch(
                f"{_PROVIDER_MODULE}.get_lock_users",
                AsyncMock(
                    return_value={
                        "max_users": 10,
                        "users": [
                            {
                                "user_index": 1,
                                "user_name": "lcm:1:test",
                                "credentials": [{"type": "pin", "index": 1}],
                            },
                            {
                                "user_index": 4,
                                "user_name": "lcm:4:test",
                                "credentials": [{"type": "pin", "index": 4}],
                            },
                        ],
                    },
                ),
            ) as mock,
        ):
            yield mock

    async def test_delete_credential_returns_true(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Successful clear returns True."""
        mock_clear = AsyncMock(return_value=None)
        ref = CredentialRef(user_id=1, type=CredentialType.PIN, slot=1)
        with (
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
        ):
            result = await matter_lock.async_delete_credential(ref)
        assert result is True

    async def test_delete_credential_returns_false_when_user_has_no_pin(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """Delete for a user with no PIN credential is a no-op (returns False).

        The seam resolves a stale owner (e.g. coordinator refresh raced with
        an out-of-band clear); the provider then finds no PIN at that user
        and returns False without calling clear_lock_credential.
        """
        mock_clear = AsyncMock(return_value=None)
        # Override the autouse fixture to seed a user with no PIN credential
        # at ref.user_id=1.
        with (
            patch(
                f"{_PROVIDER_MODULE}.get_lock_users",
                AsyncMock(
                    return_value={
                        "max_users": 10,
                        "users": [
                            {
                                "user_index": 1,
                                "user_name": "lcm:1:test",
                                "credentials": [],
                            },
                        ],
                    },
                ),
            ),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
        ):
            result = await matter_lock.async_delete_credential(
                CredentialRef(user_id=1, type=CredentialType.PIN, slot=1)
            )
        assert result is False
        mock_clear.assert_not_called()

    async def test_delete_credential_transport_error_raises_lock_disconnected(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """A transport HomeAssistantError routes to the retry path (LockDisconnected)."""
        mock_clear = AsyncMock(side_effect=HomeAssistantError("transport down"))
        ref = CredentialRef(user_id=1, type=CredentialType.PIN, slot=1)
        with (
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
            pytest.raises(LockDisconnected),
        ):
            await matter_lock.async_delete_credential(ref)

    async def test_delete_credential_matter_client_exception_routes_to_retry(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """``InvalidState: Not connected`` during clear routes to retry (#1257)."""
        mock_clear = AsyncMock(side_effect=MatterClientException("Not connected"))
        ref = CredentialRef(user_id=1, type=CredentialType.PIN, slot=1)
        with (
            patch.object(
                matter_lock,
                "_find_pin_credential_index_for_user",
                AsyncMock(return_value=5),
            ),
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
            pytest.raises(LockDisconnected),
        ):
            await matter_lock.async_delete_credential(ref)

    async def test_delete_credential_validation_error_raises_operation_failed(
        self, hass: HomeAssistant, matter_lock: MatterLock
    ) -> None:
        """A ServiceValidationError is a reachable-but-failed operation."""
        mock_clear = AsyncMock(side_effect=ServiceValidationError("bad"))
        ref = CredentialRef(user_id=1, type=CredentialType.PIN, slot=1)
        with (
            patch(f"{_PROVIDER_MODULE}.clear_lock_credential", mock_clear),
            pytest.raises(LockOperationFailed),
        ):
            await matter_lock.async_delete_credential(ref)
