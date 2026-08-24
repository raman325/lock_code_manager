"""Tests for the zwave-js-ui credential operations built on the api client."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.domain.credentials import (
    CredentialRef,
    CredentialType,
    User,
    WriteResult,
    credential_from_slot,
)
from custom_components.lock_code_manager.domain.exceptions import (
    LockDisconnected,
    LockOperationFailed,
)
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers.zwave_js_ui import ZWaveJSUILock

from .conftest import ZUI_API_BASE, ZUI_NODE_ID, ZWaveJSUIApiResponder

MODULE = "custom_components.lock_code_manager.providers.zwave_js_ui"
# The operational preamble every operation runs first lives on BaseMqttLock,
# so the MQTT-enabled check it makes is bound in that module, not this
# provider's -- which still makes its own for the subscription paths.
MQTT_BASE = "custom_components.lock_code_manager.providers._mqtt"
MANAGED_SLOTS = "custom_components.lock_code_manager.providers._base.get_managed_slots"
# User Code Command Class (99), endpoint 0 -- the ``sendCommand`` target every
# credential operation addresses.
CC_USER_CODE_ID = 99
NODE_TARGET = {"nodeId": ZUI_NODE_ID, "commandClass": CC_USER_CODE_ID, "endpoint": 0}


def _user_code_handler(
    slot_results: dict[int, Any],
) -> Callable[[str, dict[str, Any]], dict[str, Any] | None]:
    """Answer a User Code CC ``get`` with whatever the named slot holds."""

    def _handler(_api_base: str, request: dict[str, Any]) -> dict[str, Any] | None:
        _target, _method, method_args = request["args"]
        return {"success": True, "result": slot_results[method_args[0]]}

    return _handler


def _answering_handler() -> Callable[[str, dict[str, Any]], dict[str, Any] | None]:
    """
    Answer every slot with a real enabled code, whichever slot is asked about.

    What the code is does not matter to the tests that use this; that the
    transport answered at all does, because that is what proves the lock can
    be read and arms the all-silent rule.
    """

    def _handler(_api_base: str, _request: dict[str, Any]) -> dict[str, Any]:
        return {"success": True, "result": {"userIdStatus": 1, "userCode": "1234"}}

    return _handler


def _slot_state(users: list[User], slot: int) -> SlotCredential:
    """Pull one slot's projected Personal Identification Number state."""
    return next(user for user in users if user.user_id == slot).pin_credentials[0].state


def _set_credential(lock: ZWaveJSUILock, slot: int = 1) -> Awaitable[WriteResult]:
    """Invoke ``async_set_credential`` with the shape the seam calls it with."""
    return lock.async_set_credential(
        slot,
        credential_from_slot(slot, SlotCredential.known("1234")),
        "1234",
        name=None,
        source="direct",
    )


def _delete_credential(lock: ZWaveJSUILock, slot: int = 1) -> Awaitable[bool]:
    """Invoke ``async_delete_credential`` for one slot."""
    return lock.async_delete_credential(
        CredentialRef(user_id=slot, type=CredentialType.PIN, slot=slot)
    )


def test_poll_intervals_belong_to_this_provider() -> None:
    """Every slot costs a round trip, so this lock is polled slowly."""
    lock = ZWaveJSUILock.__new__(ZWaveJSUILock)
    assert lock.usercode_scan_interval == timedelta(minutes=5)
    assert lock.hard_refresh_interval == timedelta(hours=1)


