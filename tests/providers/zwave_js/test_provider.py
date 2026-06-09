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
)
from custom_components.lock_code_manager.domain.exceptions import (
    CodeRejectedError,
    DuplicateCodeError,
    LockDisconnected,
)
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers.zwave_js import ZWaveJSLock

from .helpers import _PROVIDER_MODULE

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


async def test_setup_is_idempotent(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    lock_code_manager_config_entry: MockConfigEntry,
) -> None:
    """Test that async_setup clears old listeners before re-registering."""
    await zwave_js_lock.async_setup(lock_code_manager_config_entry)
    assert len(zwave_js_lock._listeners) >= 1
    count_after_first = len(zwave_js_lock._listeners)

    # Call again — should not accumulate listeners
    await zwave_js_lock.async_setup(lock_code_manager_config_entry)
    assert len(zwave_js_lock._listeners) == count_after_first


# CC version detection tests


async def test_usercode_cc_version_v1(zwave_js_lock: ZWaveJSLock) -> None:
    """Test that V1 lock reports correct CC version."""
    assert zwave_js_lock._usercode_cc_version == 1


async def test_usercode_cc_version_v2(zwave_js_lock_v2: ZWaveJSLock) -> None:
    """Test that V2 lock reports correct CC version."""
    assert zwave_js_lock_v2._usercode_cc_version == 2


