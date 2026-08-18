"""Test the ZHA lock provider."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from zigpy.zcl.clusters.closures import DoorLock

from homeassistant.components.zha.const import DOMAIN as ZHA_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.lock_code_manager.domain.credentials import (
    CredentialRef,
    CredentialType,
    WriteResult,
    credential_from_slot,
)
from custom_components.lock_code_manager.domain.exceptions import (
    CodeRejectedError,
    LockDisconnected,
)
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers.zha import (
    ZHALock,
)
from tests.providers.helpers import ProviderNativeTransportContractTests


@pytest.mark.skip(
    reason="ZHA's read path deliberately degrades per-slot: a native zigpy "
    "error (DeliveryError / TimeoutError) from cluster.get_pin_code is caught "
    "and the slot marked unreadable, so the coordinator does not treat a "
    "transient error as confirmed-empty. There is no native non-"
    "HomeAssistantError exception that maps to LockDisconnected at a read seam "
    "(the cluster gate raises LockDisconnected only when the cluster is "
    "absent, not from a native exception), so the issue #1257 contract does "
    "not apply."
)
class TestNativeTransportContract(ProviderNativeTransportContractTests):
    """Documents that the native-transport contract does not apply to ZHA reads."""


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


async def test_describe_link_health_reports_last_frame(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Signal metrics and staleness describe the most recent frame received."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    device = cluster.endpoint.device
    device.lqi = 12
    device.rssi = -89
    device.last_seen = dt_util.utcnow() - timedelta(hours=3)

    description = zha_lock.describe_link_health()

    assert description is not None
    assert "signal quality 12 of 255" in description
    assert "-89 dBm" in description
    assert "last heard from 3 hours ago" in description


async def test_describe_link_health_reports_staleness_without_signal(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """
    A radio that reports no signal metrics still says when the lock was last
    heard from -- the part that carries the diagnosis.
    """
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    device = cluster.endpoint.device
    device.lqi = None
    device.rssi = None
    device.last_seen = dt_util.utcnow() - timedelta(minutes=45)

    description = zha_lock.describe_link_health()

    assert description is not None
    assert "45 minutes ago" in description
    assert "signal quality" not in description
    assert "dBm" not in description


async def test_describe_link_health_none_without_any_metrics(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Nothing measured means nothing truthful to report."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    device = cluster.endpoint.device
    device.lqi = None
    device.rssi = None
    device.last_seen = None

    assert zha_lock.describe_link_health() is None


async def test_describe_link_health_none_when_cluster_unresolvable(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """An unresolvable device must not abort the suspension it is describing."""
    with patch.object(zha_lock, "_get_door_lock_cluster", return_value=None):
        assert zha_lock.describe_link_health() is None


# ---------------------------------------------------------------------------
# Credential primitive tests
# ---------------------------------------------------------------------------


async def test_get_users(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test async_get_users reads from the cluster and returns a user per slot."""
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

    users = await zha_lock.async_get_users()

    by_slot = {u.user_id: u for u in users}
    assert by_slot[1].pin_credentials[0].state == SlotCredential.known("1234")
    assert by_slot[2].pin_credentials[0].state is SlotCredential.empty()