class TestAsyncGetUsers:
    """Per-slot reads over the api client."""

    async def test_slots_are_read_one_at_a_time_in_order(
        self,
        hass: HomeAssistant,
        zui_gateway_resolved: ZWaveJSUILock,
        zui_api_responder: ZWaveJSUIApiResponder,
    ) -> None:
        """
        Each managed slot is asked for on its own, lowest first.

        The gateway and the lock's firmware answer one GET at a time, so the
        order and the one-request-per-slot shape are the contract -- a
        parallel read would interleave requests this assertion would catch.

        ``{8, 1}`` iterates as ``8, 1``, so the recorded order is the sort's
        doing and not the set's.
        """
        lock = zui_gateway_resolved
        zui_api_responder.set_handler(
            "sendCommand",
            _user_code_handler(
                {
                    1: {"userIdStatus": 1, "userCode": "1234"},
                    8: {"userIdStatus": 0},
                }
            ),
        )

        with patch(MANAGED_SLOTS, return_value={8, 1}):
            users = await lock.async_get_users()

        assert _slot_state(users, 1) == SlotCredential.known("1234")
        assert _slot_state(users, 8) is SlotCredential.empty()
        assert zui_api_responder.requests == [
            (ZUI_API_BASE, "sendCommand", [NODE_TARGET, "get", [1]]),
            (ZUI_API_BASE, "sendCommand", [NODE_TARGET, "get", [8]]),
        ]

    async def test_a_named_scope_reads_only_those_slots(
        self,
        hass: HomeAssistant,
        zui_gateway_resolved: ZWaveJSUILock,
        zui_api_responder: ZWaveJSUIApiResponder,
    ) -> None:
        """
        A caller's scope replaces the managed set rather than widening it.

        Reading past the scope is not wrong, only slow, which is exactly why
        nothing else would catch it: allocation widens a few numbers at a
        time so the lock is asked about a handful of indices, not its range.
        """
        lock = zui_gateway_resolved
        zui_api_responder.set_handler(
            "sendCommand", _user_code_handler({3: {"userIdStatus": 0}})
        )

        with patch(MANAGED_SLOTS, return_value={1, 2, 3, 11}):
            users = await lock.async_get_users(slots={3})

        assert [user.user_id for user in users] == [3]
        assert [args[2] for _base, _name, args in zui_api_responder.requests] == [[3]]

    async def test_an_empty_scope_asks_nothing(
        self,
        hass: HomeAssistant,
        zui_gateway_resolved: ZWaveJSUILock,
        zui_api_responder: ZWaveJSUIApiResponder,
    ) -> None:
        """Nothing to read means no api traffic at all."""
        lock = zui_gateway_resolved

        with patch(MANAGED_SLOTS, return_value=set()):
            assert await lock.async_get_users() == []
        assert await lock.async_get_users(slots=[]) == []

        assert zui_api_responder.requests == []

    async def test_a_refused_read_is_unreadable_not_empty(
        self,
        hass: HomeAssistant,
        zui_gateway_resolved: ZWaveJSUILock,
        zui_api_responder: ZWaveJSUIApiResponder,
    ) -> None:
        """
        An api-level refusal says nothing about the slot, so it stays unreadable.

        Calling it empty would tell sync the code is gone and allocation the
        index is free -- one refused read then reprograms a slot that already
        holds the right code.
        """
        lock = zui_gateway_resolved

        def _handler(_api_base: str, request: dict[str, Any]) -> dict[str, Any]:
            slot = request["args"][2][0]
            if slot == 2:
                return {"success": False, "message": "Command failed", "result": None}
            return {"success": True, "result": {"userIdStatus": 1, "userCode": "1234"}}

        zui_api_responder.set_handler("sendCommand", _handler)

        with patch(MANAGED_SLOTS, return_value={1, 2, 3}):
            users = await lock.async_get_users()

        assert _slot_state(users, 2) is SlotCredential.unreadable()
        assert _slot_state(users, 1) == SlotCredential.known("1234")
        assert _slot_state(users, 3) == SlotCredential.known("1234")

    async def test_every_slot_refused_disconnects_rather_than_reporting_data(
        self,
        hass: HomeAssistant,
        zui_gateway_resolved: ZWaveJSUILock,
        zui_api_responder: ZWaveJSUIApiResponder,
    ) -> None:
        """
        A read where nothing was learned is not a successful poll.

        Returning all-unreadable looks like data to the coordinator: it
        resets the connectivity breaker, un-suspends every slot, and lets
        them re-fail on the next tick, oscillating on a seconds-scale loop
        that flips every slot in and out of sync. Raising leaves the lock
        unreachable so its backoff governs the next attempt.
        """
        lock = zui_gateway_resolved
        zui_api_responder.set_handler("sendCommand", _answering_handler())
        with patch(MANAGED_SLOTS, return_value={1, 2}):
            await lock.async_get_users()

        zui_api_responder.set_result(
            "sendCommand", None, success=False, message="Node 20 is not alive"
        )
        with patch(MANAGED_SLOTS, return_value={1, 2}):
            await lock.async_get_users()
        with (
            patch(MANAGED_SLOTS, return_value={1, 2}),
            pytest.raises(LockDisconnected, match="every one of the 2"),
        ):
            await lock.async_get_users()

    async def test_a_transport_that_never_answered_a_read_never_disconnects(
        self,
        hass: HomeAssistant,
        zui_gateway_resolved: ZWaveJSUILock,
        zui_api_responder: ZWaveJSUIApiResponder,
    ) -> None:
        """
        Silence only means "gone" once this transport has proven it can speak.

        A bridge that accepts writes but answers no reads at all -- the
        write-only Zigbee2MQTT converter, a gateway whose node has User Code
        Set but not Get -- worked before this rule existed: every poll came
        back all-unreadable and every write landed. Raising on it instead
        trips the breaker, suspends the slots, and takes the writes down with
        the reads that were never going to work.
        """
        lock = zui_gateway_resolved
        zui_api_responder.set_result(
            "sendCommand", None, success=False, message="Command not supported"
        )

        for _ in range(4):
            with patch(MANAGED_SLOTS, return_value={1, 2}):
                users = await lock.async_get_users()
            assert _slot_state(users, 1) is SlotCredential.unreadable()
            assert _slot_state(users, 2) is SlotCredential.unreadable()

    async def test_one_silent_poll_after_a_good_one_is_absorbed(
        self,
        hass: HomeAssistant,
        zui_gateway_resolved: ZWaveJSUILock,
        zui_api_responder: ZWaveJSUIApiResponder,
    ) -> None:
        """
        A lossy mesh drops correlated bursts, and two slots is a small sample.

        Issue #1397 had a node timing out roughly half its responses, which
        loses both replies of a two-slot poll about a quarter of the time.
        Tripping the breaker there suspends a lock that is merely slow, so
        one silent poll is absorbed and only a second consecutive one is
        treated as an outage.
        """
        lock = zui_gateway_resolved
        zui_api_responder.set_handler("sendCommand", _answering_handler())
        with patch(MANAGED_SLOTS, return_value={1, 2}):
            await lock.async_get_users()

        zui_api_responder.set_result(
            "sendCommand", None, success=False, message="Node 20 is not alive"
        )
        with patch(MANAGED_SLOTS, return_value={1, 2}):
            users = await lock.async_get_users()

        assert _slot_state(users, 1) is SlotCredential.unreadable()

    async def test_an_answered_poll_between_two_silent_ones_resets_the_count(
        self,
        hass: HomeAssistant,
        zui_gateway_resolved: ZWaveJSUILock,
        zui_api_responder: ZWaveJSUIApiResponder,
    ) -> None:
        """
        Only consecutive silence is an outage; alternating is a weak link.

        Without the reset a lock that answers every other poll accumulates
        silences forever and is eventually declared gone while it is plainly
        still talking.
        """
        lock = zui_gateway_resolved
        answering = _answering_handler()

        for _ in range(3):
            zui_api_responder.set_handler("sendCommand", answering)
            with patch(MANAGED_SLOTS, return_value={1, 2}):
                await lock.async_get_users()
            zui_api_responder.set_result(
                "sendCommand", None, success=False, message="Node 20 is not alive"
            )
            with patch(MANAGED_SLOTS, return_value={1, 2}):
                users = await lock.async_get_users()
            assert _slot_state(users, 1) is SlotCredential.unreadable()

    async def test_a_lone_slot_losing_its_reply_is_not_a_dead_transport(
        self,
        hass: HomeAssistant,
        zui_gateway_resolved: ZWaveJSUILock,
        zui_api_responder: ZWaveJSUIApiResponder,
    ) -> None:
        """
        One slot asked about and one reply lost is noise, not an outage.

        The all-silent rule needs at least two reads to say anything: with a
        single managed slot it fires on the first routine drop, so an entry
        with one user would have its breaker tripped by a mesh an entry with
        two users rides out untroubled.
        """
        lock = zui_gateway_resolved
        zui_api_responder.set_result(
            "sendCommand", None, success=False, message="Node 20 is not alive"
        )

        with patch(MANAGED_SLOTS, return_value={1}):
            users = await lock.async_get_users()

        assert _slot_state(users, 1) is SlotCredential.unreadable()

    async def test_one_refusal_beside_one_real_read_is_still_data(
        self,
        hass: HomeAssistant,
        zui_gateway_resolved: ZWaveJSUILock,
        zui_api_responder: ZWaveJSUIApiResponder,
    ) -> None:
        """
        A slot the lock described, however uselessly, keeps the poll alive.

        Both slots reach the coordinator unreadable here, so the return is
        indistinguishable from the all-refused case above -- the difference
        is entirely in what the lock did, and reading a withheld code as a
        refusal would disconnect a lock that is answering every request.
        """
        lock = zui_gateway_resolved

        def _handler(_api_base: str, request: dict[str, Any]) -> dict[str, Any]:
            if request["args"][2][0] == 1:
                return {"success": False, "message": "Command failed", "result": None}
            return {
                "success": True,
                "result": {"userIdStatus": 1, "userCode": "****"},
            }

        zui_api_responder.set_handler("sendCommand", _handler)

        with patch(MANAGED_SLOTS, return_value={1, 2}):
            users = await lock.async_get_users()

        assert _slot_state(users, 1) is SlotCredential.unreadable()
        assert _slot_state(users, 2) is SlotCredential.unreadable()

    async def test_every_slot_masked_is_a_healthy_poll(
        self,
        hass: HomeAssistant,
        zui_gateway_resolved: ZWaveJSUILock,
        zui_api_responder: ZWaveJSUIApiResponder,
    ) -> None:
        """
        A lock that withholds every code is answering, not unreachable.

        This is an ordinary configuration, not a fault, so the poll has to
        succeed -- disconnecting here would make such a lock permanently
        offline no matter how healthy the mesh is.
        """
        lock = zui_gateway_resolved
        zui_api_responder.set_result(
            "sendCommand", {"userIdStatus": 1, "userCode": "****"}
        )

        with patch(MANAGED_SLOTS, return_value={1, 2}):
            users = await lock.async_get_users()

        assert [user.user_id for user in users] == [1, 2]
        assert all(
            _slot_state(users, slot) is SlotCredential.unreadable() for slot in (1, 2)
        )

    async def test_a_silent_gateway_disconnects_rather_than_degrading(
        self,
        hass: HomeAssistant,
        zui_gateway_resolved: ZWaveJSUILock,
        zui_api_responder: ZWaveJSUIApiResponder,
    ) -> None:
        """
        A gateway that answers nothing is a lost connection, not a bad slot.

        Degrading it to unreadable would leave the poll reporting slot after
        slot with no answer while the reconnect path never runs.
        """
        lock = zui_gateway_resolved
        zui_api_responder.set_handler("sendCommand", lambda _base, _request: None)

        with (
            patch(f"{MODULE}.API_CALL_TIMEOUT", 0.01),
            patch(MANAGED_SLOTS, return_value={1, 2}),
            pytest.raises(LockDisconnected),
        ):
            await lock.async_get_users()