async def test_usercode_cc_version_missing(
    zwave_js_lock: ZWaveJSLock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test warning when User Code CC is not found on node."""
    # Remove User Code CC from the node's command classes
    node = zwave_js_lock.node
    endpoint = node.endpoints[0]
    original_ccs = endpoint.data["commandClasses"]
    endpoint.data["commandClasses"] = [
        cc for cc in original_ccs if cc["id"] != CommandClass.USER_CODE.value
    ]
    # zwave-js-server-python 0.70+ added @cached_property on endpoint.command_classes
    # so direct endpoint.data mutation needs cache invalidation here too.
    endpoint.__dict__.pop("command_classes", None)
    # Clear cached property so it re-evaluates
    zwave_js_lock.__dict__.pop("_usercode_cc_version", None)

    assert zwave_js_lock._usercode_cc_version == 1
    assert "User Code CC not found" in caplog.text


async def test_node_property(
    zwave_js_lock: ZWaveJSLock,
    lock_schlage_be469: Node,
) -> None:
    """Test node property returns the correct Z-Wave node."""
    node = zwave_js_lock.node
    assert node.node_id == lock_schlage_be469.node_id


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


# Usercode tests


async def test_set_usercode_skips_when_unchanged(
    zwave_js_lock: ZWaveJSLock,
) -> None:
    """Test that set_usercode returns False when code is already set."""
    # Slot 2 already has "1234" in the fixture
    result = await zwave_js_lock.async_set_usercode(2, "1234", "Test User")

    assert result is False


async def test_set_usercode_skips_when_code_matches(
    zwave_js_lock: ZWaveJSLock,
) -> None:
    """Test that set_usercode returns False when cached code matches the target PIN."""
    # Mock the cache to return the same code we're trying to set
    matching_slot = {"code_slot": 2, "usercode": "5678", "in_use": True}

    with patch(
        f"{_PROVIDER_MODULE}.get_usercode",
        return_value=matching_slot,
    ):
        result = await zwave_js_lock.async_set_usercode(2, "5678", "Test User")

        # Should skip the set operation since code already matches
        assert result is False


async def test_set_usercode_proceeds_when_masked(
    zwave_js_lock: ZWaveJSLock,
) -> None:
    """
    Test that set_usercode proceeds when code is masked (all asterisks).

    Some locks (like Yale) return masked PINs (****) instead of actual codes.
    Since we cannot compare masked codes, the set operation should always proceed.
    """
    # Mock the cache to return a masked code
    masked_slot = {"code_slot": 2, "usercode": "****", "in_use": True}

    with patch(
        f"{_PROVIDER_MODULE}.get_usercode",
        return_value=masked_slot,
    ):
        result = await zwave_js_lock.async_set_usercode(2, "5678", "Test User")

        # Should proceed since masked codes cannot be compared
        assert result is True


async def test_set_usercode_proceeds_on_cache_failure(
    zwave_js_lock: ZWaveJSLock,
) -> None:
    """
    Test that set_usercode proceeds when the cache lookup raises.

    A stale or missing cache entry must not block the set operation —
    the bare-except in the cache short-circuit guards against this.
    """
    with patch(
        f"{_PROVIDER_MODULE}.get_usercode",
        side_effect=ValueError("cache miss"),
    ):
        result = await zwave_js_lock.async_set_usercode(4, "5678", "Test User")

        assert result is True


async def test_clear_usercode_skips_when_already_cleared(
    zwave_js_lock: ZWaveJSLock,
) -> None:
    """Test that clear_usercode returns False when slot is already empty."""
    # Slot 3 is already empty in the fixture
    result = await zwave_js_lock.async_clear_usercode(3)

    assert result is False


async def test_clear_usercode_proceeds_on_cache_failure(
    zwave_js_lock: ZWaveJSLock,
) -> None:
    """
    Test that clear_usercode proceeds when the cache lookup raises.

    Mirrors test_set_usercode_proceeds_on_cache_failure for the clear path.
    """
    with patch(
        f"{_PROVIDER_MODULE}.get_usercode",
        side_effect=ValueError("cache miss"),
    ):
        result = await zwave_js_lock.async_clear_usercode(2)

        assert result is True


async def test_set_usercode_optimistic_update(
    zwave_js_lock: ZWaveJSLock,
    mock_coordinator,
) -> None:
    """
    Test that set_usercode performs optimistic coordinator update.

    When a set operation succeeds, the coordinator should be updated immediately
    with the new value. This prevents sync loops where the binary sensor reads
    stale cached data and triggers repeated sync attempts.

    The Z-Wave command is acknowledged by the lock (via Supervision CC), but
    the JS value cache updates asynchronously via push notifications. Without
    the optimistic update, there's a race condition where coordinator refresh
    reads stale cache data.
    """
    # Set up a mock coordinator with stale data (simulating the race condition)
    zwave_js_lock.coordinator = mock_coordinator(
        {4: SlotCredential.empty()}
    )  # Slot appears empty in stale cache

    result = await zwave_js_lock.async_set_usercode(4, "5678", "Test User")

    assert result is True
    # Verify optimistic update was called with new PIN
    zwave_js_lock.coordinator.push_update.assert_called_once_with(
        {4: SlotCredential.known("5678")}
    )


async def test_set_usercode_optimistic_update_prevents_stale_read(
    zwave_js_lock: ZWaveJSLock,
    mock_coordinator,
) -> None:
    """Test that optimistic update prevents sync loops from stale cache reads."""
    # Simulate stale cache: coordinator thinks slot is empty
    zwave_js_lock.coordinator = mock_coordinator({4: SlotCredential.empty()})

    await zwave_js_lock.async_set_usercode(4, "9999")

    # The optimistic update should have been called
    zwave_js_lock.coordinator.push_update.assert_called_once_with(
        {4: SlotCredential.known("9999")}
    )

    # Simulate what push_update does - update coordinator data
    zwave_js_lock.coordinator.data[4] = SlotCredential.known("9999")
    assert zwave_js_lock.coordinator.data[4] == SlotCredential.known("9999")


async def test_clear_usercode_optimistic_update(
    zwave_js_lock: ZWaveJSLock,
    mock_coordinator,
) -> None:
    """
    Test that clear_usercode performs optimistic coordinator update.

    When a clear operation succeeds, the coordinator should be updated immediately
    with SlotCredential.empty(). This prevents sync loops where the binary sensor reads
    stale cached data showing the old PIN and triggers repeated clear attempts.
    """
    # Set up a mock coordinator with stale data (still shows old PIN)
    zwave_js_lock.coordinator = mock_coordinator(
        {2: SlotCredential.known("1234")}
    )  # Stale: slot still shows PIN

    result = await zwave_js_lock.async_clear_usercode(2)

    assert result is True
    # Verify optimistic update was called with SlotCredential.empty()
    zwave_js_lock.coordinator.push_update.assert_called_once_with(
        {2: SlotCredential.empty()}
    )


# V1 cache poll tests


async def test_v1_set_usercode_polls_slot(
    zwave_js_lock: ZWaveJSLock,
    lock_schlage_be469: Node,
    mock_get_usercode_from_node,
    mock_coordinator,
) -> None:
    """
    Test that V1 set_usercode polls the slot from the device after set.

    V1 locks don't reliably update the Z-Wave JS value cache after a set
    operation. Polling the slot forces the cache to update before the
    coordinator reads it, preventing sync loops.
    """
    zwave_js_lock.coordinator = mock_coordinator({4: SlotCredential.empty()})

    await zwave_js_lock.async_set_usercode(4, "5678", "Test User")

    mock_get_usercode_from_node.assert_called_once_with(lock_schlage_be469, 4)


async def test_v1_clear_usercode_polls_slot(
    zwave_js_lock: ZWaveJSLock,
    lock_schlage_be469: Node,
    mock_get_usercode_from_node,
    mock_coordinator,
) -> None:
    """Test that V1 clear_usercode polls the slot from the device after clear."""
    zwave_js_lock.coordinator = mock_coordinator({2: SlotCredential.known("1234")})

    await zwave_js_lock.async_clear_usercode(2)

    mock_get_usercode_from_node.assert_called_once_with(lock_schlage_be469, 2)


async def test_v2_set_usercode_does_not_poll_slot(
    zwave_js_lock_v2: ZWaveJSLock,
    mock_get_usercode_from_node,
    mock_coordinator,
) -> None:
    """Test that V2 set_usercode does NOT poll the slot (cache updates reliably)."""
    zwave_js_lock_v2.coordinator = mock_coordinator({4: SlotCredential.empty()})

    await zwave_js_lock_v2.async_set_usercode(4, "5678", "Test User")

    mock_get_usercode_from_node.assert_not_called()


async def test_v1_set_usercode_poll_failure_raises_lock_disconnected(
    zwave_js_lock: ZWaveJSLock,
    mock_get_usercode_from_node,
    mock_coordinator,
) -> None:
    """
    Test that a V1 set poll failure raises LockDisconnected.

    When get_usercode_from_node raises after a V1 set operation, the error
    should be wrapped as LockDisconnected so it routes into the retry path
    instead of the generic exception handler that would suspend the lock.
    """
    zwave_js_lock.coordinator = mock_coordinator({4: SlotCredential.empty()})

    mock_get_usercode_from_node.side_effect = FailedZWaveCommand(
        "msg_id", 202, "Node presumed dead"
    )

    with pytest.raises(LockDisconnected, match="Post-set verification poll failed"):
        await zwave_js_lock.async_set_usercode(4, "5678", "Test User")


async def test_v1_clear_usercode_poll_failure_raises_lock_disconnected(
    zwave_js_lock: ZWaveJSLock,
    mock_get_usercode_from_node,
    mock_coordinator,
) -> None:
    """
    Test that a V1 clear poll failure raises LockDisconnected.

    When get_usercode_from_node raises after a V1 clear operation, the error
    should be wrapped as LockDisconnected so it routes into the retry path
    instead of the generic exception handler that would suspend the lock.
    """
    zwave_js_lock.coordinator = mock_coordinator({2: SlotCredential.known("1234")})

    mock_get_usercode_from_node.side_effect = FailedZWaveCommand(
        "msg_id", 202, "Node presumed dead"
    )

    with pytest.raises(LockDisconnected, match="Post-clear verification poll failed"):
        await zwave_js_lock.async_clear_usercode(2)


async def test_v1_set_usercode_poll_non_zwave_error_propagates(
    zwave_js_lock: ZWaveJSLock,
    mock_get_usercode_from_node,
    mock_coordinator,
) -> None:
    """
    Non-Z-Wave errors from V1 post-set poll propagate uncaught.

    Only FailedZWaveCommand is wrapped as LockDisconnected. Other
    exceptions (programming errors) should propagate so the sync
    manager's generic handler can suspend the lock and surface the bug.
    """
    zwave_js_lock.coordinator = mock_coordinator({4: SlotCredential.empty()})

    mock_get_usercode_from_node.side_effect = RuntimeError("unexpected bug")

    with pytest.raises(RuntimeError, match="unexpected bug"):
        await zwave_js_lock.async_set_usercode(4, "5678", "Test User")


async def test_v1_clear_usercode_poll_non_zwave_error_propagates(
    zwave_js_lock: ZWaveJSLock,
    mock_get_usercode_from_node,
    mock_coordinator,
) -> None:
    """Non-Z-Wave errors from V1 post-clear poll propagate uncaught."""
    zwave_js_lock.coordinator = mock_coordinator({2: SlotCredential.known("1234")})

    mock_get_usercode_from_node.side_effect = RuntimeError("unexpected bug")

    with pytest.raises(RuntimeError, match="unexpected bug"):
        await zwave_js_lock.async_clear_usercode(2)


async def test_set_usercode_no_coordinator(
    zwave_js_lock: ZWaveJSLock,
) -> None:
    """
    Test that set_usercode handles missing coordinator gracefully.

    The coordinator check is defensive - in normal operation it always exists
    after setup. This test verifies the guard clause works.
    """
    # Remove coordinator to test defensive check (explicit for clarity)
    zwave_js_lock.coordinator = None

    # Should not raise even without coordinator
    result = await zwave_js_lock.async_set_usercode(4, "5678")
    assert result is True


async def test_clear_usercode_no_coordinator(
    zwave_js_lock: ZWaveJSLock,
) -> None:
    """Test that clear_usercode handles missing coordinator gracefully."""
    # Remove coordinator to test defensive check (explicit for clarity)
    zwave_js_lock.coordinator = None

    # Should not raise even without coordinator
    result = await zwave_js_lock.async_clear_usercode(2)
    assert result is True


# Setup/unload tests


async def test_setup_registers_event_listener(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
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


async def test_hard_refresh_calls_refresh_cc_values(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    lock_schlage_be469: Node,
) -> None:
    """Test that hard refresh calls the node's refresh method."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"1": {}, "2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)

    with patch.object(
        lock_schlage_be469,
        "async_refresh_cc_values",
        new_callable=AsyncMock,
    ) as mock_refresh:
        codes = await zwave_js_lock.async_hard_refresh_codes()

        mock_refresh.assert_called_once_with(CommandClass.USER_CODE)
        assert isinstance(codes, dict)


# Masked usercode tests


async def test_get_usercodes_masked_pin_unmanaged_slot_returns_masked_value(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
) -> None:
    """
    Test mixed slots: managed with real code vs unmanaged with masked code.

    This test verifies behavior when the lock cache contains:
    - Slot 1: Managed by LCM, has real code "9999" -> should be returned
    - Slot 5: NOT managed by LCM, has masked code "****" -> returns UNREADABLE_CODE

    Unmanaged slots with masked PINs return SlotCredential.unreadable() so sync
    logic knows a PIN exists on the lock, even if we can't read the actual value.
    """
    # Configure LCM to only manage slot 1 (not slot 5)
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"1": {}},  # Only slot 1 is managed
        },
    )
    lcm_entry.add_to_hass(hass)

    # Mock the cache to include a slot with a masked PIN that isn't managed by LCM
    masked_slots = [
        {"code_slot": 1, "usercode": "9999", "in_use": True},  # Managed by LCM
        {"code_slot": 5, "usercode": "****", "in_use": True},  # NOT managed, masked
    ]

    with (
        patch.object(
            zwave_js_lock, "_get_usercodes_from_cache", return_value=masked_slots
        ),
        # Slot 5 not in Z-Wave node data, so code_slot_in_use returns None
        patch.object(
            zwave_js_lock,
            "code_slot_in_use",
            side_effect=lambda s: True if s == 1 else None,
        ),
    ):
        codes = await zwave_js_lock.async_get_usercodes()

        # Slot 1 should have its code
        assert codes[1] == SlotCredential.known("9999")
        # Slot 5 returns an unreadable credential (masked code, so sync logic knows a PIN exists)
        assert codes[5] is SlotCredential.unreadable()


