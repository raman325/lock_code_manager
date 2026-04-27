"""Test the ZHA lock provider."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from zigpy.zcl.clusters.closures import DoorLock

from homeassistant.components.zha.const import DOMAIN as ZHA_DOMAIN
from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.exceptions import LockDisconnected
from custom_components.lock_code_manager.models import SlotCode
from custom_components.lock_code_manager.providers.zha import (
    ZHALock,
)

# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


async def test_domain(zha_lock: ZHALock) -> None:
    """Test domain property returns zha."""
    assert zha_lock.domain == ZHA_DOMAIN


async def test_supports_push(zha_lock: ZHALock) -> None:
    """Test that ZHA locks support push updates."""
    assert zha_lock.supports_push is True


async def test_connection_check_interval(zha_lock: ZHALock) -> None:
    """Test connection check interval is 30 seconds."""
    assert zha_lock.connection_check_interval == timedelta(seconds=30)


async def test_hard_refresh_with_programming_events(zha_lock: ZHALock) -> None:
    """Test hard refresh interval is None when programming events supported."""
    zha_lock._supports_programming_events = True
    assert zha_lock.hard_refresh_interval is None


async def test_hard_refresh_without_programming_events(zha_lock: ZHALock) -> None:
    """Test hard refresh interval is 1 hour when programming events not supported."""
    zha_lock._supports_programming_events = False
    assert zha_lock.hard_refresh_interval == timedelta(hours=1)


# ---------------------------------------------------------------------------
# Connection tests
# ---------------------------------------------------------------------------


async def test_is_integration_connected(hass: HomeAssistant, zha_lock: ZHALock) -> None:
    """Test connection is up when device is available."""
    assert await zha_lock.async_is_integration_connected() is True


# ---------------------------------------------------------------------------
# Cluster access tests
# ---------------------------------------------------------------------------


async def test_get_door_lock_cluster(hass: HomeAssistant, zha_lock: ZHALock) -> None:
    """Test getting the DoorLock cluster."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    assert cluster.cluster_id == DoorLock.cluster_id


