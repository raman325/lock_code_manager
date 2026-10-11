"""Test the ZHA lock provider."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import zigpy.types as zigpy_t
from zigpy.zcl import foundation
from zigpy.zcl.clusters.closures import DoorLock
from zigpy.zcl.foundation import Status

from homeassistant.components.zha.const import DOMAIN as ZHA_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.lock_code_manager.const import MAX_SEARCHED_SLOT
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
from custom_components.lock_code_manager.providers import (
    _base as base_module,
    zha as zha_module,
)
from custom_components.lock_code_manager.providers.zha import (
    ZHALock,
)
from tests.providers.helpers import ProviderNativeTransportContractTests

from .helpers import (
    QuirkedDoorLock,
    RenamingQuirkDoorLock,
    default_response,
    lock_frame,
    quirk_redefining,
)

_GET_PIN_CODE_RESPONSE = DoorLock.ClientCommandDefs.get_pin_code_response
_SET_PIN_CODE_ID = DoorLock.ServerCommandDefs.set_pin_code.id
_CLEAR_PIN_CODE_ID = DoorLock.ServerCommandDefs.clear_pin_code.id
# Set and Clear PIN Code Response general failure (Zigbee Cluster Library).
_DOOR_LOCK_GENERAL_FAILURE = 1


def _pin_reply(
    user_id: int, user_status: DoorLock.UserStatus, code: str = ""
) -> foundation.CommandSchema:
    """Build the Get PIN Code Response zigpy decodes for one user."""
    return _GET_PIN_CODE_RESPONSE.schema(
        user_id=user_id,
        user_status=user_status,
        user_type=DoorLock.UserType.Unrestricted,
        code=code,
    )


def _every_slot_answers(
    user_status: DoorLock.UserStatus, code: str = ""
) -> Callable[[int], Awaitable[foundation.CommandSchema]]:
    """Answer every get_pin_code, for the slot asked, with one status and code."""

    async def _get_pin_code(slot_num: int) -> foundation.CommandSchema:
        return _pin_reply(slot_num, user_status, code)

    return _get_pin_code


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
            return _pin_reply(slot_num, DoorLock.UserStatus.Enabled, "1234")
        return _pin_reply(slot_num, DoorLock.UserStatus.Available, "")

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
            return _pin_reply(slot_num, DoorLock.UserStatus.Enabled, "1234")
        return _pin_reply(slot_num, DoorLock.UserStatus.Available, "")

    cluster.get_pin_code = AsyncMock(side_effect=mock_get_pin_code)

    codes = await zha_lock.async_get_usercodes()

    assert codes[1] == SlotCredential.known("1234")
    assert codes[2] is SlotCredential.empty()


# A write as each operation's cluster command: its name, request id, response.
_WRITES = pytest.mark.parametrize(
    ("command", "request_id", "response"),
    [
        ("set_pin_code", _SET_PIN_CODE_ID, "set_pin_code_response"),
        ("clear_pin_code", _CLEAR_PIN_CODE_ID, "clear_pin_code_response"),
    ],
    ids=["set", "clear"],
)


async def _write(zha_lock: ZHALock, command: str) -> WriteResult | bool:
    """Set a code on slot 3, or clear slot 3, as ``command`` names."""
    if command == "set_pin_code":
        return await zha_lock.async_set_credential(
            3,
            credential_from_slot(3, SlotCredential.known("5678")),
            "5678",
            name="Test User",
            source="direct",
        )
    return await zha_lock.async_delete_credential(
        CredentialRef(user_id=3, type=CredentialType.PIN, slot=3)
    )


def _other_write(response: str) -> str:
    """Return the other write's response name."""
    return (
        "clear_pin_code_response"
        if response == "set_pin_code_response"
        else "set_pin_code_response"
    )