class TestAsyncSetCredential:
    """Writes over the User Code CC ``set`` method."""

    async def test_set_writes_the_enabled_envelope_and_confirms(
        self,
        hass: HomeAssistant,
        zui_gateway_resolved: ZWaveJSUILock,
        zui_api_responder: ZWaveJSUIApiResponder,
    ) -> None:
        """The wire shape is ``set(slot, Enabled, pin)`` and success is confirmed."""
        lock = zui_gateway_resolved
        lock.coordinator = MagicMock()
        zui_api_responder.set_result("sendCommand", None)

        assert await _set_credential(lock, 4) is WriteResult.CONFIRMED

        assert zui_api_responder.requests == [
            (ZUI_API_BASE, "sendCommand", [NODE_TARGET, "set", [4, 1, "1234"]])
        ]
        lock.coordinator.push_update.assert_called_once_with(
            {4: SlotCredential.known("1234")}
        )

    async def test_a_refused_write_fails_and_pushes_nothing(
        self,
        hass: HomeAssistant,
        zui_gateway_resolved: ZWaveJSUILock,
        zui_api_responder: ZWaveJSUIApiResponder,
    ) -> None:
        """A refusal must not leave the coordinator believing the write landed."""
        lock = zui_gateway_resolved
        lock.coordinator = MagicMock()
        zui_api_responder.set_result("sendCommand", None, success=False, message="nope")

        with pytest.raises(LockOperationFailed):
            await _set_credential(lock)

        lock.coordinator.push_update.assert_not_called()

    async def test_a_silent_write_disconnects_and_pushes_nothing(
        self,
        hass: HomeAssistant,
        zui_gateway_resolved: ZWaveJSUILock,
        zui_api_responder: ZWaveJSUIApiResponder,
    ) -> None:
        """An unanswered write is a lost connection, and it too pushes nothing."""
        lock = zui_gateway_resolved
        lock.coordinator = MagicMock()
        zui_api_responder.set_handler("sendCommand", lambda _base, _request: None)

        with (
            patch(f"{MODULE}.API_CALL_TIMEOUT", 0.01),
            pytest.raises(LockDisconnected),
        ):
            await _set_credential(lock)

        lock.coordinator.push_update.assert_not_called()