async def test_get_door_lock_cluster_caches_result(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test that the cluster is cached after first access."""
    cluster1 = zha_lock._get_door_lock_cluster()
    cluster2 = zha_lock._get_door_lock_cluster()
    assert cluster1 is cluster2


# ---------------------------------------------------------------------------
# Usercode tests
# ---------------------------------------------------------------------------


async def test_get_usercodes(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test reading usercodes from the lock."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None

    async def mock_get_pin_code(slot_num):
        if slot_num == 1:
            return type(
                "Response",
                (),
                {"user_status": DoorLock.UserStatus.Enabled, "code": "1234"},
            )()
        return type(
            "Response",
            (),
            {"user_status": DoorLock.UserStatus.Available, "code": ""},
        )()

    cluster.get_pin_code = AsyncMock(side_effect=mock_get_pin_code)

    codes = await zha_lock.async_get_usercodes()

    assert codes[1] == "1234"
    assert codes[2] is SlotCode.EMPTY


async def test_set_usercode(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test set_usercode calls the cluster correctly."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.set_pin_code = AsyncMock(return_value=type("Response", (), {"status": 0})())

    result = await zha_lock.async_set_usercode(3, "5678", "Test User")

    assert result is True
    cluster.set_pin_code.assert_called_once_with(
        3,
        DoorLock.UserStatus.Enabled,
        DoorLock.UserType.Unrestricted,
        "5678",
    )


async def test_set_usercode_failure(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test set_usercode raises LockDisconnected on failure."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.set_pin_code = AsyncMock(return_value=type("Response", (), {"status": 1})())

    with pytest.raises(LockDisconnected, match="set_pin_code failed"):
        await zha_lock.async_set_usercode(3, "5678")


async def test_clear_usercode(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test clear_usercode calls the cluster correctly."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.clear_pin_code = AsyncMock(
        return_value=type("Response", (), {"status": 0})()
    )

    result = await zha_lock.async_clear_usercode(3)

    assert result is True
    cluster.clear_pin_code.assert_called_once_with(3)


async def test_clear_usercode_failure(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test clear_usercode raises LockDisconnected on failure."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.clear_pin_code = AsyncMock(
        return_value=type("Response", (), {"status": 1})()
    )

    with pytest.raises(LockDisconnected, match="clear_pin_code failed"):
        await zha_lock.async_clear_usercode(3)


# ---------------------------------------------------------------------------
# Push update tests
# ---------------------------------------------------------------------------


async def test_subscribe_push_updates(hass: HomeAssistant, zha_lock: ZHALock) -> None:
    """Test subscribing to push updates."""
    zha_lock.setup_push_subscription()
    assert zha_lock._cluster_listener_unsub is not None

    zha_lock.teardown_push_subscription()
    assert zha_lock._cluster_listener_unsub is None


async def test_subscribe_is_idempotent(hass: HomeAssistant, zha_lock: ZHALock) -> None:
    """Test that calling subscribe multiple times is safe."""
    zha_lock.setup_push_subscription()
    first_unsub = zha_lock._cluster_listener_unsub

    zha_lock.setup_push_subscription()
    assert zha_lock._cluster_listener_unsub is first_unsub

    zha_lock.teardown_push_subscription()


# ---------------------------------------------------------------------------
# Programming event support detection
# ---------------------------------------------------------------------------


async def test_programming_event_support_with_mask(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test detecting programming event support via mask attributes."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None

    mask_attr = DoorLock.AttributeDefs.keypad_programming_event_mask
    cluster.read_attributes = AsyncMock(return_value=({mask_attr.id: 0x0001}, {}))

    result = await zha_lock._async_check_programming_event_support()
    assert result is True


async def test_programming_event_support_without_mask(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test programming events not supported when no mask attributes."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None

    cluster.read_attributes = AsyncMock(return_value=({}, {}))

    result = await zha_lock._async_check_programming_event_support()
    assert result is False


# ---------------------------------------------------------------------------
# Connection failure paths
# ---------------------------------------------------------------------------


async def test_is_integration_connected_no_gateway(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test connection returns False when gateway is unavailable."""
    with patch.object(zha_lock, "_get_gateway", return_value=None):
        assert await zha_lock.async_is_integration_connected() is False


async def test_is_integration_connected_no_entity_ref(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test connection returns False when entity reference not found."""
    gateway = MagicMock()
    gateway.get_entity_reference.return_value = None
    with patch.object(zha_lock, "_get_gateway", return_value=gateway):
        assert await zha_lock.async_is_integration_connected() is False


async def test_is_integration_connected_no_device_proxy(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test connection returns False when device proxy is missing."""
    entity_ref = MagicMock()
    entity_ref.entity_data.device_proxy = None
    gateway = MagicMock()
    gateway.get_entity_reference.return_value = entity_ref
    with patch.object(zha_lock, "_get_gateway", return_value=gateway):
        assert await zha_lock.async_is_integration_connected() is False


# ---------------------------------------------------------------------------
# Cluster access failure paths
# ---------------------------------------------------------------------------


async def test_get_door_lock_cluster_no_gateway(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test cluster lookup returns None when gateway unavailable."""
    zha_lock._door_lock_cluster = None
    with patch.object(zha_lock, "_get_gateway", return_value=None):
        assert zha_lock._get_door_lock_cluster() is None


async def test_get_door_lock_cluster_no_entity_ref(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test cluster lookup returns None when entity ref not found."""
    zha_lock._door_lock_cluster = None
    gateway = MagicMock()
    gateway.get_entity_reference.return_value = None
    with patch.object(zha_lock, "_get_gateway", return_value=gateway):
        assert zha_lock._get_door_lock_cluster() is None


async def test_get_connected_cluster_raises_when_no_cluster(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test _get_connected_cluster raises LockDisconnected without cluster."""
    with patch.object(zha_lock, "_get_door_lock_cluster", return_value=None):
        with pytest.raises(LockDisconnected, match="cluster not available"):
            await zha_lock._get_connected_cluster()


async def test_get_connected_cluster_raises_when_disconnected(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test _get_connected_cluster raises LockDisconnected when not connected."""
    with patch.object(zha_lock, "async_is_integration_connected", return_value=False):
        with pytest.raises(LockDisconnected, match="not connected"):
            await zha_lock._get_connected_cluster()


async def test_get_gateway_handles_exceptions(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test _get_gateway returns None on KeyError/ValueError."""
    with patch(
        "custom_components.lock_code_manager.providers.zha._get_zha_gateway_proxy",
        side_effect=KeyError("not loaded"),
    ):
        assert zha_lock._get_gateway() is None


# ---------------------------------------------------------------------------
# Parse PIN response edge cases
# ---------------------------------------------------------------------------


def test_parse_pin_response_bytes() -> None:
    """Test parsing PIN response with bytes code."""
    result = type(
        "Response",
        (),
        {"user_status": DoorLock.UserStatus.Enabled, "code": b"1234"},
    )()
    status, pin = ZHALock._parse_pin_response(result)
    assert status == DoorLock.UserStatus.Enabled
    assert pin == "1234"


def test_parse_pin_response_list_format() -> None:
    """Test parsing PIN response in list format."""
    result = [0, DoorLock.UserStatus.Enabled, 0, "5678"]
    status, pin = ZHALock._parse_pin_response(result)
    assert status == DoorLock.UserStatus.Enabled
    assert pin == "5678"


def test_parse_pin_response_list_bytes() -> None:
    """Test parsing list-format response with bytes PIN."""
    result = [0, DoorLock.UserStatus.Enabled, 0, b"5678"]
    status, pin = ZHALock._parse_pin_response(result)
    assert status == DoorLock.UserStatus.Enabled
    assert pin == "5678"


def test_parse_pin_response_unknown_format() -> None:
    """Test parsing unknown response format returns Available/empty."""
    status, pin = ZHALock._parse_pin_response("unexpected")
    assert status == DoorLock.UserStatus.Available
    assert pin == ""


# ---------------------------------------------------------------------------
# Cluster command / event handling
# ---------------------------------------------------------------------------


async def test_cluster_command_programming_event(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test cluster_command dispatches programming events."""
    zha_lock.coordinator = MagicMock()
    zha_lock.coordinator.async_request_refresh = AsyncMock()

    args = type("Args", (), {"program_event_code": 1, "user_id": 2})()
    cmd_id = DoorLock.ClientCommandDefs.programming_event_notification.id
    zha_lock.cluster_command(0, cmd_id, args)
    await hass.async_block_till_done()

    zha_lock.coordinator.async_request_refresh.assert_called_once()


async def test_cluster_command_operation_event(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test cluster_command dispatches operation events and fires code slot event."""
    with patch.object(zha_lock, "async_fire_code_slot_event") as mock_fire:
        args = type(
            "Args",
            (),
            {
                "operation_event_source": DoorLock.OperationEventSource.Keypad,
                "operation_event_code": DoorLock.OperationEvent.Unlock,
                "user_id": 3,
            },
        )()
        cmd_id = DoorLock.ClientCommandDefs.operation_event_notification.id
        zha_lock.cluster_command(0, cmd_id, args)

        mock_fire.assert_called_once_with(
            code_slot=3,
            to_locked=False,
            action_text="Keypad unlock operation",
            source_data={
                "source": DoorLock.OperationEventSource.Keypad,
                "event_code": DoorLock.OperationEvent.Unlock,
                "user_id": 3,
            },
        )


async def test_operation_event_zero_user_id(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test operation event with user_id=0 passes code_slot=None."""
    with patch.object(zha_lock, "async_fire_code_slot_event") as mock_fire:
        args = type(
            "Args",
            (),
            {
                "operation_event_source": DoorLock.OperationEventSource.Manual,
                "operation_event_code": DoorLock.OperationEvent.Lock,
                "user_id": 0,
            },
        )()
        cmd_id = DoorLock.ClientCommandDefs.operation_event_notification.id
        zha_lock.cluster_command(0, cmd_id, args)

        mock_fire.assert_called_once()
        assert mock_fire.call_args.kwargs["code_slot"] is None
        assert mock_fire.call_args.kwargs["to_locked"] is True


async def test_cluster_command_unknown_ignored(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test unknown command IDs are silently ignored."""
    zha_lock.cluster_command(0, 999, None)


async def test_programming_event_unparseable(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test programming event with unparseable args is handled gracefully."""
    zha_lock._handle_programming_event(None)


async def test_operation_event_unparseable(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test operation event with unparseable args is handled gracefully."""
    zha_lock._handle_operation_event(None)


# ---------------------------------------------------------------------------
# Push subscription edge cases
# ---------------------------------------------------------------------------


async def test_setup_push_no_cluster_raises(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test setup_push_subscription raises when cluster unavailable."""
    with patch.object(zha_lock, "_get_door_lock_cluster", return_value=None):
        with pytest.raises(LockDisconnected, match="not available"):
            zha_lock.setup_push_subscription()


async def test_teardown_push_when_not_subscribed(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test teardown when not subscribed is a no-op."""
    assert zha_lock._cluster_listener_unsub is None
    zha_lock.teardown_push_subscription()
    assert zha_lock._cluster_listener_unsub is None


# ---------------------------------------------------------------------------
# Detect programming support
# ---------------------------------------------------------------------------


async def test_async_setup_detects_programming_support(
    hass: HomeAssistant, zha_lock: ZHALock, simple_lcm_config_entry: MockConfigEntry
) -> None:
    """Test async_setup detects programming event support before coordinator."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.read_attributes = AsyncMock(return_value=({}, {}))

    await zha_lock.async_setup(simple_lcm_config_entry)

    assert zha_lock._supports_programming_events is False
    assert zha_lock.hard_refresh_interval == timedelta(hours=1)


async def test_check_programming_support_no_cluster(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test programming support check returns False without cluster."""
    with patch.object(zha_lock, "_get_door_lock_cluster", return_value=None):
        assert await zha_lock._async_check_programming_event_support() is False


async def test_check_programming_support_read_failure(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test programming support check handles read_attributes failure."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None

    cluster.read_attributes = AsyncMock(side_effect=RuntimeError("read failed"))

    result = await zha_lock._async_check_programming_event_support()
    assert result is False


# ---------------------------------------------------------------------------
# get_usercodes error handling
# ---------------------------------------------------------------------------


async def test_get_usercodes_slot_read_failure(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test get_usercodes returns UNREADABLE_CODE for slots that fail to read."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.get_pin_code = AsyncMock(side_effect=RuntimeError("zigpy timeout"))

    codes = await zha_lock.async_get_usercodes()

    assert codes[1] is SlotCode.UNREADABLE_CODE
    assert codes[2] is SlotCode.UNREADABLE_CODE


async def test_get_usercodes_no_managed_slots(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test get_usercodes returns empty dict with no managed slots."""
    codes = await zha_lock.async_get_usercodes()
    assert codes == {}


async def test_set_usercode_generic_exception(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test set_usercode wraps generic exceptions as LockDisconnected."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.set_pin_code = AsyncMock(side_effect=RuntimeError("zigpy error"))

    with pytest.raises(LockDisconnected, match="Failed to set PIN"):
        await zha_lock.async_set_usercode(1, "1234")


async def test_clear_usercode_generic_exception(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test clear_usercode wraps generic exceptions as LockDisconnected."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.clear_pin_code = AsyncMock(side_effect=RuntimeError("zigpy error"))

    with pytest.raises(LockDisconnected, match="Failed to clear PIN"):
        await zha_lock.async_clear_usercode(1)
