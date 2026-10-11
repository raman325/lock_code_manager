"""End-to-end tests for the ZHA provider through a real Lock Code Manager entry."""

from __future__ import annotations

from typing import Any

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from zigpy.zcl.clusters.closures import DoorLock
from zigpy.zcl.foundation import Status

from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_ENABLED,
    SERVICE_TURN_OFF,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from custom_components.lock_code_manager.const import (
    DOMAIN,
    MAX_SYNC_ATTEMPTS,
    TICK_INTERVAL,
)
from custom_components.lock_code_manager.domain.credentials import pin_address
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers.zha import ZHALock
from tests.common import in_sync_entity_id, slot_entity_id
from tests.conftest import async_advance_time

from .conftest import ZHA_LCM_CONFIG_SLOTS, Answer, DoorLockStandIn
from .helpers import (
    Frame,
    QuirkedDoorLock,
    RenamingQuirkDoorLock,
    default_response,
    lock_frame,
)

CONFIGURED_PINS = {slot: config["pin"] for slot, config in ZHA_LCM_CONFIG_SLOTS.items()}
# A user on the lock that no Lock Code Manager entry manages.
UNMANAGED_USER = 9
_SET_PIN_CODE_ID = DoorLock.ServerCommandDefs.set_pin_code.id
_CLEAR_PIN_CODE_ID = DoorLock.ServerCommandDefs.clear_pin_code.id
_LOCK_DOOR_ID = DoorLock.ServerCommandDefs.lock_door.id
# Set and Clear PIN Code Response general failure (Zigbee Cluster Library).
_DOOR_LOCK_GENERAL_FAILURE = 1

# The stock Door Lock cluster, and quirks that redefine its replies under the
# same name or another: a reply is identified as its own cluster decodes it.
_CLUSTER_TYPES = pytest.mark.parametrize(
    "door_lock_cluster_type",
    [DoorLock, QuirkedDoorLock, RenamingQuirkDoorLock],
    ids=["stock", "quirk", "renaming_quirk"],
)


def _zha_lock(lcm_config_entry: MockConfigEntry) -> ZHALock:
    """Return the provider the loaded Lock Code Manager entry built for the lock."""
    (lock,) = lcm_config_entry.runtime_data.locks.values()
    assert isinstance(lock, ZHALock)
    return lock


async def _run_ticks(hass: HomeAssistant) -> None:
    """Run enough sync ticks for every configured slot to be written."""
    for _ in range(len(CONFIGURED_PINS) + 2):
        await async_advance_time(hass, TICK_INTERVAL)


def _slot_suspension(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry, slot_num: int
) -> ir.IssueEntry | None:
    """Return the repair suspending a slot on the ZHA lock, if there is one."""
    lock = _zha_lock(lcm_config_entry)
    return ir.async_get(hass).async_get_issue(
        DOMAIN,
        f"slot_suspended_{lcm_config_entry.entry_id}_{lock.lock.entity_id}_{slot_num}",
    )


async def _tick_until_slot_1_is_suspended(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry
) -> None:
    """Run sync ticks until slot 1 is suspended, failing if it never is."""
    for _ in range(4 * MAX_SYNC_ATTEMPTS):
        if _slot_suspension(hass, lcm_config_entry, 1) is not None:
            return
        await async_advance_time(hass, TICK_INTERVAL)
    pytest.fail("slot 1 was never suspended")


def _in_sync_state(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry, slot_num: int
) -> str:
    """Return the state of a slot's in-sync sensor for the ZHA lock."""
    lock = _zha_lock(lcm_config_entry)
    state = hass.states.get(
        in_sync_entity_id(hass, lcm_config_entry, slot_num, lock.lock.entity_id)
    )
    assert state is not None
    return state.state


def _slot_enabled_state(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry, slot_num: int
) -> str:
    """Return the state of a slot's enabled switch."""
    state = hass.states.get(
        slot_entity_id(hass, SWITCH_DOMAIN, lcm_config_entry, slot_num, CONF_ENABLED)
    )
    assert state is not None
    return state.state


def _writes(door_lock: DoorLockStandIn, name: str) -> list[int]:
    """Return the user ids of every ``name`` command the lock received."""
    return [
        request.user_id
        for request in door_lock.requests
        if request.command.name == name
    ]


async def _disable_slot_1(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry
) -> None:
    """Turn slot 1 off, so sync clears its code from the lock."""
    await hass.services.async_call(
        SWITCH_DOMAIN,
        SERVICE_TURN_OFF,
        {
            ATTR_ENTITY_ID: slot_entity_id(
                hass, SWITCH_DOMAIN, lcm_config_entry, 1, CONF_ENABLED
            )
        },
        blocking=True,
    )