class TestAsyncDeleteCredential:
    """Clears over the User Code CC ``clear`` method."""

    async def test_delete_clears_the_slot_and_pushes_empty(
        self,
        hass: HomeAssistant,
        zui_gateway_resolved: ZWaveJSUILock,
        zui_api_responder: ZWaveJSUIApiResponder,
    ) -> None:
        """The wire shape is ``clear(slot)`` and the slot is pushed empty."""
        lock = zui_gateway_resolved
        lock.coordinator = MagicMock()
        zui_api_responder.set_result("sendCommand", None)

        assert await _delete_credential(lock, 7) is True

        assert zui_api_responder.requests == [
            (ZUI_API_BASE, "sendCommand", [NODE_TARGET, "clear", [7]])
        ]
        lock.coordinator.push_update.assert_called_once_with(
            {7: SlotCredential.empty()}
        )

    async def test_a_refused_clear_fails_and_pushes_nothing(
        self,
        hass: HomeAssistant,
        zui_gateway_resolved: ZWaveJSUILock,
        zui_api_responder: ZWaveJSUIApiResponder,
    ) -> None:
        """A refused clear must not report the slot empty."""
        lock = zui_gateway_resolved
        lock.coordinator = MagicMock()
        zui_api_responder.set_result("sendCommand", None, success=False, message="nope")

        with pytest.raises(LockOperationFailed):
            await _delete_credential(lock)

        lock.coordinator.push_update.assert_not_called()