# Replies that give a write no status: each leaves the write standing. Each
# is built, from the lock's own cluster, as zigpy decodes it.
_NO_ANSWERS = [
    pytest.param(
        lambda cluster, _request_id, response: (
            lock_frame(cluster, response, status=Status.SUCCESS).payload
        ),
        id="own_response_success",
    ),
    pytest.param(
        lambda _cluster, request_id, _response: (
            default_response(request_id, Status.SUCCESS).payload
        ),
        id="default_response_success",
    ),
    pytest.param(
        lambda _cluster, _request_id, _response: (
            default_response(
                DoorLock.ServerCommandDefs.lock_door.id, Status.FAILURE
            ).payload
        ),
        id="default_response_naming_another_command",
    ),
    pytest.param(
        lambda cluster, _request_id, response: (
            lock_frame(cluster, _other_write(response), status=Status.FAILURE).payload
        ),
        id="the_other_writes_response",
    ),
    # Shares the Default Response's command id, and carries a status.
    pytest.param(
        lambda cluster, _request_id, _response: (
            lock_frame(
                cluster, "set_week_day_schedule_response", status=Status.FAILURE
            ).payload
        ),
        id="cluster_command_sharing_the_default_response_id",
    ),
    # A command the cluster has no definition for reaches the request as bytes.
    pytest.param(
        lambda _cluster, _request_id, _response: b"\x01",
        id="undecoded_bytes",
    ),
]

# The write's own refusal, in either reply that answers it.
_OWN_REFUSALS = [
    pytest.param(
        lambda cluster, _request_id, response: (
            lock_frame(cluster, response, status=_DOOR_LOCK_GENERAL_FAILURE).payload
        ),
        id="own_response",
    ),
    pytest.param(
        lambda _cluster, request_id, _response: (
            default_response(request_id, Status.FAILURE).payload
        ),
        id="default_response_naming_the_write",
    ),
]

_CLUSTERS = pytest.mark.parametrize(
    "door_lock_cluster_type",
    [DoorLock, QuirkedDoorLock, RenamingQuirkDoorLock],
    ids=["stock", "quirk", "renaming_quirk"],
)


@_CLUSTERS
@_WRITES
@pytest.mark.parametrize("reply", _NO_ANSWERS)
async def test_a_write_its_reply_gives_no_status_stands(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
    command: str,
    request_id: int,
    response: str,
    reply: Any,
) -> None:
    """
    A write not refused in its own reply is pushed and confirmed.

    zigpy pairs a reply with its request by sequence number, so a frame about
    another command can fill the write; its status says nothing of the write.
    """
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    setattr(
        cluster, command, AsyncMock(return_value=reply(cluster, request_id, response))
    )
    zha_lock.coordinator = MagicMock()

    result = await _write(zha_lock, command)

    if command == "set_pin_code":
        assert result is WriteResult.CONFIRMED
        cluster.set_pin_code.assert_called_once_with(
            3,
            DoorLock.UserStatus.Enabled,
            DoorLock.UserType.Unrestricted,
            "5678",
        )
        pushed = SlotCredential.known("5678")
    else:
        assert result is True
        cluster.clear_pin_code.assert_called_once_with(3)
        pushed = SlotCredential.empty()
    zha_lock.coordinator.push_update.assert_called_once_with({3: pushed})