async def test_get_usercodes_masked_pin_returns_unknown(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
) -> None:
    """
    Test that masked PINs return SlotCredential.unreadable().

    When the lock returns masked codes (all asterisks), the provider returns
    SlotCredential.unreadable() so consumers know a code exists but the value is hidden.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)

    # Mock the cache to have a masked PIN on a managed slot
    masked_slots = [
        {"code_slot": 2, "usercode": "****", "in_use": True},
    ]

    with patch.object(
        zwave_js_lock, "_get_usercodes_from_cache", return_value=masked_slots
    ):
        codes = await zwave_js_lock.async_get_usercodes()

        # Masked PIN should be returned as SlotCredential.unreadable()
        assert codes[2] is SlotCredential.unreadable()


async def test_get_usercodes_empty_usercode_in_use_skipped(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
) -> None:
    """Test that in_use=True with empty usercode is skipped (partially populated cache)."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [zwave_js_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}, "3": {}},
        },
    )
    lcm_entry.add_to_hass(hass)

    # Slot 2: in_use=True but empty usercode (cache not populated yet)
    # Slot 3: normal code
    slots = [
        {"code_slot": 2, "usercode": "", "in_use": True},
        {"code_slot": 3, "usercode": "5678", "in_use": True},
    ]

    with patch.object(zwave_js_lock, "_get_usercodes_from_cache", return_value=slots):
        codes = await zwave_js_lock.async_get_usercodes()

        # Slot 2 should be skipped (not in result)
        assert 2 not in codes
        # Slot 3 should have its code
        assert codes[3] == SlotCredential.known("5678")