class TestOperationalGuards:
    """Every public operation refuses to run against an unusable transport."""

    @pytest.mark.parametrize(
        "operation",
        [
            pytest.param(lambda lock: lock.async_get_users(), id="get_users"),
            pytest.param(_set_credential, id="set_credential"),
            pytest.param(_delete_credential, id="delete_credential"),
            pytest.param(lambda lock: lock.async_get_max_slot(), id="get_max_slot"),
        ],
    )
    @pytest.mark.parametrize(
        ("condition", "match"),
        [
            ("mqtt_disabled", "MQTT component not available"),
            ("no_gateway_binding", "Lock not connected"),
            ("entity_unavailable", "Device not available"),
        ],
    )
    async def test_each_guard_disconnects_every_operation(
        self,
        hass: HomeAssistant,
        zui_gateway_resolved: ZWaveJSUILock,
        zui_api_responder: ZWaveJSUIApiResponder,
        operation: Callable[[ZWaveJSUILock], Awaitable[Any]],
        condition: str,
        match: str,
    ) -> None:
        """
        MQTT off, no gateway to address, and a dead entity all disconnect.

        Every operation that touches the api shares one preamble, so the
        cross product is what proves a new one cannot quietly skip it --
        including the capability probe, whose answer is api traffic like any
        other and which would otherwise report a disabled MQTT integration as
        a call timeout.
        """
        lock = zui_gateway_resolved
        guards = {
            "mqtt_disabled": patch(
                f"{MQTT_BASE}.mqtt_config_entry_enabled", return_value=False
            ),
            # No discovery payload at all, which is the only way left to have
            # no gateway: a missing node topic alone still leaves the
            # availability entry to bind from, and such a lock works api-only.
            "no_gateway_binding": patch(
                f"{MODULE}.resolve_discovery_payload", return_value=None
            ),
            "entity_unavailable": patch.object(
                ZWaveJSUILock,
                "async_is_device_available",
                AsyncMock(return_value=False),
            ),
        }

        with guards[condition], pytest.raises(LockDisconnected, match=match):
            await operation(lock)

        assert zui_api_responder.requests == []