def _for_slot_1(frame: Frame) -> Answer:
    """Answer slot 1's request with ``frame``; leave every other one to the lock."""
    return lambda request: frame if request.user_id == 1 else None


def _applied_once(door_lock: DoorLockStandIn, frame: Frame) -> Answer:
    """
    Apply slot 1's first set or clear as the lock would, but answer it with ``frame``.

    Every later request, and every other slot's, is the lock's own to answer.
    """
    answered: list[int] = []

    def _answer(request: Any) -> Frame | None:
        if request.user_id != 1 or answered:
            return None
        answered.append(request.user_id)
        if request.command.name == "set_pin_code":
            door_lock.codes[request.user_id] = request.pin_code
        else:
            door_lock.codes.pop(request.user_id, None)
        return frame

    return _answer


def _foreign_replies(other_write: str) -> list[Any]:
    """
    Return frames about something other than a write, each built from a cluster.

    ``other_write`` is the response of the write not being tested. Each frame
    can fill the write's pending request, since zigpy pairs a reply with its
    request by sequence number, and none of them says whether the write landed.
    """
    return [
        pytest.param(
            lambda _cluster: default_response(_LOCK_DOOR_ID, Status.FAILURE),
            id="default_response_naming_another_command",
        ),
        pytest.param(
            lambda cluster: lock_frame(cluster, other_write, status=Status.FAILURE),
            id="the_other_writes_response",
        ),
        # Shares the Default Response's command id, and carries a status.
        pytest.param(
            lambda cluster: lock_frame(
                cluster, "set_week_day_schedule_response", status=Status.FAILURE
            ),
            id="cluster_command_sharing_the_default_response_id",
        ),
        pytest.param(
            lambda cluster: lock_frame(
                cluster,
                "operation_event_notification",
                operation_event_source=DoorLock.OperationEventSource.Keypad,
                operation_event_code=DoorLock.OperationEvent.Lock,
                user_id=1,
                pin=CONFIGURED_PINS[1],
                local_time=0,
            ),
            id="operation_event",
        ),
    ]


@_CLUSTER_TYPES
class TestSync:
    """Configured codes reach the lock and read back in sync."""

    async def test_configured_codes_are_written_and_in_sync(
        self,
        hass: HomeAssistant,
        lcm_config_entry: MockConfigEntry,
        door_lock: DoorLockStandIn,
    ) -> None:
        """Each configured code is set on the lock and its slot reads in sync."""
        await _run_ticks(hass)

        assert door_lock.codes == CONFIGURED_PINS
        lock = _zha_lock(lcm_config_entry)
        for slot_num, pin in CONFIGURED_PINS.items():
            assert lock.coordinator.data.get(pin_address(slot_num)) == (
                SlotCredential.known(pin)
            )
            assert _in_sync_state(hass, lcm_config_entry, slot_num) == STATE_ON

    @pytest.mark.parametrize("door_lock_codes", [CONFIGURED_PINS])
    async def test_codes_already_on_the_lock_are_not_rewritten(
        self,
        hass: HomeAssistant,
        lcm_config_entry: MockConfigEntry,
        door_lock: DoorLockStandIn,
    ) -> None:
        """A lock already holding every configured code is read, not written."""
        await _run_ticks(hass)

        assert _writes(door_lock, "set_pin_code") == []
        assert set(_writes(door_lock, "get_pin_code")) == set(CONFIGURED_PINS)
        for slot_num in CONFIGURED_PINS:
            assert _in_sync_state(hass, lcm_config_entry, slot_num) == STATE_ON


def _door_lock_cluster(zigpy_lock_device: Any) -> DoorLock:
    """Return the Door Lock cluster the lock decodes its frames with."""
    return zigpy_lock_device.endpoints[1].in_clusters[DoorLock.cluster_id]


@_CLUSTER_TYPES
class TestReadOfAnotherUser:
    """A reply about another user never answers for the slot read."""

    @pytest.fixture
    def door_lock_codes(self) -> dict[int, str]:
        """Slot 1's configured code sits on a user nothing manages; slot 1 is empty."""
        return {UNMANAGED_USER: CONFIGURED_PINS[1]}

    @pytest.fixture
    def door_lock_answers(self, zigpy_lock_device: Any) -> dict[str, Answer]:
        """Slot 1's get is answered, from the first read on, with that user's reply."""
        return {
            "get_pin_code": _for_slot_1(
                lock_frame(
                    _door_lock_cluster(zigpy_lock_device),
                    "get_pin_code_response",
                    user_id=UNMANAGED_USER,
                    user_status=DoorLock.UserStatus.Enabled,
                    user_type=DoorLock.UserType.Unrestricted,
                    code=CONFIGURED_PINS[1],
                )
            )
        }

    async def test_another_users_reply_does_not_answer_for_the_slot(
        self,
        hass: HomeAssistant,
        lcm_config_entry: MockConfigEntry,
        door_lock: DoorLockStandIn,
    ) -> None:
        """
        A reply naming another user is not slot 1's code, so slot 1 is programmed.

        zigpy pairs a reply with its request by sequence number, not by the
        user it names. Read as slot 1's, the other user's code matched slot
        1's configuration and slot 1 read in sync while the lock held nothing
        there.
        """
        await _run_ticks(hass)

        assert door_lock.codes[1] == CONFIGURED_PINS[1]
        assert 1 in _writes(door_lock, "set_pin_code")