@_CLUSTERS
@_WRITES
@pytest.mark.parametrize("reply", _OWN_REFUSALS)
async def test_the_writes_own_refusal_rejects_the_code(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
    command: str,
    request_id: int,
    response: str,
    reply: Any,
) -> None:
    """A non-zero status in the write's own reply rejects it and pushes nothing."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    setattr(
        cluster, command, AsyncMock(return_value=reply(cluster, request_id, response))
    )
    zha_lock.coordinator = MagicMock()

    with pytest.raises(CodeRejectedError, match=f"{command} rejected: status"):
        await _write(zha_lock, command)
    zha_lock.coordinator.push_update.assert_not_called()


# A quirk redefining a write's own response so it carries no numeric status:
# the write, its response, the quirk's cluster, and one such reply's fields.
_NO_NUMERIC_STATUS = [
    pytest.param(
        command,
        response,
        quirk_redefining(response, schema),
        fields,
        id=f"{command}-status_{shape}",
    )
    for command, response in (
        ("set_pin_code", "set_pin_code_response"),
        ("clear_pin_code", "clear_pin_code_response"),
    )
    for shape, schema, fields in (
        ("named_otherwise", {"result": foundation.Status}, {"result": Status.FAILURE}),
        ("absent", {"status?": foundation.Status}, {}),
        (
            "not_a_number",
            {"status": zigpy_t.LVBytes},
            {"status": zigpy_t.LVBytes(b"\x01")},
        ),
    )
]


@pytest.mark.parametrize(
    ("command", "response", "door_lock_cluster_type", "fields"), _NO_NUMERIC_STATUS
)
async def test_an_own_response_with_no_numeric_status_is_no_answer(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
    command: str,
    response: str,
    fields: dict[str, Any],
) -> None:
    """A quirk's own response that carries no numeric status leaves the write standing."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    setattr(
        cluster,
        command,
        AsyncMock(return_value=lock_frame(cluster, response, **fields).payload),
    )
    zha_lock.coordinator = MagicMock()

    assert await _write(zha_lock, command) in (WriteResult.CONFIRMED, True)
    zha_lock.coordinator.push_update.assert_called_once()


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


def _get_pin_code_response(
    cluster: type[DoorLock], user_id: int = 3, **fields: Any
) -> foundation.CommandSchema:
    """Build a Get PIN Code Response for ``user_id`` as ``cluster`` decodes it."""
    return lock_frame(
        cluster,
        "get_pin_code_response",
        **{
            "user_id": user_id,
            "user_status": DoorLock.UserStatus.Enabled,
            "user_type": DoorLock.UserType.Unrestricted,
            "code": "1234",
            **fields,
        },
    ).payload


@pytest.mark.parametrize(
    "cluster",
    [DoorLock, QuirkedDoorLock, RenamingQuirkDoorLock],
    ids=["stock", "quirk", "renaming_quirk"],
)
def test_parse_pin_response_reads_the_slots_reply(cluster: type[DoorLock]) -> None:
    """
    The Get PIN Code Response the lock's cluster decodes is the slot's answer.

    A quirk's redefinition, under the stock name or another, is a schema class
    of its own; it is still the reply.
    """
    assert ZHALock._parse_pin_response(_get_pin_code_response(cluster), 3, cluster) == (
        DoorLock.UserStatus.Enabled,
        "1234",
    )


@pytest.mark.parametrize(
    "cluster",
    [DoorLock, QuirkedDoorLock, RenamingQuirkDoorLock],
    ids=["stock", "quirk", "renaming_quirk"],
)
@pytest.mark.parametrize(
    "reply",
    [
        pytest.param(
            lambda cluster: _get_pin_code_response(cluster, user_id=4),
            id="another_users_reply",
        ),
        # Every decoded zigpy command is a tuple, so read positionally this
        # passed its event code for a user status and its code for the slot's.
        pytest.param(
            lambda cluster: (
                lock_frame(
                    cluster,
                    "operation_event_notification",
                    operation_event_source=DoorLock.OperationEventSource.Keypad,
                    operation_event_code=DoorLock.OperationEvent.Lock,
                    user_id=3,
                    pin="1234",
                    local_time=0,
                ).payload
            ),
            id="operation_event",
        ),
        pytest.param(
            lambda cluster: (
                lock_frame(
                    cluster, "set_pin_code_response", status=Status.SUCCESS
                ).payload
            ),
            id="a_writes_response",
        ),
        pytest.param(
            lambda _cluster: (
                default_response(
                    DoorLock.ServerCommandDefs.get_pin_code.id, Status.SUCCESS
                ).payload
            ),
            id="default_response",
        ),
        # A command the cluster has no definition for reaches the request as bytes.
        pytest.param(lambda _cluster: b"unexpected", id="undecoded_bytes"),
    ],
)
def test_parse_pin_response_rejects_a_reply_that_is_not_the_slots(
    cluster: type[DoorLock], reply: Any
) -> None:
    """
    A frame that is not slot 3's Get PIN Code Response is no answer for slot 3.

    zigpy pairs a reply with its request by sequence number, not by the user
    or command it is about. Reporting such a reply as ``Available`` would say
    "this slot is free" on the strength of a reply that was never about it.
    """
    assert ZHALock._parse_pin_response(reply(cluster), 3, cluster) is None


