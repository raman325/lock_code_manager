"""Test the Z-Wave JS lock provider."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from zwave_js_server.const import CommandClass, NodeStatus
from zwave_js_server.const.command_class.access_control import (
    UserCredentialType,
    UserCredentialUserType,
)
from zwave_js_server.exceptions import FailedZWaveCommand
from zwave_js_server.model.access_control import CredentialData, UserData
from zwave_js_server.model.node import Node

from homeassistant.components.zwave_js import lock_helpers
from homeassistant.components.zwave_js.const import DOMAIN as ZWAVE_JS_DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
)
from custom_components.lock_code_manager.domain.credentials import (
    Credential,
    CredentialRef,
    CredentialType,
    CredentialTypeCapability,
    LockCapabilities,
    SetUserResult,
    User,
    WriteResult,
)
from custom_components.lock_code_manager.domain.exceptions import (
    CodeRejectedError,
    DuplicateCodeError,
    LockCodeManagerProviderError,
    LockDisconnected,
    LockOperationFailed,
)
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers.zwave_js import ZWaveJSLock
from tests.providers.helpers import ProviderNativeTransportContractTests

# Properties tests


class TestNativeTransportContract(ProviderNativeTransportContractTests):
    """Z-Wave JS routes a native ``BaseZwaveJSServerError`` to LockDisconnected.

    The unified ``access_control`` read raises ``zwave_js_server`` errors (e.g.
    ``FailedZWaveCommand``) that are independent of ``HomeAssistantError``; they
    must surface as ``LockDisconnected`` rather than escaping to the sync
    catch-all (issue #1257).
    """

    native_transport_exception = FailedZWaveCommand("cmd", 1, "node gone")

    @pytest.fixture
    def provider_lock(
        self,
        zwave_js_lock: ZWaveJSLock,
        mock_access_control: MagicMock,
        mock_lock_helpers: dict,
    ) -> ZWaveJSLock:
        """A lock on the unified path with empty data and mocked capabilities."""
        mock_access_control.get_users_cached.return_value = []
        mock_access_control.get_all_credentials_cached.return_value = []
        return zwave_js_lock

    def inject_native_transport_error(self, hass, provider_lock):
        """Fail the lowest unified read call (``get_users_cached``)."""
        # ``node.access_control`` is the class-patched mock_access_control.
        provider_lock.node.access_control.get_users_cached.side_effect = (
            self.native_transport_exception
        )
        return


async def test_domain(zwave_js_lock: ZWaveJSLock) -> None:
    """Test domain property returns zwave_js."""
    assert zwave_js_lock.domain == ZWAVE_JS_DOMAIN


async def test_supports_push(zwave_js_lock: ZWaveJSLock) -> None:
    """Test that Z-Wave JS locks support push updates."""
    assert zwave_js_lock.supports_push is True


async def test_connection_check_interval_is_none(zwave_js_lock: ZWaveJSLock) -> None:
    """Test that connection check interval is None (uses config entry state)."""
    assert zwave_js_lock.connection_check_interval is None


async def test_supports_native_users(zwave_js_lock: ZWaveJSLock) -> None:
    """Test that Z-Wave JS lock reports supports_native_users=True."""
    assert zwave_js_lock.supports_native_users is True


async def test_node_property(
    zwave_js_lock: ZWaveJSLock,
    lock_schlage_be469: Node,
) -> None:
    """Test node property returns the correct Z-Wave node."""
    node = zwave_js_lock.node
    assert node.node_id == lock_schlage_be469.node_id


async def test_node_raises_lock_disconnected_when_entry_not_loaded(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Node resolution while the zwave_js entry is down maps to LockDisconnected.

    Home Assistant's async_get_node_from_entity_id raises a raw ValueError
    when the zwave_js config entry is not loaded (issue #1321). The provider
    must translate that into LockDisconnected so the base class routes it to
    the degraded-setup/retry path instead of dropping the lock.
    """
    await hass.config_entries.async_unload(zwave_integration.entry_id)
    await hass.async_block_till_done()

    with pytest.raises(LockDisconnected):
        _ = zwave_js_lock.node


async def test_setup_is_idempotent(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    lock_code_manager_config_entry: MockConfigEntry,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """Test that async_setup clears old listeners before re-registering."""
    await zwave_js_lock.async_setup(lock_code_manager_config_entry)
    assert len(zwave_js_lock._listeners) >= 1
    count_after_first = len(zwave_js_lock._listeners)

    # Call again -- should not accumulate listeners.
    await zwave_js_lock.async_setup(lock_code_manager_config_entry)
    assert len(zwave_js_lock._listeners) == count_after_first


# Connection tests


async def test_is_integration_connected_when_loaded(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test connection is up when config entry is loaded and client connected."""
    assert zwave_integration.state == ConfigEntryState.LOADED
    assert await zwave_js_lock.async_is_integration_connected() is True


async def test_is_integration_not_connected_when_not_loaded(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test connection is down when config entry not loaded."""
    await hass.config_entries.async_unload(zwave_integration.entry_id)
    await hass.async_block_till_done()

    assert zwave_integration.state != ConfigEntryState.LOADED
    assert await zwave_js_lock.async_is_integration_connected() is False


# Setup/unload tests


async def test_setup_registers_event_listener(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """Test that setup registers an event listener for Z-Wave JS events."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)

    assert len(zwave_js_lock._listeners) == 0

    await zwave_js_lock.async_setup_internal(lcm_entry)

    assert len(zwave_js_lock._listeners) == 1

    await zwave_js_lock.async_unload(False)

    assert len(zwave_js_lock._listeners) == 0


async def test_unload_cleans_up_push_subscription(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """Test that unload cleans up push subscriptions."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await zwave_js_lock.async_setup_internal(lcm_entry)

    zwave_js_lock.subscribe_push_updates()
    assert zwave_js_lock._push_unsubs

    await zwave_js_lock.async_unload(False)
    assert not zwave_js_lock._push_unsubs


# Hard refresh tests


async def test_hard_refresh_codes_calls_access_control(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """
    Test that async_hard_refresh_codes refreshes both users and credentials from the lock.

    The non-cached methods (get_users and get_all_credentials) are called to force a
    fresh read from the device, bypassing any cached state. The result is then projected
    through the base class async_get_usercodes which calls the cached reads.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"1": {}, "2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)

    codes = await zwave_js_lock.async_hard_refresh_codes()

    mock_access_control.get_users.assert_called_once()
    mock_access_control.get_all_credentials.assert_called_once()
    assert isinstance(codes, dict)
    # Managed slots 1 and 2 are present (empty since mock returns no users)
    assert 1 in codes
    assert 2 in codes


async def test_hard_refresh_codes_maps_transport_error_to_lock_disconnected(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """Z-Wave transport failure during hard refresh surfaces as LockDisconnected."""
    mock_access_control.get_users.side_effect = FailedZWaveCommand(
        "cmd", 1, "node gone"
    )

    with pytest.raises(LockDisconnected, match="hard refresh failed"):
        await zwave_js_lock.async_hard_refresh_codes()


async def test_hard_refresh_codes_maps_ha_error_to_operation_failed(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """A reachable-but-rejected hard refresh surfaces as LockOperationFailed."""
    mock_access_control.get_users.side_effect = HomeAssistantError("rejected")

    with pytest.raises(LockOperationFailed, match="hard refresh failed"):
        await zwave_js_lock.async_hard_refresh_codes()


# Base orchestration integration tests


async def test_async_get_usercodes_returns_projection_with_managed_slots(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """
    Test that async_get_usercodes projects lock users to slot-keyed credentials.

    The base implementation ensures every managed slot is present in the result
    (starting from empty), and overlays any Personal Identification Number
    credentials returned by async_get_users. With no users on the lock, all
    managed slots are present and empty.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"1": {}, "2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    # No users on the lock
    mock_access_control.get_users_cached.return_value = []
    mock_access_control.get_all_credentials_cached.return_value = []

    codes = await zwave_js_lock.async_get_usercodes()

    assert codes[1] == SlotCredential.empty()
    assert codes[2] == SlotCredential.empty()


async def test_async_get_usercodes_overlays_pin_credentials(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """
    Test that async_get_usercodes projects Personal Identification Number credentials onto slot keys.

    When the lock has a user with a PIN credential at slot 1, the result maps slot 1
    to the readable credential value.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"1": {}, "2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    mock_access_control.get_users_cached.return_value = [
        UserData(
            user_id=1,
            active=True,
            user_type=UserCredentialUserType.GENERAL,
            user_name="alice",
        ),
    ]
    mock_access_control.get_all_credentials_cached.return_value = [
        CredentialData(
            user_id=1,
            type=UserCredentialType.PIN_CODE,
            slot=1,
            data="9999",
        ),
    ]

    codes = await zwave_js_lock.async_get_usercodes()

    assert codes[1] == SlotCredential.known("9999")
    assert codes[2] == SlotCredential.empty()


async def test_async_internal_set_usercode_calls_primitives(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
    zwave_integration: MockConfigEntry,
) -> None:
    """
    Test that async_internal_set_usercode drives the base User->Credential orchestration.

    With supports_native_users=True and the connection up, the base class runs
    async_set_user (to create or update the user) then async_set_credential (to write
    the Personal Identification Number), both delegating to lock_helpers.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"1": {}},
        },
    )
    lcm_entry.add_to_hass(hass)

    # Disable the operation delay so the test does not wait
    zwave_js_lock._min_operation_delay = 0.0
    mock_access_control.get_user_cached.return_value = None

    await zwave_js_lock.async_internal_set_usercode(
        1, "5678", name="alice", source="sync"
    )

    mock_lock_helpers["async_set_user"].assert_called_once()
    mock_lock_helpers["async_set_credential"].assert_called_once()
    # Verify the credential call carries the right Personal Identification Number and slot
    call_args = mock_lock_helpers["async_set_credential"].call_args
    assert call_args.args[1] == 1  # user_id from set_user result
    assert call_args.args[2] == UserCredentialType.PIN_CODE
    assert call_args.args[3] == "5678"
    assert call_args.kwargs["credential_slot"] == 1


async def test_async_internal_clear_usercode_calls_delete_primitives(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
    zwave_integration: MockConfigEntry,
) -> None:
    """
    Test that async_internal_clear_usercode deletes only the credential.

    With supports_native_users=True the base class resolves the credential
    owner from async_get_users and calls async_delete_credential. The user
    record is now an LCM-managed slot anchor (per the user-tag idempotency
    design) and survives PIN clear cycles -- teardown happens only via
    async_release_managed_slot when the slot itself is removed from LCM.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"1": {}},
        },
    )
    lcm_entry.add_to_hass(hass)

    zwave_js_lock._min_operation_delay = 0.0
    # Seed the lock with one user owning one PIN at slot 1
    mock_access_control.get_users_cached.return_value = [
        UserData(
            user_id=1,
            active=True,
            user_type=UserCredentialUserType.GENERAL,
            user_name="alice",
        ),
    ]
    mock_access_control.get_all_credentials_cached.return_value = [
        CredentialData(
            user_id=1,
            type=UserCredentialType.PIN_CODE,
            slot=1,
            data="5678",
        ),
    ]

    await zwave_js_lock.async_internal_clear_usercode(1, source="sync")

    mock_lock_helpers["async_delete_credential"].assert_called_once()
    mock_lock_helpers["async_delete_user"].assert_not_called()


# Device availability tests


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (NodeStatus.ALIVE, True),
        (NodeStatus.ASLEEP, True),
        (NodeStatus.UNKNOWN, True),
        (NodeStatus.DEAD, False),
    ],
)
async def test_is_device_available_by_status(
    zwave_js_lock: ZWaveJSLock,
    lock_schlage_be469: Node,
    status: NodeStatus,
    expected: bool,
) -> None:
    """Test async_is_device_available for each node status."""
    with patch.object(
        type(lock_schlage_be469),
        "status",
        new_callable=lambda: property(lambda self: status),
    ):
        assert await zwave_js_lock.async_is_device_available() is expected


async def test_is_device_available_returns_false_on_exception(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
) -> None:
    """Test that async_is_device_available returns False when node access raises."""

    def raise_error(self):
        raise RuntimeError("node gone")

    with patch.object(
        type(zwave_js_lock),
        "node",
        new_callable=lambda: property(raise_error),
    ):
        assert await zwave_js_lock.async_is_device_available() is False


# Credential API tests (Option B: readable PINs via node.access_control)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ("1234", SlotCredential.known("1234")),
        (b"1234", SlotCredential.known("1234")),
        (None, SlotCredential.unreadable()),
        ("", SlotCredential.unreadable()),
        # Masked/withheld codes carry no usable value -> unreadable, NOT
        # known("****") (which would surface a wrong PIN and never reconcile).
        ("****", SlotCredential.unreadable()),
        ("**********", SlotCredential.unreadable()),
    ],
    ids=["known_str", "known_bytes", "none", "empty", "masked", "masked_long"],
)
async def test_pin_state_projects_masked_codes_as_unreadable(
    zwave_js_lock: ZWaveJSLock,
    data: str | bytes | None,
    expected: SlotCredential,
) -> None:
    """The unified read projection maps masked/withheld codes to unreadable.

    A masked code read through the unified access-control path must not be
    mistaken for a readable PIN (issue #1251 working-capability variant).
    """
    assert zwave_js_lock._pin_state(data) == expected


async def test_async_get_users_returns_all_mappable_credential_types(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """
    async_get_users returns every credential type the domain represents.

    The base orchestration filters to Personal Identification Number at
    the slot-projection layer via ``user.pin_credentials``, so the
    provider stores RFID/NFC/etc. alongside PINs rather than dropping
    them up front. Readable Personal Identification Number data becomes
    SlotCredential.known; non-PIN data is opaque to the integration so
    those credentials surface as SlotCredential.unreadable.
    """
    mock_access_control.get_users_cached.return_value = [
        UserData(
            user_id=1,
            active=True,
            user_type=UserCredentialUserType.GENERAL,
            user_name="alice",
        ),
        UserData(
            user_id=2,
            active=True,
            user_type=UserCredentialUserType.GENERAL,
            user_name=None,
        ),
        # User with no credentials at all -> projects to an empty list.
        UserData(
            user_id=3,
            active=True,
            user_type=UserCredentialUserType.GENERAL,
            user_name="carol",
        ),
    ]
    mock_access_control.get_all_credentials_cached.return_value = [
        CredentialData(
            user_id=1,
            type=UserCredentialType.PIN_CODE,
            slot=1,
            data="1234",
        ),
        CredentialData(
            user_id=2,
            type=UserCredentialType.PIN_CODE,
            slot=2,
            data=None,
        ),
        # Non-PIN credential — now retained alongside the PIN, surfaced
        # as unreadable so direct callers see it without exposing an
        # opaque tag identifier to the slot-projection layer.
        CredentialData(
            user_id=1,
            type=UserCredentialType.RFID_CODE,
            slot=1,
            data="AABB",
        ),
        # Z-Wave type with no domain equivalent — dropped.
        CredentialData(
            user_id=1,
            type=UserCredentialType.BLE,
            slot=2,
            data="raw",
        ),
    ]

    users = await zwave_js_lock.async_get_users()

    assert len(users) == 3
    user3 = next(u for u in users if u.user_id == 3)
    assert user3.credentials == []

    user1 = next(u for u in users if u.user_id == 1)
    assert user1.name == "alice"
    # PIN_CODE -> CredentialType.PIN (known), RFID_CODE -> CredentialType.RFID
    # (unreadable, opaque tag), BLE dropped.
    assert user1.credentials == [
        Credential(
            type=CredentialType.PIN,
            slot=1,
            state=SlotCredential.known("1234"),
        ),
        Credential(
            type=CredentialType.RFID,
            slot=1,
            state=SlotCredential.unreadable(),
        ),
    ]
    # And pin_credentials still surfaces only the PIN so the base
    # orchestration's slot projection is unaffected.
    assert user1.pin_credentials == [
        Credential(
            type=CredentialType.PIN,
            slot=1,
            state=SlotCredential.known("1234"),
        ),
    ]

    user2 = next(u for u in users if u.user_id == 2)
    assert user2.credentials == [
        Credential(
            type=CredentialType.PIN,
            slot=2,
            state=SlotCredential.unreadable(),
        ),
    ]


async def test_async_get_users_drops_orphan_credentials(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """
    A credential whose user_id is not in the users list is silently dropped.

    This is the Z-Wave equivalent of a stale credential row that lost its
    owner; surfacing it under a synthetic user would invent state, so the
    safer projection is to ignore it and let the next hard refresh clean
    things up.
    """
    mock_access_control.get_users_cached.return_value = [
        UserData(
            user_id=1,
            active=True,
            user_type=UserCredentialUserType.GENERAL,
            user_name="alice",
        ),
    ]
    mock_access_control.get_all_credentials_cached.return_value = [
        CredentialData(
            user_id=1,
            type=UserCredentialType.PIN_CODE,
            slot=1,
            data="1234",
        ),
        # Orphan: no matching user record.
        CredentialData(
            user_id=99,
            type=UserCredentialType.PIN_CODE,
            slot=2,
            data="9999",
        ),
    ]

    users = await zwave_js_lock.async_get_users()
    assert len(users) == 1
    assert users[0].user_id == 1
    assert len(users[0].credentials) == 1


async def test_async_get_capabilities_maps_lock_helpers_response(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """
    Test async_get_capabilities maps the lock_helpers response to LockCapabilities.

    The raw dict from async_get_credential_capabilities is projected to the
    domain LockCapabilities type, pulling the Personal Identification Number
    entry from supported_credential_types.
    """
    pin_type_str = lock_helpers.CREDENTIAL_TYPE_MAP[UserCredentialType.PIN_CODE]
    mock_lock_helpers["async_get_credential_capabilities"].return_value = {
        "supports_user_management": True,
        "max_users": 30,
        "supported_user_types": [],
        "max_user_name_length": 10,
        "supported_credential_rules": [],
        "supported_credential_types": {
            pin_type_str: {
                "num_slots": 30,
                "min_length": 4,
                "max_length": 8,
                "supports_learn": False,
            }
        },
    }

    caps = await zwave_js_lock.async_get_capabilities()

    assert caps == LockCapabilities(
        supports_user_management=True,
        max_users=30,
        credential_types={
            CredentialType.PIN: CredentialTypeCapability(
                num_slots=30,
                min_length=4,
                max_length=8,
                supports_learn=False,
            )
        },
        max_user_name_length=10,
    )


def _zero_slot_caps() -> dict:
    """Return a degenerate capabilities response: PIN type advertised, 0 slots."""
    pin_type_str = lock_helpers.CREDENTIAL_TYPE_MAP[UserCredentialType.PIN_CODE]
    return {
        "supports_user_management": False,
        "max_users": 0,
        "supported_user_types": [],
        "max_user_name_length": 0,
        "supported_credential_rules": [],
        "supported_credential_types": {
            pin_type_str: {
                "num_slots": 0,
                "min_length": 4,
                "max_length": 10,
                "supports_learn": False,
            }
        },
    }


def _healthy_caps() -> dict:
    """Return a healthy capabilities response with 30 PIN slots."""
    pin_type_str = lock_helpers.CREDENTIAL_TYPE_MAP[UserCredentialType.PIN_CODE]
    return {
        "supports_user_management": False,
        "max_users": 0,
        "supported_user_types": [],
        "max_user_name_length": 0,
        "supported_credential_rules": [],
        "supported_credential_types": {
            pin_type_str: {
                "num_slots": 30,
                "min_length": 4,
                "max_length": 10,
                "supports_learn": False,
            }
        },
    }


async def test_async_get_capabilities_zero_slots_recovers_via_users_count_query(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """
    A degenerate ``num_slots == 0`` probe recovers by re-querying the users count.

    The driver derives a User Code CC lock's slot count from the cached
    ``supportedUsers`` value, defaulting to 0 when it is missing from the value
    DB (e.g. the interview completed while the lock was asleep after a
    re-inclusion). ``UserCodeCC.refreshValues`` reads that same cached value, so
    the only primitive that repopulates it is the CC API ``getUsersCount``
    device query. The provider invokes it once and re-reads capabilities before
    concluding the lock is unusable (issue #1298 follow-up).
    """
    mock_lock_helpers["async_get_credential_capabilities"].side_effect = [
        _zero_slot_caps(),
        _healthy_caps(),
    ]
    invoke = AsyncMock()
    with patch.object(zwave_js_lock.node.endpoints[0], "async_invoke_cc_api", invoke):
        caps = await zwave_js_lock.async_get_capabilities()

    invoke.assert_awaited_once_with(CommandClass.USER_CODE, "getUsersCount")
    assert caps.credential_types[CredentialType.PIN].num_slots == 30
    assert mock_lock_helpers["async_get_credential_capabilities"].await_count == 2


async def test_async_get_capabilities_zero_slots_raises_actionable_error(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """
    A degenerate ``num_slots == 0`` capability probe fails with an actionable error.

    The unified API reports a PIN credential type but zero usable slots when the
    node's interview is incomplete (values missing from the DB) or the connected
    Z-Wave JS driver predates the spec-compliant capability fix (15.24.3). The
    one-shot ``getUsersCount`` recovery query runs first; when the re-read is
    still degenerate, the provider surfaces a structural
    ``LockCodeManagerProviderError`` that points at the actual remedies:
    re-interview the lock and update Z-Wave JS (see issue #1298).
    """
    mock_lock_helpers[
        "async_get_credential_capabilities"
    ].return_value = _zero_slot_caps()
    invoke = AsyncMock()
    with patch.object(zwave_js_lock.node.endpoints[0], "async_invoke_cc_api", invoke):
        with pytest.raises(LockCodeManagerProviderError, match="no usable PIN slots"):
            await zwave_js_lock.async_get_capabilities()

    invoke.assert_awaited_once_with(CommandClass.USER_CODE, "getUsersCount")
    assert mock_lock_helpers["async_get_credential_capabilities"].await_count == 2


async def test_async_get_capabilities_zero_slots_recovery_query_failure_still_raises(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """
    A failed recovery query falls through to the actionable structural error.

    The ``getUsersCount`` device query is best-effort: when it fails (node
    unreachable, command error), the provider raises the same actionable error
    without re-reading capabilities.
    """
    mock_lock_helpers[
        "async_get_credential_capabilities"
    ].return_value = _zero_slot_caps()
    invoke = AsyncMock(side_effect=FailedZWaveCommand("cmd", 1, "node asleep"))
    with patch.object(zwave_js_lock.node.endpoints[0], "async_invoke_cc_api", invoke):
        with pytest.raises(LockCodeManagerProviderError, match="no usable PIN slots"):
            await zwave_js_lock.async_get_capabilities()

    assert mock_lock_helpers["async_get_credential_capabilities"].await_count == 1


async def test_async_get_capabilities_no_pin_type_returns_empty(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """
    A lock that advertises no PIN credential type at all yields empty capabilities.

    This is the genuinely-unsupported case (distinct from zero slots): the base
    ``async_setup_internal`` rejects it with the generic "does not advertise PIN
    credential support", which is accurate here.
    """
    mock_lock_helpers["async_get_credential_capabilities"].return_value = {
        "supports_user_management": False,
        "max_users": 0,
        "supported_user_types": [],
        "max_user_name_length": 0,
        "supported_credential_rules": [],
        "supported_credential_types": {},
    }

    caps = await zwave_js_lock.async_get_capabilities()

    assert CredentialType.PIN not in caps.credential_types


# Write primitive tests (Task 2)


async def test_async_set_user_returns_created_when_no_tagged_user_exists(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """
    ``async_set_user`` reports ``created=True`` and allocates a new user_id.

    With no existing tagged user for slot 5 (and no legacy adoption
    target), the provider delegates to the upstream helper with
    ``user_id=None`` so Z-Wave finds the first free user_id. The
    returned ``user_id`` carries the allocated value.
    """
    mock_access_control.get_users_cached.return_value = []
    mock_access_control.get_all_credentials_cached.return_value = []
    mock_lock_helpers["async_set_user"].return_value = {"user_id": 12}

    user = User(user_id=5, name="lcm:5:alice", active=True)
    result = await zwave_js_lock.async_set_user(user)

    assert result == SetUserResult(user_id=12, created=True)
    mock_lock_helpers["async_set_user"].assert_called_once_with(
        zwave_js_lock.node,
        user_id=None,
        user_name="lcm:5:alice",
        active=True,
    )


async def test_async_set_user_returns_not_created_when_tagged_user_exists(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """
    ``async_set_user`` reports ``created=False`` when a tagged user already exists.

    Seeds a lock user at ``user_id=17`` whose name carries the
    ``lcm:3:`` tag; the provider must discover it via the tag (NOT via
    ``user_id == slot``) and dispatch an UPDATE to that user_id, not a
    CREATE.
    """
    mock_access_control.get_users_cached.return_value = [
        UserData(
            user_id=17,
            active=True,
            user_type=UserCredentialUserType.GENERAL,
            user_name="lcm:3:bob",
        ),
    ]
    mock_access_control.get_all_credentials_cached.return_value = []
    mock_lock_helpers["async_set_user"].return_value = {"user_id": 17}

    user = User(user_id=3, name="lcm:3:bob", active=True)
    result = await zwave_js_lock.async_set_user(user)

    assert result == SetUserResult(user_id=17, created=False)
    mock_lock_helpers["async_set_user"].assert_called_once_with(
        zwave_js_lock.node,
        user_id=17,
        user_name="lcm:3:bob",
        active=True,
    )


async def test_async_set_user_adopts_legacy_user_at_user_id_equals_slot(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """
    Pre-PR-C upgrade path: adopt an untagged user at ``user_id == slot``.

    Pre-PR-C LCM pinned ``user_id`` to the LCM slot. On the first
    write after upgrade, the new code must find that legacy user (it
    has no tag, but it sits at the old invariant) and UPDATE it rather
    than CREATE a second user, preserving the existing PIN credential.
    """
    mock_access_control.get_users_cached.return_value = [
        UserData(
            user_id=4,  # legacy user_id == LCM slot
            active=True,
            user_type=UserCredentialUserType.GENERAL,
            user_name="Carol",  # untagged
        ),
    ]
    mock_access_control.get_all_credentials_cached.return_value = [
        CredentialData(
            user_id=4,
            type=UserCredentialType.PIN_CODE,
            slot=4,  # legacy credential.slot == LCM slot
            data="1234",
        ),
    ]
    mock_lock_helpers["async_set_user"].return_value = {"user_id": 4}

    user = User(user_id=4, name="lcm:4:carol", active=True)
    result = await zwave_js_lock.async_set_user(user)

    assert result == SetUserResult(user_id=4, created=False)
    mock_lock_helpers["async_set_user"].assert_called_once_with(
        zwave_js_lock.node,
        user_id=4,
        user_name="lcm:4:carol",
        active=True,
    )


async def test_async_set_user_legacy_pass_skips_users_tagged_for_other_slots(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """
    Legacy adoption must not steal a user already tagged for a different slot.

    The legacy probe matches ``user_id == slot`` AND a PIN at the same
    slot. A user that happens to satisfy both halves but already
    carries an LCM tag for a *different* slot is NOT the slot's legacy
    anchor; adopting it would re-bind one slot's user as another
    slot's anchor and silently corrupt both slots' state.
    """
    mock_access_control.get_users_cached.return_value = [
        UserData(
            user_id=2,
            active=True,
            user_type=UserCredentialUserType.GENERAL,
            user_name="lcm:9:elsewhere",  # tagged for slot 9, not 2
        ),
    ]
    mock_access_control.get_all_credentials_cached.return_value = [
        CredentialData(
            user_id=2,
            type=UserCredentialType.PIN_CODE,
            slot=2,
            data="0000",
        ),
    ]
    mock_lock_helpers["async_set_user"].return_value = {"user_id": 88}

    user = User(user_id=2, name="lcm:2:newcomer", active=True)
    result = await zwave_js_lock.async_set_user(user)

    # Legacy pass skipped -> CREATE, not adoption.
    assert result == SetUserResult(user_id=88, created=True)
    mock_lock_helpers["async_set_user"].assert_called_once_with(
        zwave_js_lock.node,
        user_id=None,
        user_name="lcm:2:newcomer",
        active=True,
    )


async def test_async_set_credential_returns_true_on_success(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """
    Test async_set_credential returns True and calls the helper with the right args.

    The helper is called with the node, user_id, PIN_CODE credential type, the
    readable Personal Identification Number string, and the credential slot.
    """
    mock_lock_helpers["async_set_credential"].return_value = {
        "credential_slot": 2,
        "user_id": 1,
    }

    credential = Credential(
        type=CredentialType.PIN, slot=2, state=SlotCredential.known("5678")
    )
    result = await zwave_js_lock.async_set_credential(
        user_id=1,
        credential=credential,
        pin="5678",
        name="alice",
        source="sync",
    )

    assert result is WriteResult.CONFIRMED
    mock_lock_helpers["async_set_credential"].assert_called_once_with(
        zwave_js_lock.node,
        1,
        UserCredentialType.PIN_CODE,
        "5678",
        credential_slot=2,
    )


async def test_async_set_credential_raises_duplicate_code_error(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """
    Test async_set_credential raises DuplicateCodeError on duplicate rejection.

    When the lock_helpers helper raises HomeAssistantError with
    translation_key="credential_rejected_duplicate", the provider must re-raise
    as DuplicateCodeError so the seam's orchestration can handle it correctly.
    """
    err = HomeAssistantError(translation_key="credential_rejected_duplicate")
    mock_lock_helpers["async_set_credential"].side_effect = err

    credential = Credential(
        type=CredentialType.PIN, slot=3, state=SlotCredential.known("1111")
    )
    with pytest.raises(DuplicateCodeError) as exc_info:
        await zwave_js_lock.async_set_credential(
            user_id=1,
            credential=credential,
            pin="1111",
            name=None,
            source="sync",
        )

    assert exc_info.value.code_slot == 3
    assert exc_info.value.lock_entity_id == zwave_js_lock.lock.entity_id


async def test_async_set_credential_raises_code_rejected_error_on_other_ha_error(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """
    Test async_set_credential raises CodeRejectedError for definitive rejections.

    When the lock_helpers helper raises HomeAssistantError with a definitive
    rejection translation_key (for example "credential_rejected_manufacturer_rules"),
    the provider must re-raise as CodeRejectedError.
    """
    err = HomeAssistantError(translation_key="credential_rejected_manufacturer_rules")
    mock_lock_helpers["async_set_credential"].side_effect = err

    credential = Credential(
        type=CredentialType.PIN, slot=4, state=SlotCredential.known("2222")
    )
    with pytest.raises(CodeRejectedError) as exc_info:
        await zwave_js_lock.async_set_credential(
            user_id=1,
            credential=credential,
            pin="2222",
            name=None,
            source="sync",
        )

    assert exc_info.value.code_slot == 4
    assert not isinstance(exc_info.value, DuplicateCodeError)


async def test_async_set_credential_error_unknown_returns_optimistic(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """
    A driver ERROR_UNKNOWN (credential_rejected_unknown) is treated as a
    completed set, not a rejection.

    The driver returns ERROR_UNKNOWN when its post-write verification can't
    confirm the code -- notably for locks that report the user code back
    masked, where the write actually succeeded (issue #1251). The provider
    must return OPTIMISTIC so the seam records it pending and lets a
    confirmation (push/refresh) verify it, instead of raising CodeRejectedError
    (which would permanently disable an accepted write).
    """
    mock_lock_helpers["async_set_credential"].side_effect = HomeAssistantError(
        translation_key="credential_rejected_unknown"
    )

    credential = Credential(
        type=CredentialType.PIN, slot=4, state=SlotCredential.known("2222")
    )
    result = await zwave_js_lock.async_set_credential(
        user_id=1,
        credential=credential,
        pin="2222",
        name=None,
        source="sync",
    )

    assert result is WriteResult.OPTIMISTIC


async def test_async_set_credential_maps_failed_command_to_lock_disconnected(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """
    A transient Z-Wave command failure routes to the retry path, not a suspend.

    FailedZWaveCommand is not a HomeAssistantError; if it escaped, the sync
    manager would treat it as an unexpected bug and suspend the slot. It must
    surface as LockDisconnected so the lock is retried instead.
    """
    mock_lock_helpers["async_set_credential"].side_effect = FailedZWaveCommand(
        "cmd", 1, "lock asleep"
    )
    credential = Credential(
        type=CredentialType.PIN, slot=4, state=SlotCredential.known("2222")
    )
    with pytest.raises(LockDisconnected):
        await zwave_js_lock.async_set_credential(
            user_id=1,
            credential=credential,
            pin="2222",
            name=None,
            source="sync",
        )


async def test_async_delete_credential_maps_failed_command_to_lock_disconnected(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """A transient delete failure also routes to the retry path."""
    mock_lock_helpers["async_delete_credential"].side_effect = FailedZWaveCommand(
        "cmd", 1, "lock asleep"
    )
    with pytest.raises(LockDisconnected):
        await zwave_js_lock.async_delete_credential(
            CredentialRef(user_id=4, type=CredentialType.PIN, slot=4)
        )


async def test_hard_refresh_interval_disabled(zwave_js_lock: ZWaveJSLock) -> None:
    """No periodic drift poll: Z-Wave trusts its event stream end to end."""
    assert zwave_js_lock.hard_refresh_interval is None


async def test_async_delete_user_calls_helper(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """
    Test async_delete_user calls lock_helpers.async_delete_user with correct args.

    The method delegates directly to the helper with the node and user_id.
    """
    await zwave_js_lock.async_delete_user(7)

    mock_lock_helpers["async_delete_user"].assert_called_once_with(
        zwave_js_lock.node, 7
    )


async def test_async_delete_credential_calls_helper_and_returns_true(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """
    Test async_delete_credential calls the helper and returns True on success.

    The helper is called with the node, user_id, PIN_CODE credential type, and
    the credential slot resolved from the CredentialRef.
    """
    ref = CredentialRef(user_id=2, type=CredentialType.PIN, slot=5)
    result = await zwave_js_lock.async_delete_credential(ref)

    assert result is True
    mock_lock_helpers["async_delete_credential"].assert_called_once_with(
        zwave_js_lock.node,
        2,
        UserCredentialType.PIN_CODE,
        5,
    )


# ── Exception wrapping for read primitives ──────────────────────────


async def test_async_get_users_raises_lock_disconnected_on_zwave_error(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """BaseZwaveJSServerError from access_control reads surfaces as LockDisconnected."""
    mock_access_control.get_users_cached.side_effect = FailedZWaveCommand(
        "cmd", 1, "server error"
    )
    with pytest.raises(LockDisconnected):
        await zwave_js_lock.async_get_users()


async def test_async_get_users_raises_lock_operation_failed_on_ha_error(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """HomeAssistantError from access_control reads surfaces as LockOperationFailed."""
    mock_access_control.get_users_cached.side_effect = HomeAssistantError("boom")
    with pytest.raises(LockOperationFailed):
        await zwave_js_lock.async_get_users()


async def test_async_get_capabilities_raises_lock_disconnected_on_zwave_error(
    zwave_js_lock: ZWaveJSLock,
    mock_lock_helpers: dict,
) -> None:
    """BaseZwaveJSServerError from lock_helpers surfaces as LockDisconnected."""
    mock_lock_helpers[
        "async_get_credential_capabilities"
    ].side_effect = FailedZWaveCommand("cmd", 1, "server error")
    with pytest.raises(LockDisconnected):
        await zwave_js_lock.async_get_capabilities()


async def test_async_get_capabilities_raises_lock_operation_failed_on_ha_error(
    zwave_js_lock: ZWaveJSLock,
    mock_lock_helpers: dict,
) -> None:
    """HomeAssistantError from lock_helpers surfaces as LockOperationFailed."""
    mock_lock_helpers[
        "async_get_credential_capabilities"
    ].side_effect = HomeAssistantError("boom")
    with pytest.raises(LockOperationFailed):
        await zwave_js_lock.async_get_capabilities()


# ── Name length validation in async_set_user ───────────────────────


async def test_set_usercode_user_code_cc_skips_set_user_and_writes_credential_only(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """
    On a User Code CC lock, the seam orchestration skips set_user entirely.

    The unified accessControl API's setUser cannot run before a credential
    exists on a UC lock (the user IS the code), so the base orchestration
    in _set_credential takes the no-user-write path when the capabilities
    report ``max_user_name_length == 0``. async_set_credential creates
    the user implicitly. End-to-end check via async_set_usercode covers
    the gate, the cache, and the provider-level credential write.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"5": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    # UC-shaped capabilities: real user-record support is hardcoded to
    # True by the driver, but max_user_name_length == 0 signals the user
    # is implicit in the credential.
    pin_type_str = lock_helpers.CREDENTIAL_TYPE_MAP[UserCredentialType.PIN_CODE]
    mock_lock_helpers["async_get_credential_capabilities"].return_value = {
        "supports_user_management": True,
        "max_users": 30,
        "supported_user_types": [],
        "max_user_name_length": 0,
        "supported_credential_rules": [],
        "supported_credential_types": {
            pin_type_str: {
                "num_slots": 30,
                "min_length": 4,
                "max_length": 8,
                "supports_learn": False,
            }
        },
    }

    changed = await zwave_js_lock.async_set_usercode(5, "9999", name="alice")

    assert changed is WriteResult.CONFIRMED
    mock_lock_helpers["async_set_user"].assert_not_called()
    mock_lock_helpers["async_set_credential"].assert_called_once()


async def test_async_set_user_writes_name_verbatim(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """
    Provider primitive writes ``user.name`` as-is.

    Truncation and tagging are the base orchestration's responsibility
    (applied in ``_set_credential`` via ``_build_tagged_user_name``);
    the provider receives a User with the name already shaped and
    writes it verbatim. End-to-end truncation behavior is covered
    through ``async_set_usercode`` below.
    """
    mock_access_control.get_users_cached.return_value = []
    mock_access_control.get_all_credentials_cached.return_value = []
    mock_lock_helpers["async_set_user"].return_value = {"user_id": 7}

    user = User(user_id=1, name="lcm:1:alice", active=True)
    await zwave_js_lock.async_set_user(user)

    mock_lock_helpers["async_set_user"].assert_called_once_with(
        zwave_js_lock.node,
        user_id=None,
        user_name="lcm:1:alice",
        active=True,
    )


async def test_async_set_usercode_builds_tagged_name_within_lock_limit(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """End-to-end: ``async_set_usercode`` writes the LCM-tagged user name.

    The base seam builds ``lcm:<slot>:<display>`` via
    ``_build_tagged_user_name``, truncating the display so the full
    tagged name fits the lock's advertised ``max_user_name_length``.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"1": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    pin_type_str = lock_helpers.CREDENTIAL_TYPE_MAP[UserCredentialType.PIN_CODE]
    mock_lock_helpers["async_get_credential_capabilities"].return_value = {
        "supports_user_management": True,
        "max_users": 30,
        "supported_user_types": [],
        # "lcm:1:" prefix is 6 chars; 10-char limit leaves 4 for the display.
        "max_user_name_length": 10,
        "supported_credential_rules": [],
        "supported_credential_types": {
            pin_type_str: {
                "num_slots": 30,
                "min_length": 4,
                "max_length": 8,
                "supports_learn": False,
            }
        },
    }
    mock_access_control.get_users_cached.return_value = []
    mock_access_control.get_all_credentials_cached.return_value = []
    mock_lock_helpers["async_set_user"].return_value = {"user_id": 1}

    # "alexandra" (9 chars) is the display; only "alex" fits after the
    # tag prefix, so the lock-side name becomes "lcm:1:alex".
    await zwave_js_lock.async_set_usercode(1, "1234", name="alexandra")

    mock_lock_helpers["async_set_user"].assert_called_once_with(
        zwave_js_lock.node,
        user_id=None,
        user_name="lcm:1:alex",
        active=True,
    )


# ── Credential type validation ──────────────────────────────────────


# ── Exception wrapping for write primitives ─────────────────────────


async def test_async_set_user_maps_failed_command_to_lock_disconnected(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """A transient Z-Wave failure during set surfaces as LockDisconnected (retry)."""
    mock_access_control.get_user_cached.return_value = None
    mock_lock_helpers["async_set_user"].side_effect = FailedZWaveCommand(
        "cmd", 1, "lock asleep"
    )
    with pytest.raises(LockDisconnected):
        await zwave_js_lock.async_set_user(User(user_id=1, name="alice", active=True))


async def test_async_set_user_maps_ha_error_to_operation_failed(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """A reachable-lock HomeAssistantError during set surfaces as LockOperationFailed."""
    mock_access_control.get_user_cached.return_value = None
    mock_lock_helpers["async_set_user"].side_effect = HomeAssistantError("nope")
    with pytest.raises(LockOperationFailed):
        await zwave_js_lock.async_set_user(User(user_id=1, name="alice", active=True))


async def test_async_delete_user_maps_failed_command_to_lock_disconnected(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """A transient Z-Wave failure during delete-user surfaces as LockDisconnected."""
    mock_lock_helpers["async_delete_user"].side_effect = FailedZWaveCommand(
        "cmd", 1, "lock asleep"
    )
    with pytest.raises(LockDisconnected):
        await zwave_js_lock.async_delete_user(7)


async def test_async_delete_user_maps_ha_error_to_operation_failed(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """A reachable-lock HomeAssistantError during delete-user surfaces as LockOperationFailed."""
    mock_lock_helpers["async_delete_user"].side_effect = HomeAssistantError("nope")
    with pytest.raises(LockOperationFailed):
        await zwave_js_lock.async_delete_user(7)


async def test_async_delete_credential_maps_ha_error_to_operation_failed(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """A reachable-lock HomeAssistantError during delete-credential surfaces as LockOperationFailed."""
    mock_lock_helpers["async_delete_credential"].side_effect = HomeAssistantError(
        "nope"
    )
    with pytest.raises(LockOperationFailed):
        await zwave_js_lock.async_delete_credential(
            CredentialRef(user_id=4, type=CredentialType.PIN, slot=4)
        )


# ── Client-readiness gating ─────────────────────────────────────────


async def test_get_client_state_not_ready_when_client_missing(
    zwave_js_lock: ZWaveJSLock,
) -> None:
    """A loaded entry with no client reports not-ready."""
    runtime_data = MagicMock()
    runtime_data.client = None
    with patch.object(zwave_js_lock.lock_config_entry, "runtime_data", runtime_data):
        ready, reason = zwave_js_lock._get_client_state()

    assert ready is False
    assert "not ready" in reason


async def test_get_client_state_not_ready_when_disconnected(
    zwave_js_lock: ZWaveJSLock,
) -> None:
    """A client that is present but disconnected reports not-ready."""
    runtime_data = MagicMock()
    runtime_data.client = MagicMock(connected=False)
    with patch.object(zwave_js_lock.lock_config_entry, "runtime_data", runtime_data):
        ready, reason = zwave_js_lock._get_client_state()

    assert ready is False
    assert "not connected" in reason


async def test_get_client_state_not_ready_when_driver_missing(
    zwave_js_lock: ZWaveJSLock,
) -> None:
    """A connected client with no driver reports not-ready."""
    runtime_data = MagicMock()
    runtime_data.client = MagicMock(connected=True, driver=None)
    with patch.object(zwave_js_lock.lock_config_entry, "runtime_data", runtime_data):
        ready, reason = zwave_js_lock._get_client_state()

    assert ready is False
    assert "driver not ready" in reason


# ---------------------------------------------------------------------------
# Post-write reconciliation read tests (delete with the report shim; grep _uc_)
# ---------------------------------------------------------------------------


def _pin_credential(slot: int, pin: str) -> Credential:
    """Build a PIN credential for reconciliation-read tests."""
    return Credential(
        type=CredentialType.PIN, slot=slot, state=SlotCredential.known(pin)
    )


async def test_set_credential_success_triggers_reconcile_read(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """A confirmed set reads the slot back so the driver's value DB converges."""
    result = await zwave_js_lock.async_set_credential(
        user_id=1,
        credential=_pin_credential(2, "5678"),
        pin="5678",
        name=None,
        source="sync",
    )

    assert result is WriteResult.CONFIRMED
    mock_access_control.get_credential.assert_awaited_once_with(
        UserCredentialType.PIN_CODE, 2
    )


async def test_set_credential_optimistic_skips_reconcile_read(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """An OPTIMISTIC set skips the read; the seam's confirmation read covers it."""
    mock_lock_helpers["async_set_credential"].side_effect = HomeAssistantError(
        translation_key="credential_rejected_unknown"
    )

    result = await zwave_js_lock.async_set_credential(
        user_id=1,
        credential=_pin_credential(2, "5678"),
        pin="5678",
        name=None,
        source="sync",
    )

    assert result is WriteResult.OPTIMISTIC
    mock_access_control.get_credential.assert_not_called()


async def test_set_credential_rejection_triggers_reconcile_read(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """A definitive rejection reads the slot back before raising."""
    mock_lock_helpers["async_set_credential"].side_effect = HomeAssistantError(
        translation_key="credential_rejected_manufacturer_rules"
    )

    with pytest.raises(CodeRejectedError):
        await zwave_js_lock.async_set_credential(
            user_id=1,
            credential=_pin_credential(4, "2222"),
            pin="2222",
            name=None,
            source="sync",
        )

    mock_access_control.get_credential.assert_awaited_once_with(
        UserCredentialType.PIN_CODE, 4
    )


async def test_set_credential_duplicate_triggers_reconcile_read(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """A duplicate rejection reads the slot back before raising."""
    mock_lock_helpers["async_set_credential"].side_effect = HomeAssistantError(
        translation_key="credential_rejected_duplicate"
    )

    with pytest.raises(DuplicateCodeError):
        await zwave_js_lock.async_set_credential(
            user_id=1,
            credential=_pin_credential(3, "1111"),
            pin="1111",
            name=None,
            source="sync",
        )

    mock_access_control.get_credential.assert_awaited_once_with(
        UserCredentialType.PIN_CODE, 3
    )


async def test_set_credential_disconnect_skips_reconcile_read(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """A transient command failure skips the read; the node is unreachable anyway."""
    mock_lock_helpers["async_set_credential"].side_effect = FailedZWaveCommand(
        "failed", 1, "error"
    )

    with pytest.raises(LockDisconnected):
        await zwave_js_lock.async_set_credential(
            user_id=1,
            credential=_pin_credential(2, "5678"),
            pin="5678",
            name=None,
            source="sync",
        )

    mock_access_control.get_credential.assert_not_called()


async def test_set_credential_skips_reconcile_read_without_user_code_cc(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """A pure User Credential CC lock never needs the reconciliation read."""
    with patch.object(ZWaveJSLock, "_node_advertises_user_code_cc", return_value=False):
        result = await zwave_js_lock.async_set_credential(
            user_id=1,
            credential=_pin_credential(2, "5678"),
            pin="5678",
            name=None,
            source="sync",
        )

    assert result is WriteResult.CONFIRMED
    mock_access_control.get_credential.assert_not_called()


async def test_reconcile_read_failure_does_not_change_write_outcome(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """A failed reconciliation read is swallowed; the write result stands."""
    mock_access_control.get_credential.side_effect = FailedZWaveCommand(
        "failed", 1, "error"
    )

    result = await zwave_js_lock.async_set_credential(
        user_id=1,
        credential=_pin_credential(2, "5678"),
        pin="5678",
        name=None,
        source="sync",
    )

    assert result is WriteResult.CONFIRMED


async def test_delete_credential_success_skips_reconcile_read(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """A successful delete needs no read: the driver clears its cache (#8866)."""
    result = await zwave_js_lock.async_delete_credential(
        CredentialRef(user_id=1, type=CredentialType.PIN, slot=2)
    )

    assert result is True
    mock_access_control.get_credential.assert_not_called()


async def test_delete_credential_failure_triggers_reconcile_read(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """A failed delete reads the slot back before raising."""
    mock_lock_helpers["async_delete_credential"].side_effect = HomeAssistantError(
        "delete failed"
    )

    with pytest.raises(LockOperationFailed):
        await zwave_js_lock.async_delete_credential(
            CredentialRef(user_id=1, type=CredentialType.PIN, slot=5)
        )

    mock_access_control.get_credential.assert_awaited_once_with(
        UserCredentialType.PIN_CODE, 5
    )


async def test_reconcile_read_unexpected_error_does_not_replace_typed_error(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """An unexpected reconcile-read error must not replace the mapped typed error.

    The rejection call sites run inside except clauses; if the read raised
    there, the seam would see the raw exception instead of DuplicateCodeError
    and mishandle the rejection.
    """
    mock_lock_helpers["async_set_credential"].side_effect = HomeAssistantError(
        translation_key="credential_rejected_duplicate"
    )
    mock_access_control.get_credential.side_effect = RuntimeError("boom")

    with pytest.raises(DuplicateCodeError):
        await zwave_js_lock.async_set_credential(
            user_id=1,
            credential=_pin_credential(3, "1111"),
            pin="1111",
            name=None,
            source="sync",
        )
