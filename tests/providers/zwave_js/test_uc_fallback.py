"""Tests for the temporary Z-Wave JS User Code CC fallback (issue #1251).

Covers detection, write/read/hard-refresh routing, push handling, and the
full setup lifecycle for locks whose unified access-control capabilities
are degenerate. Delete this file together with
``providers/_zwave_js_uc.py`` once the upstream driver fix
(zwave-js/zwave-js#8873) is the minimum supported version -- see that
module's docstring for the full removal recipe.
"""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from zwave_js_server.const import CommandClass, SetValueStatus
from zwave_js_server.const.command_class.access_control import (
    UserCredentialType,
)
from zwave_js_server.const.command_class.lock import CodeSlotStatus
from zwave_js_server.event import Event as ZwaveEvent
from zwave_js_server.exceptions import FailedZWaveCommand, NotFoundError
from zwave_js_server.model.node import Node
from zwave_js_server.model.value import SetValueResult

from homeassistant.components.zwave_js import lock_helpers
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_SLOTS,
    DOMAIN,
)
from custom_components.lock_code_manager.domain.credentials import (
    Credential,
    CredentialRef,
    CredentialType,
    WriteResult,
)
from custom_components.lock_code_manager.domain.exceptions import (
    CodeRejectedError,
    LockDisconnected,
    LockOperationFailed,
)
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers._zwave_js_uc import (
    ZWaveJSUserCodeFallbackSupport,
)
from custom_components.lock_code_manager.providers.zwave_js import ZWaveJSLock

from .conftest import get_zwave_lock, uc_only_caps_response, uc_slot_walk

# ---------------------------------------------------------------------------
# User Code CC fallback (issue #1251)
#
# When the unified access-control API reports no usable PIN capabilities
# but the node advertises User Code CC, the provider routes all PIN
# operations through the legacy User Code CC utilities.
# ---------------------------------------------------------------------------


async def test_capabilities_fall_back_to_uc_when_pin_missing(
    uc_fallback_lock: ZWaveJSLock,
    mock_uc_utils: dict,
) -> None:
    """Degenerate unified capabilities + UC slots found -> slot-only capabilities."""
    mock_uc_utils["get_usercodes"].return_value = uc_slot_walk(250)

    caps = await uc_fallback_lock.async_get_capabilities()

    assert caps.supports_user_management is False
    assert caps.max_user_name_length == 0
    assert CredentialType.PIN in caps.credential_types
    assert caps.credential_types[CredentialType.PIN].num_slots == 250


async def test_capabilities_fall_back_to_uc_when_pin_zero_slots(
    uc_fallback_lock: ZWaveJSLock,
    mock_lock_helpers: dict,
    mock_uc_utils: dict,
) -> None:
    """PIN advertised with num_slots=0 (the 'between 1 and 0' shape) -> UC fallback."""
    pin_type_str = lock_helpers.CREDENTIAL_TYPE_MAP[UserCredentialType.PIN_CODE]
    caps_response = uc_only_caps_response()
    caps_response["supported_credential_types"] = {
        pin_type_str: {
            "num_slots": 0,
            "min_length": 4,
            "max_length": 8,
            "supports_learn": False,
        }
    }
    mock_lock_helpers["async_get_credential_capabilities"].return_value = caps_response

    caps = await uc_fallback_lock.async_get_capabilities()

    assert caps.supports_user_management is False
    assert caps.credential_types[CredentialType.PIN].num_slots == 30


async def test_capabilities_empty_when_degenerate_and_no_uc_slots(
    uc_fallback_lock: ZWaveJSLock,
    mock_uc_utils: dict,
) -> None:
    """Degenerate unified capabilities and an empty UC walk -> no PIN support."""
    mock_uc_utils["get_usercodes"].return_value = []

    caps = await uc_fallback_lock.async_get_capabilities()

    assert caps.supports_user_management is False
    assert caps.credential_types == {}


async def test_capabilities_no_fallback_when_node_lacks_user_code_cc(
    uc_fallback_lock: ZWaveJSLock,
    mock_uc_utils: dict,
) -> None:
    """Without User Code CC on the node, degenerate capabilities stay degenerate.

    The fallback would be useless (the UC utilities have nothing to talk
    to), so the value DB walk must not even be attempted.
    """
    with patch.object(ZWaveJSLock, "_node_supports_user_code_cc", return_value=False):
        caps = await uc_fallback_lock.async_get_capabilities()

    assert caps.credential_types == {}
    mock_uc_utils["get_usercodes"].assert_not_called()


async def test_capabilities_healthy_unified_caps_disable_fallback(
    zwave_js_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
    mock_uc_utils: dict,
) -> None:
    """Usable unified capabilities -> no fallback, UC walk not attempted.

    Once the upstream node-zwave-js fix (zwave-js/zwave-js#8873) reports
    real PIN slots for these locks, this is the path they take and the
    fallback becomes a no-op.
    """
    caps = await zwave_js_lock.async_get_capabilities()

    assert caps.supports_user_management is True
    assert caps.credential_types[CredentialType.PIN].num_slots == 30
    mock_uc_utils["get_usercodes"].assert_not_called()

    # Writes route through the HA helper, not the UC utilities.
    credential = Credential(
        type=CredentialType.PIN, slot=2, state=SlotCredential.known("5678")
    )
    await zwave_js_lock.async_set_credential(
        user_id=1, credential=credential, pin="5678", name=None, source="sync"
    )
    mock_lock_helpers["async_set_credential"].assert_called_once()
    mock_uc_utils["set_usercode"].assert_not_called()