@pytest.mark.parametrize(
    ("schema", "fields", "parsed"),
    [
        pytest.param(
            {"code": zigpy_t.LVBytes},
            {"code": zigpy_t.LVBytes(b"1234")},
            (DoorLock.UserStatus.Enabled, "1234"),
            id="code_as_bytes",
        ),
        pytest.param(
            {"code": None, "pin": zigpy_t.CharacterString},
            {"pin": "1234"},
            (DoorLock.UserStatus.Enabled, ""),
            id="code_named_otherwise",
        ),
        pytest.param(
            {"user_status": None, "state": DoorLock.UserStatus},
            {"state": DoorLock.UserStatus.Enabled},
            None,
            id="user_status_named_otherwise",
        ),
        pytest.param(
            {"user_id": None, "user": zigpy_t.uint16_t},
            {"user": 3},
            None,
            id="user_id_named_otherwise",
        ),
    ],
)
def test_parse_pin_response_reads_a_quirks_fields_by_name(
    schema: dict[str, Any], fields: dict[str, Any], parsed: tuple[int, str] | None
) -> None:
    """
    A quirk's Get PIN Code Response is read by field name, guarded.

    A code decoded as bytes is its text; no code field is no code; no user
    status, or no user to match against the slot, is no answer.
    """
    stock = {field.name: field.type for field in _GET_PIN_CODE_RESPONSE.schema.fields}
    redefined = {
        name: kind for name, kind in {**stock, **schema}.items() if kind is not None
    }
    cluster = quirk_redefining("get_pin_code_response", redefined)
    values = {
        "user_id": 3,
        "user_status": DoorLock.UserStatus.Enabled,
        "user_type": DoorLock.UserType.Unrestricted,
        "code": "1234",
        **fields,
    }
    reply = lock_frame(
        cluster,
        "get_pin_code_response",
        **{name: value for name, value in values.items() if name in redefined},
    ).payload

    assert ZHALock._parse_pin_response(reply, 3, cluster) == parsed


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
        return _pin_reply(slot_num, DoorLock.UserStatus.Enabled, "1111")

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
            return _pin_reply(slot_num, DoorLock.UserStatus.Enabled, "9999")
        return _pin_reply(slot_num, DoorLock.UserStatus.Available, "")

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
        side_effect=_every_slot_answers(DoorLock.UserStatus.Available, "")
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
        return _pin_reply(slot_num, DoorLock.UserStatus.Available, "")

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
        side_effect=_every_slot_answers(DoorLock.UserStatus.Enabled, "")
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
    # A command the cluster has no definition for reaches the request as bytes.
    cluster.get_pin_code = AsyncMock(return_value=b"unexpected shape")

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
    """AVAILABLE is the only status that leaves an index free to write to.

    Reading any of the others as cleared tells sync to reprogram the slot and
    tells allocation to hand the index out. What each of them means beyond
    "not free" is something this credential model cannot express, so they are
    reported alike.
    """
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.get_pin_code = AsyncMock(side_effect=_every_slot_answers(user_status, code))

    codes = await zha_lock.async_get_usercodes(range(1, 3))

    assert codes[1].is_present
    assert codes[2].is_present
    # And never as a value sync can compare: a code the lock is not
    # accepting would otherwise read as in sync while the door stays shut.
    assert not codes[1].is_readable
    assert not codes[2].is_readable