class TestAsyncGetMaxSlot:
    """The lock's advertised User Code capacity, or no opinion."""

    @pytest.mark.parametrize(
        ("result", "expected"),
        [
            (30, 30),
            (0, None),
            (-1, None),
            # ``True == 1`` in Python, so an unguarded check would report a
            # one-slot lock and strand allocation on index 1.
            (True, None),
            ("30", None),
            (None, None),
        ],
    )
    async def test_capacity_is_taken_only_from_a_positive_integer(
        self,
        hass: HomeAssistant,
        zui_gateway_resolved: ZWaveJSUILock,
        zui_api_responder: ZWaveJSUIApiResponder,
        result: Any,
        expected: int | None,
    ) -> None:
        """Anything that is not a positive integer is no opinion at all."""
        zui_api_responder.set_result("sendCommand", result)

        assert await zui_gateway_resolved.async_get_max_slot() == expected
        assert zui_api_responder.requests == [
            (ZUI_API_BASE, "sendCommand", [NODE_TARGET, "getUsersCount", []])
        ]


async def test_hard_refresh_reads_the_same_codes_as_a_poll(
    hass: HomeAssistant,
    zui_gateway_resolved: ZWaveJSUILock,
    zui_api_responder: ZWaveJSUIApiResponder,
) -> None:
    """Hard refresh is the same read; nothing here is cached to invalidate."""
    lock = zui_gateway_resolved
    zui_api_responder.set_handler(
        "sendCommand", _user_code_handler({5: {"userIdStatus": 1, "userCode": "9876"}})
    )

    with patch(MANAGED_SLOTS, return_value={5}):
        refreshed = await lock.async_hard_refresh_codes()
        polled = await lock.async_get_usercodes()

    assert refreshed == polled == {5: SlotCredential.known("9876")}