async def test_uc_set_credential_uses_user_code_cc_util(
    uc_fallback_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
    mock_uc_utils: dict,
) -> None:
    """UC-mode writes go through ``set_usercode``, not the unified API."""
    credential = Credential(
        type=CredentialType.PIN, slot=5, state=SlotCredential.known("4321")
    )
    result = await uc_fallback_lock.async_set_credential(
        user_id=5, credential=credential, pin="4321", name=None, source="sync"
    )

    assert result is WriteResult.CONFIRMED
    mock_uc_utils["set_usercode"].assert_awaited_once_with(
        uc_fallback_lock.node, 5, "4321"
    )
    mock_lock_helpers["async_set_credential"].assert_not_called()
    mock_access_control.set_credential.assert_not_called()


async def test_uc_set_credential_skips_when_code_matches(
    uc_fallback_lock: ZWaveJSLock,
    mock_uc_utils: dict,
) -> None:
    """A cached identical unmasked code short-circuits the write."""
    mock_uc_utils["get_usercode"].side_effect = None
    mock_uc_utils["get_usercode"].return_value = {
        "code_slot": 5,
        "name": "Slot 5",
        "in_use": True,
        "usercode": "4321",
    }

    credential = Credential(
        type=CredentialType.PIN, slot=5, state=SlotCredential.known("4321")
    )
    result = await uc_fallback_lock.async_set_credential(
        user_id=5, credential=credential, pin="4321", name=None, source="sync"
    )

    assert result is WriteResult.NO_CHANGE
    mock_uc_utils["set_usercode"].assert_not_called()


async def test_uc_set_credential_proceeds_when_masked(
    uc_fallback_lock: ZWaveJSLock,
    mock_uc_utils: dict,
) -> None:
    """A masked cached code can't be compared, so the write proceeds."""
    mock_uc_utils["get_usercode"].side_effect = None
    mock_uc_utils["get_usercode"].return_value = {
        "code_slot": 5,
        "name": "Slot 5",
        "in_use": True,
        "usercode": "****",
    }

    credential = Credential(
        type=CredentialType.PIN, slot=5, state=SlotCredential.known("4321")
    )
    result = await uc_fallback_lock.async_set_credential(
        user_id=5, credential=credential, pin="4321", name=None, source="sync"
    )

    assert result is WriteResult.CONFIRMED
    mock_uc_utils["set_usercode"].assert_awaited_once()