@pytest.mark.parametrize(
    ("code", "readable"),
    [
        pytest.param("****", False, id="masked"),
        pytest.param("1234", True, id="digits"),
        # Asterisks mixed with digits are not a shape any lock produces, and
        # guessing at partial masking would discard a code that is there.
        pytest.param("12**", True, id="partially_masked"),
    ],
)
async def test_a_masked_code_is_unreadable_not_known(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
    code: str,
    readable: bool,
) -> None:
    """
    A lock withholding its codes answers with an asterisk per digit.

    The reply arrives and the slot is plainly Enabled, but the asterisks can
    never equal the configured PIN -- confirming them as the code makes sync
    reprogram the slot on every tick, forever. Same Zigbee hardware, same
    behaviour, and the same guard the Zigbee2MQTT read path applies.
    """
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.get_pin_code = AsyncMock(
        side_effect=_every_slot_answers(DoorLock.UserStatus.Enabled, code)
    )

    codes = await zha_lock.async_get_usercodes(range(1, 2))

    assert codes[1].is_present
    assert codes[1].is_readable is readable


async def test_available_is_an_empty_slot(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """The one status that does mean cleared still reads as cleared."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.get_pin_code = AsyncMock(
        side_effect=_every_slot_answers(DoorLock.UserStatus.Available, "")
    )

    codes = await zha_lock.async_get_usercodes(range(1, 3))

    assert codes[1].is_empty
    assert codes[2].is_empty


async def test_max_slot_comes_from_the_zcl_attribute(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """The Zigbee Cluster Library defines this, so the lock is asked."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    attr_id = DoorLock.AttributeDefs.num_of_pin_users_supported.id
    cluster.read_attributes = AsyncMock(return_value=({attr_id: 30}, {}))

    assert await zha_lock.async_get_max_slot() == 30
    # The stub answers whatever it is asked, so the request itself is what
    # says this reads the PIN user count rather than some neighbouring
    # attribute that happens to be a number.
    assert cluster.read_attributes.await_args.args[0] == [attr_id]
    assert cluster.read_attributes.await_args.kwargs["allow_cache"] is True


@pytest.mark.parametrize(
    "response",
    [
        ({}, {}),
        ({DoorLock.AttributeDefs.num_of_pin_users_supported.id: 0}, {}),
    ],
)
async def test_max_slot_falls_back_when_the_lock_will_not_say(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
    response: tuple,
) -> None:
    """A missing or zero count means the lock declined, not that it has none.

    Treating zero as "no slots" would stop the search before it began, and
    treating it as a real range would blame the lock for a limit it never
    reported. It has no opinion.
    """
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.read_attributes = AsyncMock(return_value=response)

    assert await zha_lock.async_get_max_slot() is None


async def test_max_slot_falls_back_when_the_lock_cannot_be_reached(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """An unreachable lock still has to yield a place for the search to stop."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.read_attributes = AsyncMock(side_effect=OSError("radio dropped"))

    assert await zha_lock.async_get_max_slot() is None


async def test_max_slot_does_not_believe_an_implausible_range(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """A 16-bit attribute can claim a range no search should walk.

    This provider spends a round trip per index, so taking 65535 at its word
    would mean tens of thousands of them before the flow could refuse.
    """
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    attr_id = DoorLock.AttributeDefs.num_of_pin_users_supported.id
    cluster.read_attributes = AsyncMock(return_value=({attr_id: 65535}, {}))

    assert await zha_lock.async_get_max_slot() == MAX_SEARCHED_SLOT


async def test_max_slot_survives_a_reply_it_cannot_unpack(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """This always answers, including for a reply of the wrong shape."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    cluster.read_attributes = AsyncMock(return_value=None)

    assert await zha_lock.async_get_max_slot() is None


async def test_a_scoped_read_touches_only_the_slots_asked_for(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """
    The scope is the whole reason allocation is affordable.

    This cluster answers one index per round trip, so reading past the scope
    is not wrong, only slow -- which is exactly why nothing would catch it.
    Allocation widens its search a few numbers at a time precisely so a lock
    is asked about a handful of indices rather than its whole range.
    """
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None

    async def mock_get_pin_code(slot_num):
        return _pin_reply(slot_num, DoorLock.UserStatus.Available, "")

    cluster.get_pin_code = AsyncMock(side_effect=mock_get_pin_code)

    await zha_lock.async_get_users(slots={2})

    asked = {call.args[0] for call in cluster.get_pin_code.call_args_list}
    assert asked == {2}


async def test_zha_declares_a_per_exchange_budget_and_keeps_the_flat_floor(
    zha_lock: ZHALock,
) -> None:
    """
    One ZCL command per slot, each of them slow, is declared, not overridden.

    Each of zigpy's three attempts waits for the radio's send confirmation
    (up to 120s on zigpy-xbee) and then 28s for the reply, so one command can
    take 444s before zigpy gives up on it -- and its per-device concurrency
    gate can hold the command behind one more attempt before it is sent. The
    declared budget sits above that on every radio zigpy ships, so zigpy's own
    word about a slot always comes first. The flat floor is unchanged: the
    walk scales it at the call site (#1528).
    """
    assert ZHALock.per_exchange_budget == zha_module._WORST_CASE_ZCL_COMMAND
    assert zha_module._WORST_CASE_ZCL_COMMAND > 4 * (120 + 28)
    with patch.object(
        type(zha_lock), "managed_slots", property(lambda _self: frozenset(range(1, 11)))
    ):
        assert zha_lock.operation_timeout_seconds == base_module.OPERATION_TIMEOUT
        assert zha_lock._operation_budget(10) == 10 * zha_module._WORST_CASE_ZCL_COMMAND


async def test_a_slot_read_nobody_answers_is_a_disconnect_not_an_unreadable_slot(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """A command nothing ever answers ends the walk as a disconnect at once.

    zigpy gives up on a flaky slot by itself; a read that outlives even that
    is a wedged transport, caught on the first silent slot rather than after
    the whole walk (#1528).
    """
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None

    async def _never_answers(_slot_num):
        await asyncio.Event().wait()

    cluster.get_pin_code = AsyncMock(side_effect=_never_answers)
    with (
        patch.object(zha_module, "_WORST_CASE_ZCL_COMMAND", 0.05),
        pytest.raises(LockDisconnected, match="no answer for slot"),
    ):
        await asyncio.wait_for(zha_lock.async_get_users(), timeout=5)


async def test_zigpys_own_timeout_marks_the_slot_unreadable_and_walks_on(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """zigpy giving up on one slot is a flaky slot, not a dead transport."""
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None

    async def mock_get_pin_code(slot_num):
        if slot_num == 1:
            raise TimeoutError("zigpy gave up")
        return _pin_reply(slot_num, DoorLock.UserStatus.Available, "")

    cluster.get_pin_code = AsyncMock(side_effect=mock_get_pin_code)
    users = await zha_lock.async_get_users()
    by_slot = {u.user_id: u for u in users}
    assert by_slot[1].pin_credentials[0].state == SlotCredential.unreadable()
    assert by_slot[2].pin_credentials[0].state is SlotCredential.empty()


async def test_scoped_hard_refresh_still_names_every_managed_slot(
    hass: HomeAssistant,
    zha_lock: ZHALock,
    simple_lcm_config_entry: MockConfigEntry,
) -> None:
    """A cacheless provider's scoped refresh walks managed slots plus the scope.

    The coordinator replaces its data with the answer, so the answer must
    name every managed slot; and the deadline the base declares must be the
    walk this actually makes (#1528).
    """
    cluster = zha_lock._get_door_lock_cluster()
    assert cluster is not None
    asked: list[int] = []

    async def mock_get_pin_code(slot_num):
        asked.append(slot_num)
        return _pin_reply(slot_num, DoorLock.UserStatus.Available, "")

    cluster.get_pin_code = AsyncMock(side_effect=mock_get_pin_code)
    codes = await zha_lock.async_hard_refresh_codes({7})

    assert set(asked) == set(zha_lock.managed_slots) | {7}
    assert set(zha_lock.managed_slots) <= set(codes)