@_CLUSTER_TYPES
class TestReadOfAnotherCommand:
    """A frame that is not a Get PIN Code Response never answers a read."""

    @pytest.fixture
    def door_lock_answers(self, zigpy_lock_device: Any) -> dict[str, Answer]:
        """Slot 1's get is answered, from the first read on, with a keypad lock."""
        return {
            "get_pin_code": _for_slot_1(
                lock_frame(
                    _door_lock_cluster(zigpy_lock_device),
                    "operation_event_notification",
                    operation_event_source=DoorLock.OperationEventSource.Keypad,
                    operation_event_code=DoorLock.OperationEvent.Lock,
                    user_id=1,
                    pin=CONFIGURED_PINS[1],
                    local_time=0,
                )
            )
        }

    async def test_an_operation_event_does_not_answer_for_the_slot(
        self,
        hass: HomeAssistant,
        lcm_config_entry: MockConfigEntry,
        door_lock: DoorLockStandIn,
    ) -> None:
        """
        An operation event filling slot 1's read leaves slot 1 to be programmed.

        Every decoded zigpy command is a tuple. Read positionally, the event's
        Lock code (1) passed for the Enabled status and its Personal
        Identification Number for the code, so the empty slot read as holding
        its configured code.
        """
        await _run_ticks(hass)

        assert door_lock.codes[1] == CONFIGURED_PINS[1]
        assert 1 in _writes(door_lock, "set_pin_code")


@_CLUSTER_TYPES
class TestWriteStatus:
    """A write's status is taken only from the reply that answers it."""

    @pytest.mark.parametrize(
        "own_refusal",
        [
            pytest.param(
                lambda cluster: lock_frame(
                    cluster, "set_pin_code_response", status=_DOOR_LOCK_GENERAL_FAILURE
                ),
                id="own_response",
            ),
            pytest.param(
                lambda _cluster: default_response(_SET_PIN_CODE_ID, Status.FAILURE),
                id="default_response_naming_the_set",
            ),
        ],
    )
    async def test_the_locks_own_rejection_disables_the_slot(
        self,
        hass: HomeAssistant,
        lcm_config_entry: MockConfigEntry,
        door_lock: DoorLockStandIn,
        own_refusal: Any,
    ) -> None:
        """A non-zero status in the set's own reply rejects the code."""
        door_lock.answers["set_pin_code"] = _for_slot_1(own_refusal(door_lock.cluster))

        await _run_ticks(hass)

        assert 1 not in door_lock.codes
        assert _slot_enabled_state(hass, lcm_config_entry, 1) == STATE_OFF
        assert _slot_enabled_state(hass, lcm_config_entry, 2) == STATE_ON

    @pytest.mark.parametrize("foreign", _foreign_replies("clear_pin_code_response"))
    async def test_a_refused_set_answered_by_another_frame_is_sent_again(
        self,
        hass: HomeAssistant,
        lcm_config_entry: MockConfigEntry,
        door_lock: DoorLockStandIn,
        foreign: Any,
    ) -> None:
        """
        A set the lock refused, answered by a frame about something else, is retried.

        The frame says nothing of whether the set landed. Confirmed on its
        strength, slot 1 read in sync while the lock held no code there;
        taken as a refusal, the slot was disabled for another command's
        failure. Each such answer is a failed write of slot 1, charged to
        the slot breaker until it suspends the slot -- not a connectivity
        failure, which would mark the whole lock unreachable.
        """
        door_lock.answers["set_pin_code"] = _for_slot_1(foreign(door_lock.cluster))

        await _tick_until_slot_1_is_suspended(hass, lcm_config_entry)

        assert 1 not in door_lock.codes
        assert _writes(door_lock, "set_pin_code").count(1) == MAX_SYNC_ATTEMPTS
        assert _in_sync_state(hass, lcm_config_entry, 1) != STATE_ON
        assert _slot_enabled_state(hass, lcm_config_entry, 1) == STATE_ON
        assert not _zha_lock(lcm_config_entry).coordinator.unreachable
        assert door_lock.codes[2] == CONFIGURED_PINS[2]
        assert _in_sync_state(hass, lcm_config_entry, 2) == STATE_ON

    @pytest.mark.parametrize("foreign", _foreign_replies("clear_pin_code_response"))
    async def test_a_set_answered_by_another_frame_converges_on_its_own_reply(
        self,
        hass: HomeAssistant,
        lcm_config_entry: MockConfigEntry,
        door_lock: DoorLockStandIn,
        foreign: Any,
    ) -> None:
        """A set the lock applied but another frame answered is sent again and syncs."""
        door_lock.answers["set_pin_code"] = _applied_once(
            door_lock, foreign(door_lock.cluster)
        )

        await _run_ticks(hass)

        assert _writes(door_lock, "set_pin_code").count(1) == 2
        assert door_lock.codes == CONFIGURED_PINS
        for slot_num in CONFIGURED_PINS:
            assert _slot_enabled_state(hass, lcm_config_entry, slot_num) == STATE_ON
            assert _in_sync_state(hass, lcm_config_entry, slot_num) == STATE_ON


