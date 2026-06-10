"""Test the Z-Wave JS lock provider."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from zwave_js_server.const import NodeStatus
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
)
from custom_components.lock_code_manager.domain.exceptions import (
    CodeRejectedError,
    DuplicateCodeError,
    LockDisconnected,
    LockOperationFailed,
)
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers.zwave_js import ZWaveJSLock

# Properties tests


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
    Test that async_internal_clear_usercode drives the base drop-credential lifecycle.

    With supports_native_users=True, the base class resolves the credential owner
    from async_get_users, then calls async_delete_credential and (when the user has no
    remaining credentials) async_delete_user.
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
    # The user had exactly one credential so it should be deleted too
    mock_lock_helpers["async_delete_user"].assert_called_once_with(
        zwave_js_lock.node, 1
    )


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


# Write primitive tests (Task 2)


async def test_async_set_user_returns_created_when_user_absent(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """
    Test async_set_user reports created=True when no existing user is found.

    When get_user_cached returns None the user does not yet exist on the lock,
    so SetUserResult.created must be True. The helper is called with the correct
    node, user_id, user_name, and active values.
    """
    mock_access_control.get_user_cached.return_value = None
    mock_lock_helpers["async_set_user"].return_value = {"user_id": 5}

    user = User(user_id=5, name="alice", active=True)
    result = await zwave_js_lock.async_set_user(user)

    assert result == SetUserResult(user_id=5, created=True)
    mock_lock_helpers["async_set_user"].assert_called_once_with(
        zwave_js_lock.node,
        user_id=5,
        user_name="alice",
        active=True,
    )


async def test_async_set_user_returns_not_created_when_user_exists(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """
    Test async_set_user reports created=False when the user already exists.

    When get_user_cached returns a UserData object the slot is being updated,
    not created, so SetUserResult.created must be False.
    """
    mock_access_control.get_user_cached.return_value = UserData(
        user_id=3,
        active=True,
        user_type=UserCredentialUserType.GENERAL,
        user_name="bob",
    )
    mock_lock_helpers["async_set_user"].return_value = {"user_id": 3}

    user = User(user_id=3, name="bob", active=True)
    result = await zwave_js_lock.async_set_user(user)

    assert result == SetUserResult(user_id=3, created=False)


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
        pin=credential.readable_pin or "",
        name="alice",
        source="sync",
    )

    assert result is True
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
            pin=credential.readable_pin or "",
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
    Test async_set_credential raises CodeRejectedError for non-duplicate rejections.

    When the lock_helpers helper raises HomeAssistantError with any other
    translation_key (for example "credential_rejected_unknown"), the provider
    must re-raise as CodeRejectedError.
    """
    err = HomeAssistantError(translation_key="credential_rejected_unknown")
    mock_lock_helpers["async_set_credential"].side_effect = err

    credential = Credential(
        type=CredentialType.PIN, slot=4, state=SlotCredential.known("2222")
    )
    with pytest.raises(CodeRejectedError) as exc_info:
        await zwave_js_lock.async_set_credential(
            user_id=1,
            credential=credential,
            pin=credential.readable_pin or "",
            name=None,
            source="sync",
        )

    assert exc_info.value.code_slot == 4
    assert not isinstance(exc_info.value, DuplicateCodeError)


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
            pin=credential.readable_pin or "",
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


async def test_hard_refresh_interval_is_hourly(zwave_js_lock: ZWaveJSLock) -> None:
    """The drift-recovery backstop is scheduled (not disabled)."""
    assert zwave_js_lock.hard_refresh_interval == timedelta(hours=1)


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

    assert changed is True
    mock_lock_helpers["async_set_user"].assert_not_called()
    mock_lock_helpers["async_set_credential"].assert_called_once()


async def test_async_set_user_writes_name_verbatim(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """
    Provider primitive writes user.name as-is.

    Truncation is the base orchestration's responsibility (applied in
    ``_set_credential`` before the call); the provider receives a User
    with the name already shaped to the lock's limit and writes it
    verbatim. End-to-end truncation behavior is covered through
    ``async_set_usercode`` below.
    """
    mock_access_control.get_user_cached.return_value = None
    mock_lock_helpers["async_set_user"].return_value = {"user_id": 1}

    user = User(user_id=1, name="alice", active=True)
    await zwave_js_lock.async_set_user(user)

    mock_lock_helpers["async_set_user"].assert_called_once_with(
        zwave_js_lock.node,
        user_id=1,
        user_name="alice",
        active=True,
    )


async def test_async_set_usercode_truncates_name_to_lock_limit(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """End-to-end: ``async_set_usercode`` truncates per the cached limit."""
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
        "max_user_name_length": 5,
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
    mock_access_control.get_user_cached.return_value = None
    mock_lock_helpers["async_set_user"].return_value = {"user_id": 1}

    # "alexandra" (9 chars) → truncated to "alexa" (5 chars) by the base.
    await zwave_js_lock.async_set_usercode(1, "1234", name="alexandra")

    mock_lock_helpers["async_set_user"].assert_called_once_with(
        zwave_js_lock.node,
        user_id=1,
        user_name="alexa",
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A loaded entry with no client reports not-ready."""
    runtime_data = MagicMock()
    runtime_data.client = None
    monkeypatch.setattr(zwave_js_lock.lock_config_entry, "runtime_data", runtime_data)

    ready, reason = zwave_js_lock._get_client_state()

    assert ready is False
    assert "not ready" in reason


async def test_get_client_state_not_ready_when_disconnected(
    zwave_js_lock: ZWaveJSLock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client that is present but disconnected reports not-ready."""
    runtime_data = MagicMock()
    runtime_data.client = MagicMock(connected=False)
    monkeypatch.setattr(zwave_js_lock.lock_config_entry, "runtime_data", runtime_data)

    ready, reason = zwave_js_lock._get_client_state()

    assert ready is False
    assert "not connected" in reason


async def test_get_client_state_not_ready_when_driver_missing(
    zwave_js_lock: ZWaveJSLock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A connected client with no driver reports not-ready."""
    runtime_data = MagicMock()
    runtime_data.client = MagicMock(connected=True, driver=None)
    monkeypatch.setattr(zwave_js_lock.lock_config_entry, "runtime_data", runtime_data)

    ready, reason = zwave_js_lock._get_client_state()

    assert ready is False
    assert "driver not ready" in reason