async def test_get_usercodes_via_base_projection(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Base async_get_usercodes projection surfaces all managed slots."""
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

    assert codes[1] == SlotCredential.known("1234")
    assert codes[2] is SlotCredential.empty()


async def test_set_credential(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test async_set_credential calls the cluster and pushes optimistic update."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.set_pin_code = AsyncMock(return_value=type("Response", (), {"status": 0})())
    zha_lock.coordinator = MagicMock()

    credential = credential_from_slot(3, SlotCredential.known("5678"))
    result = await zha_lock.async_set_credential(
        3, credential, "5678", name="Test User", source="direct"
    )

    assert result is WriteResult.CONFIRMED
    cluster.set_pin_code.assert_called_once_with(
        3,
        DoorLock.UserStatus.Enabled,
        DoorLock.UserType.Unrestricted,
        "5678",
    )
    zha_lock.coordinator.push_update.assert_called_once_with(
        {3: SlotCredential.known("5678")}
    )


async def test_set_credential_failure(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test async_set_credential raises CodeRejectedError on non-zero status."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.set_pin_code = AsyncMock(return_value=type("Response", (), {"status": 1})())

    credential = credential_from_slot(3, SlotCredential.known("5678"))
    with pytest.raises(CodeRejectedError, match="set_pin_code rejected"):
        await zha_lock.async_set_credential(
            3, credential, "5678", name=None, source="direct"
        )


async def test_delete_credential(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test async_delete_credential calls the cluster and pushes optimistic update."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.clear_pin_code = AsyncMock(
        return_value=type("Response", (), {"status": 0})()
    )
    zha_lock.coordinator = MagicMock()

    ref = CredentialRef(user_id=3, type=CredentialType.PIN, slot=3)
    result = await zha_lock.async_delete_credential(ref)

    assert result is True
    cluster.clear_pin_code.assert_called_once_with(3)
    zha_lock.coordinator.push_update.assert_called_once_with(
        {3: SlotCredential.empty()}
    )


async def test_delete_credential_failure(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test async_delete_credential raises CodeRejectedError on non-zero status."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.clear_pin_code = AsyncMock(
        return_value=type("Response", (), {"status": 1})()
    )

    ref = CredentialRef(user_id=3, type=CredentialType.PIN, slot=3)
    with pytest.raises(CodeRejectedError, match="clear_pin_code rejected"):
        await zha_lock.async_delete_credential(ref)


# ---------------------------------------------------------------------------
# Push update tests
# ---------------------------------------------------------------------------


async def test_subscribe_push_updates(hass: HomeAssistant, zha_lock: ZHALock) -> None:
    """Test subscribing to push updates."""
    zha_lock.setup_push_subscription()
    assert zha_lock._push_unsubs

    zha_lock.teardown_push_subscription()
    assert not zha_lock._push_unsubs


async def test_subscribe_is_idempotent(hass: HomeAssistant, zha_lock: ZHALock) -> None:
    """Test that calling subscribe multiple times is safe."""
    zha_lock.setup_push_subscription()
    first_unsubs = list(zha_lock._push_unsubs)

    zha_lock.setup_push_subscription()
    assert list(zha_lock._push_unsubs) == first_unsubs

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


async def test_get_gateway_handles_value_error(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test _get_gateway logs a warning and returns None on an unexpected ValueError."""
    with patch(
        "custom_components.lock_code_manager.providers.zha._get_zha_gateway_proxy",
        side_effect=ValueError("unexpected"),
    ):
        assert zha_lock._get_gateway() is None


async def test_get_door_lock_cluster_no_device_proxy(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test cluster lookup returns None when device proxy is missing."""
    zha_lock._door_lock_cluster = None
    entity_ref = MagicMock()
    entity_ref.entity_data.device_proxy = None
    gateway = MagicMock()
    gateway.get_entity_reference.return_value = entity_ref
    with patch.object(zha_lock, "_get_gateway", return_value=gateway):
        assert zha_lock._get_door_lock_cluster() is None


async def test_get_door_lock_cluster_no_zigpy_device(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test cluster lookup returns None when the underlying zigpy device is missing."""
    zha_lock._door_lock_cluster = None
    entity_ref = MagicMock()
    entity_ref.entity_data.device_proxy.device.device = None
    gateway = MagicMock()
    gateway.get_entity_reference.return_value = entity_ref
    with patch.object(zha_lock, "_get_gateway", return_value=gateway):
        assert zha_lock._get_door_lock_cluster() is None


async def test_get_door_lock_cluster_no_matching_endpoint_cluster(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test cluster lookup returns None and warns when no endpoint exposes DoorLock."""
    zha_lock._door_lock_cluster = None
    fake_endpoint = MagicMock()
    fake_endpoint.in_clusters = {}
    entity_ref = MagicMock()
    entity_ref.entity_data.device_proxy.device.device.endpoints = {
        0: MagicMock(),
        1: fake_endpoint,
    }
    gateway = MagicMock()
    gateway.get_entity_reference.return_value = entity_ref
    with patch.object(zha_lock, "_get_gateway", return_value=gateway):
        assert zha_lock._get_door_lock_cluster() is None
        # Not cached since no cluster was found.
        assert zha_lock._door_lock_cluster is None


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
    """An unrecognized response is not an answer.

    Reporting it as ``Available`` would say "this slot is free" on the
    strength of a reply that was never understood.
    """
    assert ZHALock._parse_pin_response("unexpected") is None


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
    assert not zha_lock._push_unsubs
    zha_lock.teardown_push_subscription()
    assert not zha_lock._push_unsubs


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


async def test_async_setup_clears_cached_state(
    hass: HomeAssistant, zha_lock: ZHALock, simple_lcm_config_entry: MockConfigEntry
) -> None:
    """Test async_setup clears cached cluster on reconnect."""
    # Prime the cache
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    assert zha_lock._door_lock_cluster is not None

    cluster.read_attributes = AsyncMock(return_value=({}, {}))

    # async_setup should clear the cache so a stale reference isn't kept
    await zha_lock.async_setup(simple_lcm_config_entry)

    # The cluster was re-discovered (cache cleared then re-populated)
    # Verify the cache was cleared by checking it went through discovery again
    assert zha_lock._endpoint_id is not None


async def test_async_setup_is_idempotent(
    hass: HomeAssistant, zha_lock: ZHALock, simple_lcm_config_entry: MockConfigEntry
) -> None:
    """Test calling async_setup twice does not accumulate cluster listeners."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.read_attributes = AsyncMock(return_value=({}, {}))

    await zha_lock.async_setup(simple_lcm_config_entry)
    zha_lock.setup_push_subscription()
    assert zha_lock._push_unsubs

    # Simulate ZHA reload — async_setup tears down and re-initializes
    await zha_lock.async_setup(simple_lcm_config_entry)

    # Listener torn down by async_setup's teardown call
    assert not zha_lock._push_unsubs


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
# Primitive error handling
# ---------------------------------------------------------------------------


async def test_get_users_slot_read_failure(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test async_get_users marks unreadable for slots that fail to read."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.get_pin_code = AsyncMock(side_effect=RuntimeError("zigpy timeout"))

    users = await zha_lock.async_get_users()
    by_slot = {u.user_id: u for u in users}
    assert by_slot[1].pin_credentials[0].state is SlotCredential.unreadable()
    assert by_slot[2].pin_credentials[0].state is SlotCredential.unreadable()


async def test_get_users_no_managed_slots(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test async_get_users returns empty list with no managed slots."""
    users = await zha_lock.async_get_users()
    assert users == []


async def test_set_credential_generic_exception(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test async_set_credential wraps zigpy comms failures as LockDisconnected."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.set_pin_code = AsyncMock(side_effect=RuntimeError("zigpy error"))

    credential = credential_from_slot(1, SlotCredential.known("1234"))
    with pytest.raises(LockDisconnected, match="Failed to set PIN"):
        await zha_lock.async_set_credential(
            1, credential, "1234", name=None, source="direct"
        )


async def test_delete_credential_generic_exception(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test async_delete_credential wraps zigpy comms failures as LockDisconnected."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.clear_pin_code = AsyncMock(side_effect=RuntimeError("zigpy error"))

    ref = CredentialRef(user_id=1, type=CredentialType.PIN, slot=1)
    with pytest.raises(LockDisconnected, match="Failed to clear PIN"):
        await zha_lock.async_delete_credential(ref)


async def test_get_users_propagates_lock_disconnected(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test async_get_users re-raises LockDisconnected instead of marking unreadable.

    Unlike a bare zigpy comms error (which degrades the slot to unreadable),
    a LockDisconnected raised mid-loop means the connection gate itself
    tripped, so it must propagate for reconnect handling rather than being
    swallowed per-slot.
    """
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.get_pin_code = AsyncMock(side_effect=LockDisconnected("gone"))

    with pytest.raises(LockDisconnected, match="gone"):
        await zha_lock.async_get_users()


async def test_hard_refresh_codes(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Test async_hard_refresh_codes re-reads all managed slots from the lock."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None

    async def mock_get_pin_code(slot_num):
        return type(
            "Response",
            (),
            {"user_status": DoorLock.UserStatus.Enabled, "code": "1111"},
        )()

    cluster.get_pin_code = AsyncMock(side_effect=mock_get_pin_code)

    codes = await zha_lock.async_hard_refresh_codes()
    assert codes[1] == SlotCredential.known("1111")
    assert codes[2] == SlotCredential.known("1111")


async def test_programming_event_unparseable_with_coordinator_triggers_refresh(
    hass: HomeAssistant, zha_lock: ZHALock
) -> None:
    """Test unparseable programming event still refreshes as a safety net.

    When the args can't be parsed we don't know which slot changed, but we
    know *something* did — so a coordinator refresh must fire if attached.
    """
    zha_lock.coordinator = MagicMock()
    zha_lock.coordinator.async_request_refresh = AsyncMock()

    zha_lock._handle_programming_event(None)
    await hass.async_block_till_done()

    zha_lock.coordinator.async_request_refresh.assert_called_once()


async def test_occupied_indices_sees_slots_no_entry_manages(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """Occupancy reports codes this integration did not put there.

    ``async_get_users`` is scoped to the slots Lock Code Manager manages
    because it also decides where writes land. Allocation needs the wider
    view: a code programmed by hand holds its index just as firmly, and
    issuing that index to a new user would overwrite it.
    """
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None

    async def mock_get_pin_code(slot_num):
        # Slot 4 is outside anything this entry manages.
        if slot_num == 4:
            return type(
                "Response",
                (),
                {"user_status": DoorLock.UserStatus.Enabled, "code": "9999"},
            )()
        return type(
            "Response",
            (),
            {"user_status": DoorLock.UserStatus.Available, "code": ""},
        )()

    cluster.get_pin_code = AsyncMock(side_effect=mock_get_pin_code)

    codes = await zha_lock.async_get_usercodes(range(1, 6))
    assert codes[4].is_present
    # The default scope cannot see it, which is what the scope argument is for.
    assert 4 not in await zha_lock.async_get_usercodes()


async def test_occupied_indices_stops_at_the_limit(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """The bound is what keeps a per-index lock from being walked end to end."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.get_pin_code = AsyncMock(
        return_value=type(
            "Response", (), {"user_status": DoorLock.UserStatus.Available, "code": ""}
        )()
    )

    codes = await zha_lock.async_get_usercodes(range(1, 4))
    assert all(credential.is_empty for credential in codes.values())
    assert cluster.get_pin_code.await_count == 3


async def test_a_failed_index_is_unreadable_not_empty(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """An index the lock did not answer about is not an empty index.

    Calling it empty understates what the lock holds, and an understated
    answer is the one that overwrites a code. Only a read that fails outright
    makes occupancy unknown; a single index does not.
    """
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None

    async def mock_get_pin_code(slot_num):
        if slot_num == 2:
            raise OSError("radio dropped")
        return type(
            "Response", (), {"user_status": DoorLock.UserStatus.Available, "code": ""}
        )()

    cluster.get_pin_code = AsyncMock(side_effect=mock_get_pin_code)

    codes = await zha_lock.async_get_usercodes(range(1, 4))
    # Unreadable, not empty: the index holds something we could not read, and
    # calling that empty is what lets a real credential be overwritten.
    assert codes[2] is SlotCredential.unreadable()
    assert codes[1].is_empty and codes[3].is_empty


async def test_occupied_indices_counts_a_write_only_slot(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """An enabled slot is occupied even when the lock will not return its code."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.get_pin_code = AsyncMock(
        return_value=type(
            "Response", (), {"user_status": DoorLock.UserStatus.Enabled, "code": ""}
        )()
    )

    codes = await zha_lock.async_get_usercodes(range(1, 3))
    assert codes[1] is SlotCredential.unreadable()
    assert codes[2] is SlotCredential.unreadable()


async def test_unparseable_response_is_unreadable_not_empty(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """A reply we cannot parse is not an answer that the slot is free.

    Calling it empty would tell sync the slot is confirmed cleared, and tell
    allocation the index is available -- both from a response nothing
    understood.
    """
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.get_pin_code = AsyncMock(return_value="unexpected shape")

    codes = await zha_lock.async_get_usercodes(range(1, 3))

    assert codes[1] is SlotCredential.unreadable()
    assert codes[2] is SlotCredential.unreadable()


@pytest.mark.parametrize(
    ("user_status", "code"),
    [
        (DoorLock.UserStatus.Disabled, "1234"),
        (DoorLock.UserStatus.Disabled, ""),
        (DoorLock.UserStatus.Not_Supported, ""),
    ],
)
async def test_only_available_means_the_slot_is_free(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
    user_status: int,
    code: str,
) -> None:
    """DISABLED holds a code the lock is refusing, not an empty slot.

    The ZCL has four statuses and only AVAILABLE means nothing is there.
    Reading DISABLED as cleared tells sync to reprogram a slot that already
    holds a code, and tells allocation the index is free to hand out.
    """
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.get_pin_code = AsyncMock(
        return_value=type("Response", (), {"user_status": user_status, "code": code})()
    )

    codes = await zha_lock.async_get_usercodes(range(1, 3))

    assert codes[1].is_present
    assert codes[2].is_present


async def test_available_is_an_empty_slot(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """The one status that does mean cleared still reads as cleared."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.get_pin_code = AsyncMock(
        return_value=type(
            "Response", (), {"user_status": DoorLock.UserStatus.Available, "code": ""}
        )()
    )

    codes = await zha_lock.async_get_usercodes(range(1, 3))

    assert codes[1].is_empty
    assert codes[2].is_empty