@_CLUSTER_TYPES
class TestClearStatus:
    """A clear's status is taken only from the reply that answers it."""

    @pytest.mark.parametrize("foreign", _foreign_replies("set_pin_code_response"))
    async def test_a_refused_clear_answered_by_another_frame_is_sent_again(
        self,
        hass: HomeAssistant,
        lcm_config_entry: MockConfigEntry,
        door_lock: DoorLockStandIn,
        foreign: Any,
    ) -> None:
        """
        A clear the lock refused, answered by a frame about something else, is retried.

        Confirmed on that frame's strength, the slot read empty while the lock
        still held a code that opens the door. Each such answer is a failed
        write of slot 1, charged to the slot breaker until it suspends the
        slot, while the lock stays reachable.
        """
        await _run_ticks(hass)
        door_lock.answers["clear_pin_code"] = _for_slot_1(foreign(door_lock.cluster))

        await _disable_slot_1(hass, lcm_config_entry)
        await _tick_until_slot_1_is_suspended(hass, lcm_config_entry)

        assert door_lock.codes[1] == CONFIGURED_PINS[1]
        assert _writes(door_lock, "clear_pin_code").count(1) == MAX_SYNC_ATTEMPTS
        assert not _zha_lock(lcm_config_entry).coordinator.unreachable
        assert _zha_lock(lcm_config_entry).coordinator.data.get(pin_address(1)) == (
            SlotCredential.known(CONFIGURED_PINS[1])
        )
        assert _in_sync_state(hass, lcm_config_entry, 1) != STATE_ON

    @pytest.mark.parametrize("foreign", _foreign_replies("set_pin_code_response"))
    async def test_a_clear_answered_by_another_frame_converges_on_its_own_reply(
        self,
        hass: HomeAssistant,
        lcm_config_entry: MockConfigEntry,
        door_lock: DoorLockStandIn,
        foreign: Any,
    ) -> None:
        """A clear the lock applied but another frame answered is sent again and syncs."""
        await _run_ticks(hass)
        door_lock.answers["clear_pin_code"] = _applied_once(
            door_lock, foreign(door_lock.cluster)
        )

        await _disable_slot_1(hass, lcm_config_entry)
        await _run_ticks(hass)

        assert 1 not in door_lock.codes
        assert _writes(door_lock, "clear_pin_code").count(1) == 2
        assert _zha_lock(lcm_config_entry).coordinator.data.get(pin_address(1)) == (
            SlotCredential.empty()
        )
        assert _in_sync_state(hass, lcm_config_entry, 1) == STATE_ON

    @pytest.mark.parametrize(
        "own_refusal",
        [
            pytest.param(
                lambda cluster: lock_frame(
                    cluster,
                    "clear_pin_code_response",
                    status=_DOOR_LOCK_GENERAL_FAILURE,
                ),
                id="own_response",
            ),
            pytest.param(
                lambda _cluster: default_response(_CLEAR_PIN_CODE_ID, Status.FAILURE),
                id="default_response_naming_the_clear",
            ),
        ],
    )
    async def test_the_locks_own_rejection_fails_the_clear(
        self,
        hass: HomeAssistant,
        lcm_config_entry: MockConfigEntry,
        door_lock: DoorLockStandIn,
        own_refusal: Any,
    ) -> None:
        """A non-zero status in the clear's own reply leaves the code where it was."""
        await _run_ticks(hass)
        door_lock.answers["clear_pin_code"] = lambda _request: own_refusal(
            door_lock.cluster
        )

        await _disable_slot_1(hass, lcm_config_entry)
        await _run_ticks(hass)

        assert door_lock.codes[1] == CONFIGURED_PINS[1]
        assert _zha_lock(lcm_config_entry).coordinator.data.get(pin_address(1)) == (
            SlotCredential.known(CONFIGURED_PINS[1])
        )
        assert _in_sync_state(hass, lcm_config_entry, 1) != STATE_ON