# code_slot_in_use tests


@pytest.mark.parametrize(
    ("mock_config", "expected"),
    [
        ({"return_value": {"in_use": True, "usercode": "1234"}}, True),
        ({"return_value": {"in_use": False, "usercode": ""}}, False),
        ({"side_effect": KeyError("slot not found")}, None),
        ({"side_effect": ValueError("invalid slot")}, None),
    ],
)
async def test_code_slot_in_use(
    zwave_js_lock: ZWaveJSLock,
    mock_config: dict,
    expected: bool | None,
) -> None:
    """Test code_slot_in_use for various return values and exceptions."""
    with patch(
        f"{_PROVIDER_MODULE}.get_usercode",
        **mock_config,
    ):
        assert zwave_js_lock.code_slot_in_use(1) is expected


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


async def test_async_get_users_maps_users_and_pin_credentials(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """
    Test async_get_users returns users with PIN credentials correctly projected.

    Given two users and three credentials (two PINs, one non-PIN), the result
    maps each user to its PIN credentials only. Readable data becomes
    SlotCredential.known; absent data becomes SlotCredential.unreadable.
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
        # Non-PIN credential — must be filtered out.
        CredentialData(
            user_id=1,
            type=UserCredentialType.RFID_CODE,
            slot=1,
            data="AABB",
        ),
    ]

    users = await zwave_js_lock.async_get_users()

    assert len(users) == 3
    user3 = next(u for u in users if u.user_id == 3)
    assert user3.credentials == []

    user1 = next(u for u in users if u.user_id == 1)
    assert user1.name == "alice"
    assert len(user1.credentials) == 1
    assert user1.credentials[0] == Credential(
        type=CredentialType.PIN,
        slot=1,
        state=SlotCredential.known("1234"),
    )

    user2 = next(u for u in users if u.user_id == 2)
    assert len(user2.credentials) == 1
    assert user2.credentials[0] == Credential(
        type=CredentialType.PIN,
        slot=2,
        state=SlotCredential.unreadable(),
    )


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
        user_id=1, credential=credential, name="alice", source="sync"
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
            user_id=1, credential=credential, name=None, source="sync"
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
            user_id=1, credential=credential, name=None, source="sync"
        )

    assert exc_info.value.code_slot == 4
    assert not isinstance(exc_info.value, DuplicateCodeError)


async def test_async_set_credential_rejects_unreadable_credential(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """An unreadable credential (no Personal Identification Number) is rejected cleanly."""
    credential = Credential(
        type=CredentialType.PIN, slot=4, state=SlotCredential.unreadable()
    )
    with pytest.raises(CodeRejectedError) as exc_info:
        await zwave_js_lock.async_set_credential(
            user_id=1, credential=credential, name=None, source="sync"
        )

    assert exc_info.value.code_slot == 4
    mock_lock_helpers["async_set_credential"].assert_not_called()


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