async def test_uc_set_fail_status_logs_and_continues(
    uc_fallback_lock: ZWaveJSLock,
    mock_uc_utils: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A transient ``FAIL`` set-value status is tolerated, not a rejection.

    3.3.0 routed sets through the HA service ``zwave_js.set_lock_usercode``,
    which never inspected ``SetValueResult.status`` -- transient ``FAIL``
    responses (the canonical case on flaky-interview Schlage UC v1 locks,
    #1251) were silently swallowed and the slot stayed enabled. The
    structural statuses (``NO_DEVICE_SUPPORT`` etc.) still escalate to
    ``CodeRejectedError`` because they are real impossibilities.
    """
    mock_coordinator = MagicMock()
    mock_coordinator.data = {}
    uc_fallback_lock.coordinator = mock_coordinator

    mock_uc_utils["set_usercode"].return_value = SetValueResult(
        {"status": SetValueStatus.FAIL}
    )

    credential = Credential(
        type=CredentialType.PIN, slot=5, state=SlotCredential.known("4321")
    )
    with caplog.at_level(logging.INFO):
        result = await uc_fallback_lock.async_set_credential(
            user_id=5, credential=credential, pin="4321", name=None, source="sync"
        )

    assert result is WriteResult.CONFIRMED
    mock_coordinator.push_update.assert_called_once_with(
        {5: SlotCredential.known("4321")}
    )
    assert "set returned FAIL" in caplog.text


@pytest.mark.parametrize(
    "status",
    [
        SetValueStatus.NO_DEVICE_SUPPORT,
        SetValueStatus.ENDPOINT_NOT_FOUND,
        SetValueStatus.NOT_IMPLEMENTED,
        SetValueStatus.INVALID_VALUE,
    ],
    ids=lambda s: s.name,
)
async def test_uc_set_fatal_status_raises_code_rejected(
    uc_fallback_lock: ZWaveJSLock,
    mock_uc_utils: dict,
    status: SetValueStatus,
) -> None:
    """Structural-impossibility statuses still raise ``CodeRejectedError``.

    These mean the operation cannot succeed on this device/endpoint --
    not transient -- so the slot should be rejected loudly.
    """
    mock_uc_utils["set_usercode"].return_value = SetValueResult({"status": status})

    credential = Credential(
        type=CredentialType.PIN, slot=5, state=SlotCredential.known("4321")
    )
    with pytest.raises(CodeRejectedError, match=status.name):
        await uc_fallback_lock.async_set_credential(
            user_id=5, credential=credential, pin="4321", name=None, source="sync"
        )


async def test_uc_set_credential_transport_error_raises_lock_disconnected(
    uc_fallback_lock: ZWaveJSLock,
    mock_uc_utils: dict,
) -> None:
    """A Z-Wave transport failure maps to LockDisconnected (retry path)."""
    mock_uc_utils["set_usercode"].side_effect = FailedZWaveCommand(
        "cmd", 1, "node gone"
    )

    credential = Credential(
        type=CredentialType.PIN, slot=5, state=SlotCredential.known("4321")
    )
    with pytest.raises(LockDisconnected, match="set usercode slot 5"):
        await uc_fallback_lock.async_set_credential(
            user_id=5, credential=credential, pin="4321", name=None, source="sync"
        )


async def test_uc_set_credential_missing_slot_raises_code_rejected(
    uc_fallback_lock: ZWaveJSLock,
    mock_uc_utils: dict,
) -> None:
    """A slot with no value in the DB is a permanent rejection, not a retry."""
    mock_uc_utils["set_usercode"].side_effect = NotFoundError("no such slot")

    credential = Credential(
        type=CredentialType.PIN, slot=99, state=SlotCredential.known("4321")
    )
    with pytest.raises(CodeRejectedError, match="slot not found"):
        await uc_fallback_lock.async_set_credential(
            user_id=99, credential=credential, pin="4321", name=None, source="sync"
        )


async def test_uc_delete_credential_uses_clear_usercode(
    uc_fallback_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
    mock_uc_utils: dict,
) -> None:
    """UC-mode deletes go through ``clear_usercode``, not the unified API."""
    ref = CredentialRef(user_id=3, type=CredentialType.PIN, slot=3)
    result = await uc_fallback_lock.async_delete_credential(ref)

    assert result is True
    mock_uc_utils["clear_usercode"].assert_awaited_once_with(uc_fallback_lock.node, 3)
    mock_lock_helpers["async_delete_credential"].assert_not_called()
    mock_access_control.delete_credential.assert_not_called()


async def test_uc_delete_credential_skips_when_already_clear(
    uc_fallback_lock: ZWaveJSLock,
    mock_uc_utils: dict,
) -> None:
    """Clearing an already-clear slot short-circuits."""
    mock_uc_utils["get_usercode"].side_effect = None
    mock_uc_utils["get_usercode"].return_value = {
        "code_slot": 3,
        "name": "Slot 3",
        "in_use": False,
        "usercode": None,
    }

    ref = CredentialRef(user_id=3, type=CredentialType.PIN, slot=3)
    result = await uc_fallback_lock.async_delete_credential(ref)

    assert result is False
    mock_uc_utils["clear_usercode"].assert_not_called()


@pytest.mark.parametrize(
    "current_value",
    [
        # Unknown in_use must NOT be treated as already-clear; a partially
        # populated cache could mask a live PIN, so the clear has to proceed.
        {"code_slot": 3, "name": "Slot 3", "in_use": None, "usercode": None},
        # Missing in_use key is the same uncertainty as None.
        {"code_slot": 3, "name": "Slot 3", "usercode": None},
    ],
    ids=["in_use_none", "in_use_missing"],
)
async def test_uc_delete_credential_proceeds_when_in_use_unknown(
    uc_fallback_lock: ZWaveJSLock,
    mock_uc_utils: dict,
    current_value: dict,
) -> None:
    """Only an explicit in_use=False short-circuits the clear."""
    mock_uc_utils["get_usercode"].side_effect = None
    mock_uc_utils["get_usercode"].return_value = current_value

    ref = CredentialRef(user_id=3, type=CredentialType.PIN, slot=3)
    result = await uc_fallback_lock.async_delete_credential(ref)

    assert result is True
    mock_uc_utils["clear_usercode"].assert_awaited_once_with(uc_fallback_lock.node, 3)


async def test_uc_v1_write_polls_slot_after_set(
    uc_fallback_lock: ZWaveJSLock,
    mock_uc_utils: dict,
) -> None:
    """V1 locks are polled after a write to force-update the value cache."""
    credential = Credential(
        type=CredentialType.PIN, slot=5, state=SlotCredential.known("4321")
    )
    await uc_fallback_lock.async_set_credential(
        user_id=5, credential=credential, pin="4321", name=None, source="sync"
    )

    mock_uc_utils["get_usercode_from_node"].assert_awaited_once_with(
        uc_fallback_lock.node, 5
    )


async def test_uc_v2_write_does_not_poll_slot(
    zwave_js_lock_v2: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
    mock_uc_utils: dict,
) -> None:
    """V2+ locks report back reliably; no verification poll."""
    mock_lock_helpers[
        "async_get_credential_capabilities"
    ].return_value = uc_only_caps_response()
    mock_uc_utils["get_usercodes"].return_value = uc_slot_walk(30)

    credential = Credential(
        type=CredentialType.PIN, slot=5, state=SlotCredential.known("4321")
    )
    await zwave_js_lock_v2.async_set_credential(
        user_id=5, credential=credential, pin="4321", name=None, source="sync"
    )

    mock_uc_utils["set_usercode"].assert_awaited_once()
    mock_uc_utils["get_usercode_from_node"].assert_not_called()


async def test_uc_get_users_synthesizes_from_value_db(
    uc_fallback_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_uc_utils: dict,
) -> None:
    """UC-mode users come from the value DB walk, one per occupied slot."""
    mock_uc_utils["get_usercodes"].return_value = [
        {"code_slot": 1, "name": "Slot 1", "in_use": True, "usercode": "1234"},
        {"code_slot": 2, "name": "Slot 2", "in_use": True, "usercode": "****"},
        {"code_slot": 3, "name": "Slot 3", "in_use": False, "usercode": None},
        {"code_slot": 4, "name": "Slot 4", "in_use": True, "usercode": None},
    ]

    users = await uc_fallback_lock.async_get_users()

    by_id = {user.user_id: user for user in users}
    assert set(by_id) == {1, 2, 4}
    assert by_id[1].credentials[0].slot == 1
    assert by_id[1].credentials[0].state == SlotCredential.known("1234")
    assert by_id[2].credentials[0].state == SlotCredential.unreadable()
    assert by_id[4].credentials[0].state == SlotCredential.unreadable()
    mock_access_control.get_users_cached.assert_not_called()


async def test_uc_hard_refresh_uses_refresh_cc_values(
    uc_fallback_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_uc_utils: dict,
) -> None:
    """UC-mode hard refresh re-reads the User Code CC values, not the unified API."""
    with patch.object(
        type(uc_fallback_lock.node), "async_refresh_cc_values", new=AsyncMock()
    ) as refresh:
        await uc_fallback_lock.async_hard_refresh_codes()

    refresh.assert_awaited_once_with(CommandClass.USER_CODE)
    mock_access_control.get_users.assert_not_called()


async def test_uc_mode_routes_through_slot_only_seam_path(
    hass: HomeAssistant,
    uc_fallback_lock: ZWaveJSLock,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
    mock_uc_utils: dict,
) -> None:
    """End-to-end: a UC-fallback lock skips the user lifecycle on async_set_usercode.

    With ``supports_user_management=False`` from the fallback
    capabilities, the seam's ``_set_credential`` orchestration skips
    ``async_set_user`` and goes straight to ``async_set_credential``,
    which routes through the User Code CC utility.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [uc_fallback_lock.lock.entity_id],
            CONF_SLOTS: {"1": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    uc_fallback_lock._min_operation_delay = 0.0

    await uc_fallback_lock.async_set_usercode(1, "5678", name="alice", source="sync")

    mock_lock_helpers["async_set_user"].assert_not_called()
    mock_uc_utils["set_usercode"].assert_awaited_once_with(
        uc_fallback_lock.node, 1, "5678"
    )


# ---------------------------------------------------------------------------
# User Code CC fallback push handling (issue #1251)
# ---------------------------------------------------------------------------


def _make_uc_value_event(
    node_id: int, property_name: str, code_slot: int, new_value
) -> ZwaveEvent:
    """Create a User Code CC value-updated ZwaveEvent."""
    return ZwaveEvent(
        type="value updated",
        data={
            "source": "node",
            "event": "value updated",
            "nodeId": node_id,
            "args": {
                "commandClass": CommandClass.USER_CODE,
                "property": property_name,
                "propertyKey": code_slot,
                "newValue": new_value,
            },
        },
    )


def _make_duplicate_code_event(node_id: int, user_id: int | None = None) -> ZwaveEvent:
    """Create a duplicate code notification ZwaveEvent."""
    params: dict = {}
    if user_id is not None:
        params["userId"] = user_id
    return ZwaveEvent(
        type="notification",
        data={
            "source": "node",
            "event": "notification",
            "nodeId": node_id,
            "endpointIndex": 0,
            "ccId": 113,
            "args": {
                "type": 6,  # ACCESS_CONTROL
                "event": 15,  # NEW_USER_CODE_NOT_ADDED_DUE_TO_DUPLICATE_CODE
                "label": "Access Control",
                "eventLabel": "New user code not added due to duplicate code",
                "parameters": params,
            },
        },
    )


async def test_subscribe_uc_mode_registers_value_listener_only(
    hass: HomeAssistant,
    uc_fallback_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
) -> None:
    """In UC-fallback mode, push uses one value-updated listener."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await uc_fallback_lock.async_setup_internal(lcm_entry)

    uc_fallback_lock.unsubscribe_push_updates()
    uc_fallback_lock.subscribe_push_updates()
    assert len(uc_fallback_lock._push_unsubs) == 1

    uc_fallback_lock.unsubscribe_push_updates()
    await uc_fallback_lock.async_unload(False)


async def test_subscribe_unknown_mode_registers_both_listener_sets(
    hass: HomeAssistant,
    zwave_js_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    mock_access_control: MagicMock,
    mock_lock_helpers: dict,
) -> None:
    """Before the capability probe runs, both listener sets are registered.

    The handlers are self-filtering and pushes are idempotent, so
    double-coverage is safe; it only happens when push subscription
    races ahead of the first capability probe.
    """
    zwave_js_lock.subscribe_push_updates()
    assert len(zwave_js_lock._push_unsubs) == 4

    zwave_js_lock.unsubscribe_push_updates()


async def test_uc_push_masked_code_sends_unreadable(
    hass: HomeAssistant,
    uc_fallback_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
    mock_uc_utils: dict,
) -> None:
    """A masked userCode value update pushes SlotCredential.unreadable()."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [uc_fallback_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await uc_fallback_lock.async_setup_internal(lcm_entry)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {}
    uc_fallback_lock.coordinator = mock_coordinator

    uc_fallback_lock.unsubscribe_push_updates()
    uc_fallback_lock.subscribe_push_updates()

    lock_schlage_be469.receive_event(
        _make_uc_value_event(lock_schlage_be469.node_id, "userCode", 2, "****")
    )
    await hass.async_block_till_done()

    mock_coordinator.push_update.assert_called_once_with(
        {2: SlotCredential.unreadable()}
    )

    uc_fallback_lock.unsubscribe_push_updates()
    await uc_fallback_lock.async_unload(False)


async def test_uc_push_status_available_ignored_when_pin_expected(
    hass: HomeAssistant,
    uc_fallback_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
    mock_uc_utils: dict,
) -> None:
    """A stale AVAILABLE status is ignored when LCM expects a PIN on the slot.

    Some locks send stale AVAILABLE events after a code was set; acting
    on them would cause infinite sync loops.
    """
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [uc_fallback_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await uc_fallback_lock.async_setup_internal(lcm_entry)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: SlotCredential.known("1234")}
    mock_coordinator.desired_credential.return_value = SlotCredential.known("1234")
    uc_fallback_lock.coordinator = mock_coordinator

    uc_fallback_lock.unsubscribe_push_updates()
    uc_fallback_lock.subscribe_push_updates()

    lock_schlage_be469.receive_event(
        _make_uc_value_event(
            lock_schlage_be469.node_id, "userIdStatus", 2, CodeSlotStatus.AVAILABLE
        )
    )
    await hass.async_block_till_done()

    mock_coordinator.push_update.assert_not_called()

    uc_fallback_lock.unsubscribe_push_updates()
    await uc_fallback_lock.async_unload(False)


async def test_uc_push_status_available_clears_slot_when_no_pin_expected(
    hass: HomeAssistant,
    uc_fallback_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
    mock_uc_utils: dict,
) -> None:
    """An AVAILABLE status pushes empty when LCM does not expect a PIN."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [uc_fallback_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await uc_fallback_lock.async_setup_internal(lcm_entry)

    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: SlotCredential.known("1234")}
    mock_coordinator.desired_credential.return_value = SlotCredential.empty()
    uc_fallback_lock.coordinator = mock_coordinator

    uc_fallback_lock.unsubscribe_push_updates()
    uc_fallback_lock.subscribe_push_updates()

    lock_schlage_be469.receive_event(
        _make_uc_value_event(
            lock_schlage_be469.node_id, "userIdStatus", 2, CodeSlotStatus.AVAILABLE
        )
    )
    await hass.async_block_till_done()

    mock_coordinator.push_update.assert_called_once_with({2: SlotCredential.empty()})

    uc_fallback_lock.unsubscribe_push_updates()
    await uc_fallback_lock.async_unload(False)


async def test_uc_duplicate_code_notification_marks_rejected(
    hass: HomeAssistant,
    uc_fallback_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
    mock_uc_utils: dict,
) -> None:
    """The duplicate-code notification marks the in-flight slot rejected."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await uc_fallback_lock.async_setup_internal(lcm_entry)

    uc_fallback_lock._set_in_progress_code_slot = 2
    lock_schlage_be469.receive_event(
        _make_duplicate_code_event(lock_schlage_be469.node_id, user_id=2)
    )
    await hass.async_block_till_done()

    assert 2 in uc_fallback_lock._rejected_code_slots
    assert uc_fallback_lock._set_in_progress_code_slot is None

    await uc_fallback_lock.async_unload(False)


async def test_uc_duplicate_code_notification_user_id_zero_marks_in_flight_slot(
    hass: HomeAssistant,
    uc_fallback_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
    mock_uc_utils: dict,
) -> None:
    """Firmwares reporting userId=0 still reject the slot LCM is setting."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await uc_fallback_lock.async_setup_internal(lcm_entry)

    uc_fallback_lock._set_in_progress_code_slot = 3
    lock_schlage_be469.receive_event(
        _make_duplicate_code_event(lock_schlage_be469.node_id, user_id=0)
    )
    await hass.async_block_till_done()

    assert 3 in uc_fallback_lock._rejected_code_slots
    assert uc_fallback_lock._set_in_progress_code_slot is None

    await uc_fallback_lock.async_unload(False)


async def test_uc_duplicate_code_notification_ignored_when_not_in_progress(
    hass: HomeAssistant,
    uc_fallback_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
    mock_uc_utils: dict,
) -> None:
    """Without an in-flight set, the notification is not attributed to any slot."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await uc_fallback_lock.async_setup_internal(lcm_entry)

    lock_schlage_be469.receive_event(
        _make_duplicate_code_event(lock_schlage_be469.node_id, user_id=2)
    )
    await hass.async_block_till_done()

    assert not uc_fallback_lock._rejected_code_slots

    await uc_fallback_lock.async_unload(False)


async def test_uc_set_in_progress_cleared_on_value_update(
    hass: HomeAssistant,
    uc_fallback_lock: ZWaveJSLock,
    zwave_integration: MockConfigEntry,
    lock_schlage_be469: Node,
    mock_uc_utils: dict,
) -> None:
    """A userCode update for the in-flight slot closes the rejection window."""
    lcm_entry = MockConfigEntry(domain=DOMAIN, data={CONF_LOCKS: [], CONF_SLOTS: {}})
    lcm_entry.add_to_hass(hass)
    await uc_fallback_lock.async_setup_internal(lcm_entry)

    uc_fallback_lock.unsubscribe_push_updates()
    uc_fallback_lock.subscribe_push_updates()

    uc_fallback_lock._set_in_progress_code_slot = 2
    lock_schlage_be469.receive_event(
        _make_uc_value_event(lock_schlage_be469.node_id, "userCode", 2, "5678")
    )
    await hass.async_block_till_done()

    assert uc_fallback_lock._set_in_progress_code_slot is None

    uc_fallback_lock.unsubscribe_push_updates()
    await uc_fallback_lock.async_unload(False)


class TestUCFallbackLifecycle:
    """Full LCM lifecycle against a lock in User Code CC fallback mode (issue #1251)."""

    async def test_setup_succeeds_and_writes_route_through_uc_utils(
        self,
        hass: HomeAssistant,
        zwave_integration: MockConfigEntry,
        lock_entity: er.RegistryEntry,
        mock_access_control: MagicMock,
        mock_lock_helpers: dict,
        mock_uc_utils: dict,
    ) -> None:
        """A UC-fallback lock completes LCM setup and routes PIN writes via UC utils.

        This pins two regressions at once: setup must not reject the
        slot-only capabilities the fallback reports (the base used to
        require ``supports_user_management``), and writes must use the
        User Code CC utilities instead of the unified API whose broken
        capability data caused 'between 1 and 0' rejections.
        """
        mock_lock_helpers[
            "async_get_credential_capabilities"
        ].return_value = uc_only_caps_response()
        mock_uc_utils["get_usercodes"].return_value = uc_slot_walk(
            30, occupied={2: "1234"}
        )

        lcm_entry = MockConfigEntry(
            domain=DOMAIN,
            data={
                CONF_LOCKS: [lock_entity.entity_id],
                CONF_SLOTS: {"2": {}, "4": {}},
            },
            unique_id="test_zwave_js_uc_e2e",
        )
        lcm_entry.add_to_hass(hass)
        assert await hass.config_entries.async_setup(lcm_entry.entry_id)
        await hass.async_block_till_done()

        try:
            lock = get_zwave_lock(hass, lcm_entry, lock_entity)
            lock._min_operation_delay = 0.0

            # Setting a code skips the user lifecycle and writes via set_usercode
            result = await lock.async_set_usercode(4, "5678", "Test User")
            assert result is WriteResult.CONFIRMED
            mock_lock_helpers["async_set_user"].assert_not_called()
            mock_uc_utils["set_usercode"].assert_awaited_once_with(lock.node, 4, "5678")
            mock_access_control.set_credential.assert_not_called()

            # Clearing resolves the owner from the synthesized UC users and
            # clears via clear_usercode
            result = await lock.async_clear_usercode(2)
            assert result is True
            mock_uc_utils["clear_usercode"].assert_awaited_once_with(lock.node, 2)
            mock_access_control.delete_credential.assert_not_called()
            mock_lock_helpers["async_delete_credential"].assert_not_called()
        finally:
            await hass.config_entries.async_unload(lcm_entry.entry_id)


# ---------------------------------------------------------------------------
# Error mapping and edge cases
# ---------------------------------------------------------------------------


async def test_support_layer_node_stub_raises() -> None:
    """The support layer's node property must be overridden by the provider."""
    with pytest.raises(NotImplementedError):
        ZWaveJSUserCodeFallbackSupport.node.fget(None)


async def test_support_layer_pin_state_stub_raises() -> None:
    """The support layer's _pin_state must be overridden by the provider.

    It exists only so the UC fallback's read path can reuse the concrete
    provider's universal masked/withheld projection.
    """
    with pytest.raises(NotImplementedError):
        ZWaveJSUserCodeFallbackSupport._pin_state(None, "1234")


async def test_usercode_cc_version_defaults_to_v1_when_cc_missing(
    zwave_js_lock: ZWaveJSLock,
) -> None:
    """A node without User Code CC in its command-class list defaults to V1.

    An incomplete interview can leave the CC list empty; V1 is the
    conservative default (it enables the post-write verification poll).
    """
    with patch.object(
        type(zwave_js_lock.node),
        "command_classes",
        new_callable=PropertyMock,
        return_value=[],
    ):
        assert zwave_js_lock._usercode_cc_version == 1


async def test_uc_get_users_transport_error_raises_lock_disconnected(
    uc_fallback_lock: ZWaveJSLock,
    mock_uc_utils: dict,
) -> None:
    """A transport failure during the value DB walk maps to LockDisconnected."""
    # Establish UC mode first (detection also walks the value DB)
    await uc_fallback_lock.async_get_capabilities()
    mock_uc_utils["get_usercodes"].side_effect = FailedZWaveCommand(
        "cmd", 1, "node gone"
    )

    with pytest.raises(LockDisconnected, match="get usercodes failed"):
        await uc_fallback_lock.async_get_users()


async def test_uc_get_users_refreshes_when_managed_slot_unknown(
    hass: HomeAssistant,
    uc_fallback_lock: ZWaveJSLock,
    mock_uc_utils: dict,
) -> None:
    """An unknown managed slot triggers one cache refresh before projecting."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [uc_fallback_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await uc_fallback_lock.async_get_capabilities()

    stale_walk = uc_slot_walk(30)
    stale_walk[1]["in_use"] = None  # slot 2 unknown
    fresh_walk = uc_slot_walk(30, occupied={2: "1234"})
    mock_uc_utils["get_usercodes"].side_effect = [stale_walk, fresh_walk]

    with patch.object(
        type(uc_fallback_lock.node), "async_refresh_cc_values", new=AsyncMock()
    ) as refresh:
        users = await uc_fallback_lock.async_get_users()

    refresh.assert_awaited_once_with(CommandClass.USER_CODE)
    assert [user.user_id for user in users] == [2]
    assert users[0].credentials[0].state == SlotCredential.known("1234")


async def test_uc_get_users_refresh_failure_raises_lock_disconnected(
    hass: HomeAssistant,
    uc_fallback_lock: ZWaveJSLock,
    mock_uc_utils: dict,
) -> None:
    """A failing cache refresh during the retry maps to LockDisconnected."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [uc_fallback_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await uc_fallback_lock.async_get_capabilities()

    stale_walk = uc_slot_walk(30)
    stale_walk[1]["in_use"] = None
    mock_uc_utils["get_usercodes"].side_effect = [stale_walk]

    with (
        patch.object(
            type(uc_fallback_lock.node),
            "async_refresh_cc_values",
            new=AsyncMock(side_effect=FailedZWaveCommand("cmd", 1, "node gone")),
        ),
        pytest.raises(LockDisconnected, match="usercode cache refresh failed"),
    ):
        await uc_fallback_lock.async_get_users()


async def test_uc_get_users_second_walk_failure_raises_lock_disconnected(
    hass: HomeAssistant,
    uc_fallback_lock: ZWaveJSLock,
    mock_uc_utils: dict,
) -> None:
    """A transport failure on the post-refresh walk maps to LockDisconnected."""
    lcm_entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            CONF_LOCKS: [uc_fallback_lock.lock.entity_id],
            CONF_SLOTS: {"2": {}},
        },
    )
    lcm_entry.add_to_hass(hass)
    await uc_fallback_lock.async_get_capabilities()

    stale_walk = uc_slot_walk(30)
    stale_walk[1]["in_use"] = None
    mock_uc_utils["get_usercodes"].side_effect = [
        stale_walk,
        FailedZWaveCommand("cmd", 1, "node gone"),
    ]

    with (
        patch.object(
            type(uc_fallback_lock.node), "async_refresh_cc_values", new=AsyncMock()
        ),
        pytest.raises(LockDisconnected, match="get usercodes failed"),
    ):
        await uc_fallback_lock.async_get_users()


async def test_uc_clear_credential_missing_slot_raises_operation_failed(
    uc_fallback_lock: ZWaveJSLock,
    mock_uc_utils: dict,
) -> None:
    """Clearing a slot with no value in the DB maps to LockOperationFailed."""
    mock_uc_utils["clear_usercode"].side_effect = NotFoundError("no such slot")

    ref = CredentialRef(user_id=99, type=CredentialType.PIN, slot=99)
    with pytest.raises(LockOperationFailed, match="clear usercode slot 99"):
        await uc_fallback_lock.async_delete_credential(ref)


async def test_uc_clear_credential_transport_error_raises_lock_disconnected(
    uc_fallback_lock: ZWaveJSLock,
    mock_uc_utils: dict,
) -> None:
    """A Z-Wave transport failure during clear maps to LockDisconnected."""
    mock_uc_utils["clear_usercode"].side_effect = FailedZWaveCommand(
        "cmd", 1, "node gone"
    )

    ref = CredentialRef(user_id=3, type=CredentialType.PIN, slot=3)
    with pytest.raises(LockDisconnected, match="clear usercode slot 3"):
        await uc_fallback_lock.async_delete_credential(ref)


async def test_uc_clear_fail_status_logs_and_continues(
    uc_fallback_lock: ZWaveJSLock,
    mock_uc_utils: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A transient ``FAIL`` clear status is tolerated; mirrors the set path."""
    mock_coordinator = MagicMock()
    mock_coordinator.data = {}
    uc_fallback_lock.coordinator = mock_coordinator

    mock_uc_utils["clear_usercode"].return_value = SetValueResult(
        {"status": SetValueStatus.FAIL}
    )

    ref = CredentialRef(user_id=3, type=CredentialType.PIN, slot=3)
    with caplog.at_level(logging.INFO):
        result = await uc_fallback_lock.async_delete_credential(ref)

    assert result is True
    mock_coordinator.push_update.assert_called_once_with({3: SlotCredential.empty()})
    assert "clear returned FAIL" in caplog.text


@pytest.mark.parametrize(
    "status",
    [
        SetValueStatus.NO_DEVICE_SUPPORT,
        SetValueStatus.ENDPOINT_NOT_FOUND,
        SetValueStatus.NOT_IMPLEMENTED,
        SetValueStatus.INVALID_VALUE,
    ],
    ids=lambda s: s.name,
)
async def test_uc_clear_fatal_status_raises_operation_failed(
    uc_fallback_lock: ZWaveJSLock,
    mock_uc_utils: dict,
    status: SetValueStatus,
) -> None:
    """Structural-impossibility clear statuses still raise ``LockOperationFailed``."""
    mock_uc_utils["clear_usercode"].return_value = SetValueResult({"status": status})

    ref = CredentialRef(user_id=3, type=CredentialType.PIN, slot=3)
    with pytest.raises(LockOperationFailed, match=status.name):
        await uc_fallback_lock.async_delete_credential(ref)


async def test_uc_v1_set_verify_failure_is_non_fatal(
    uc_fallback_lock: ZWaveJSLock,
    mock_uc_utils: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """V1 verify failure during set is logged at INFO and does not fail the set.

    The wire-level SET already ack'd and the optimistic
    ``_push_credential_update`` delivers the truth to the coordinator.
    Failing here turns a flaky-interview lock (issue #1251) into a
    slot-suspension feedback loop -- the exact pathology the fallback
    is meant to dodge. The on-demand refresh on missing/unknown slots,
    plus the next sync tick, reconcile any cache drift that genuinely
    matters.
    """
    mock_coordinator = MagicMock()
    mock_coordinator.data = {}
    uc_fallback_lock.coordinator = mock_coordinator

    mock_uc_utils["get_usercode_from_node"].side_effect = FailedZWaveCommand(
        "cmd", 1, "node gone"
    )

    credential = Credential(
        type=CredentialType.PIN, slot=5, state=SlotCredential.known("4321")
    )
    with caplog.at_level(logging.INFO):
        result = await uc_fallback_lock.async_set_credential(
            user_id=5, credential=credential, pin="4321", name=None, source="sync"
        )

    assert result is WriteResult.CONFIRMED
    mock_coordinator.push_update.assert_called_once_with(
        {5: SlotCredential.known("4321")}
    )
    assert "verification poll failed" in caplog.text


async def test_uc_v1_clear_verify_failure_is_non_fatal(
    uc_fallback_lock: ZWaveJSLock,
    mock_uc_utils: dict,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """V1 verify failure during clear is logged at INFO and does not fail the clear."""
    mock_coordinator = MagicMock()
    mock_coordinator.data = {}
    uc_fallback_lock.coordinator = mock_coordinator

    mock_uc_utils["get_usercode_from_node"].side_effect = FailedZWaveCommand(
        "cmd", 1, "node gone"
    )

    ref = CredentialRef(user_id=3, type=CredentialType.PIN, slot=3)
    with caplog.at_level(logging.INFO):
        result = await uc_fallback_lock.async_delete_credential(ref)

    assert result is True
    mock_coordinator.push_update.assert_called_once_with({3: SlotCredential.empty()})
    assert "verification poll failed" in caplog.text


async def test_uc_v1_verify_non_zwave_error_propagates(
    uc_fallback_lock: ZWaveJSLock,
    mock_uc_utils: dict,
) -> None:
    """Non-Z-Wave verify errors still propagate as bugs.

    The softening only covers ``BaseZwaveJSServerError``; a RuntimeError
    here means a programming bug and must surface for diagnosis rather
    than be silently swallowed alongside the wire-level timeouts.
    """
    mock_uc_utils["get_usercode_from_node"].side_effect = RuntimeError("bug")

    credential = Credential(
        type=CredentialType.PIN, slot=5, state=SlotCredential.known("4321")
    )
    with pytest.raises(RuntimeError, match="bug"):
        await uc_fallback_lock.async_set_credential(
            user_id=5, credential=credential, pin="4321", name=None, source="sync"
        )


async def test_uc_value_updated_ignores_unrelated_events(
    uc_fallback_lock: ZWaveJSLock,
    mock_uc_utils: dict,
) -> None:
    """Other command classes, properties, and slot 0 are filtered out."""
    mock_coordinator = MagicMock()
    mock_coordinator.data = {}
    uc_fallback_lock.coordinator = mock_coordinator

    for args in (
        # Different command class
        {
            "commandClass": CommandClass.DOOR_LOCK,
            "property": "userCode",
            "propertyKey": 2,
            "newValue": "1234",
        },
        # Unrelated User Code CC property
        {
            "commandClass": CommandClass.USER_CODE,
            "property": "keypadMode",
            "propertyKey": 2,
            "newValue": 1,
        },
        # Slot 0 is not a valid user code slot
        {
            "commandClass": CommandClass.USER_CODE,
            "property": "userCode",
            "propertyKey": 0,
            "newValue": "1234",
        },
    ):
        uc_fallback_lock._on_uc_value_updated({"args": args})

    mock_coordinator.push_update.assert_not_called()


async def test_uc_value_update_falsy_value_pushes_empty(
    uc_fallback_lock: ZWaveJSLock,
    mock_uc_utils: dict,
) -> None:
    """A falsy userCode value resolves to an empty slot."""
    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: SlotCredential.known("1234")}
    uc_fallback_lock.coordinator = mock_coordinator

    uc_fallback_lock._on_uc_value_updated(
        {
            "args": {
                "commandClass": CommandClass.USER_CODE,
                "property": "userCode",
                "propertyKey": 2,
                "newValue": None,
            }
        }
    )

    mock_coordinator.push_update.assert_called_once_with({2: SlotCredential.empty()})


async def test_uc_value_update_all_zeros_not_in_use_pushes_empty(
    uc_fallback_lock: ZWaveJSLock,
    mock_uc_utils: dict,
) -> None:
    """All-zeros with in_use explicitly False resolves to an empty slot."""
    mock_uc_utils["get_usercode"].side_effect = None
    mock_uc_utils["get_usercode"].return_value = {
        "code_slot": 2,
        "name": "Slot 2",
        "in_use": False,
        "usercode": "0000",
    }
    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: SlotCredential.known("1234")}
    uc_fallback_lock.coordinator = mock_coordinator

    uc_fallback_lock._on_uc_value_updated(
        {
            "args": {
                "commandClass": CommandClass.USER_CODE,
                "property": "userCode",
                "propertyKey": 2,
                "newValue": "0000",
            }
        }
    )

    mock_coordinator.push_update.assert_called_once_with({2: SlotCredential.empty()})


async def test_uc_value_update_duplicate_value_skipped(
    uc_fallback_lock: ZWaveJSLock,
    mock_uc_utils: dict,
) -> None:
    """Z-Wave JS sends duplicate events; unchanged values are not re-pushed."""
    mock_coordinator = MagicMock()
    mock_coordinator.data = {2: SlotCredential.known("1234")}
    uc_fallback_lock.coordinator = mock_coordinator

    uc_fallback_lock._on_uc_value_updated(
        {
            "args": {
                "commandClass": CommandClass.USER_CODE,
                "property": "userCode",
                "propertyKey": 2,
                "newValue": "1234",
            }
        }
    )

    mock_coordinator.push_update.assert_not_called()


@pytest.mark.parametrize(
    ("in_use", "usercode", "expected"),
    [
        # Explicit in_use=False is the only "definitely empty" signal.
        (False, None, SlotCredential.empty()),
        (False, "", SlotCredential.empty()),
        # Unknown in_use with no cached value matches the legacy 3.x reader.
        (None, None, SlotCredential.empty()),
        # Masked codes are unreadable regardless of in_use -- mirrors the
        # push-path rule (masked + in_use!=False -> unreadable).
        (True, "****", SlotCredential.unreadable()),
        (None, "****", SlotCredential.unreadable()),
        # Occupied slot without a cached value is unreadable, not empty.
        (True, None, SlotCredential.unreadable()),
        # Real codes pass through; unknown in_use must not erase a present PIN.
        (True, "1234", SlotCredential.known("1234")),
        (None, "1234", SlotCredential.known("1234")),
    ],
    ids=[
        "false_no_code_empty",
        "false_blank_code_empty",
        "none_no_code_empty",
        "true_masked_unreadable",
        "none_masked_unreadable",
        "true_no_cached_value_unreadable",
        "true_known_code",
        "none_with_code_known",
    ],
)
def test_uc_slot_state_projection(
    zwave_js_lock: ZWaveJSLock,
    in_use: bool | None,
    usercode: str | None,
    expected: SlotCredential,
) -> None:
    """_uc_slot_state must distinguish None (unknown) from False (cleared)."""
    assert zwave_js_lock._uc_slot_state(in_use, usercode) == expected
