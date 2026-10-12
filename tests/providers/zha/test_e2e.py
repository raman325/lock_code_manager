"""End-to-end tests for the ZHA provider through a real Lock Code Manager entry."""

from __future__ import annotations

import re
from typing import Any

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from zigpy.zcl.clusters.closures import DoorLock
from zigpy.zcl.foundation import Status

from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.text import (
    ATTR_VALUE,
    DOMAIN as TEXT_DOMAIN,
    SERVICE_SET_VALUE,
)
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_CONDITION,
    CONF_ENABLED,
    CONF_PIN,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_OFF,
    STATE_ON,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir, translation

from custom_components.lock_code_manager.const import (
    CONF_SLOT,
    CONF_USERS,
    DOMAIN,
    MAX_SYNC_ATTEMPTS,
    SUBENTRY_TYPE_USER,
    TICK_INTERVAL,
)
from custom_components.lock_code_manager.domain.credentials import pin_address
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers.zha import ZHALock
from tests.common import (
    async_advance_clock,
    async_advance_sync_ticks,
    async_tick_until,
    in_sync_entity_id,
    slot_entity_id,
)
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
# A PIN for slot 1 that differs from the one it is configured with.
NEW_PIN = "9876"
# The entity that gates slot 1 in the tests that give it a condition.
SLOT_1_CONDITION = "input_boolean.slot_1_access"
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


async def _switch_slot_1(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry, service: str
) -> None:
    """Turn slot 1's enabled switch on or off."""
    await hass.services.async_call(
        SWITCH_DOMAIN,
        service,
        {
            ATTR_ENTITY_ID: slot_entity_id(
                hass, SWITCH_DOMAIN, lcm_config_entry, 1, CONF_ENABLED
            )
        },
        blocking=True,
    )


async def _disable_slot_1(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry
) -> None:
    """Turn slot 1 off, so sync clears its code from the lock."""
    await _switch_slot_1(hass, lcm_config_entry, SERVICE_TURN_OFF)


async def _enable_slot_1(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry
) -> None:
    """Turn slot 1 back on, so sync wants its configured code on the lock."""
    await _switch_slot_1(hass, lcm_config_entry, SERVICE_TURN_ON)


async def _set_slot_1_pin(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry, pin: str
) -> None:
    """Set slot 1's PIN; an empty one makes sync clear its code from the lock."""
    await hass.services.async_call(
        TEXT_DOMAIN,
        SERVICE_SET_VALUE,
        {ATTR_VALUE: pin},
        target={
            ATTR_ENTITY_ID: slot_entity_id(
                hass, TEXT_DOMAIN, lcm_config_entry, 1, CONF_PIN
            )
        },
        blocking=True,
    )


async def _remove_slot_1_pin(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry
) -> None:
    """Empty slot 1's PIN, so sync clears its code from the lock."""
    await _set_slot_1_pin(hass, lcm_config_entry, "")


async def _give_slot_1_a_new_pin_and_enable_it(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry
) -> None:
    """Give slot 1 a PIN other than the one on the lock, and turn it back on."""
    await _set_slot_1_pin(hass, lcm_config_entry, NEW_PIN)
    await _enable_slot_1(hass, lcm_config_entry)


async def _turn_slot_1_condition_off(
    hass: HomeAssistant, _lcm_config_entry: MockConfigEntry
) -> None:
    """Turn slot 1's condition off while the slot stays enabled."""
    hass.states.async_set(SLOT_1_CONDITION, STATE_OFF)
    await hass.async_block_till_done()


async def _turn_slot_1_condition_on(
    hass: HomeAssistant, _lcm_config_entry: MockConfigEntry
) -> None:
    """Turn slot 1's condition back on."""
    hass.states.async_set(SLOT_1_CONDITION, STATE_ON)
    await hass.async_block_till_done()


def _slot_1_disabled_repair(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry
) -> ir.IssueEntry | None:
    """Return the repair reporting slot 1 as disabled, if there is one."""
    return ir.async_get(hass).async_get_issue(
        DOMAIN, f"slot_disabled_{lcm_config_entry.entry_id}_1"
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


@pytest.mark.parametrize("door_lock_codes", [CONFIGURED_PINS])
async def test_a_new_pin_on_a_slot_in_sync_is_written(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    lcm_config_entry: MockConfigEntry,
    door_lock: DoorLockStandIn,
) -> None:
    """
    Regression for #63: changing the PIN of a slot in sync writes the new PIN.

    The new PIN stayed configured while the lock kept opening for the old
    one.
    """
    await async_tick_until(
        hass,
        freezer,
        lambda: _in_sync_state(hass, lcm_config_entry, 1) == STATE_ON,
        max_ticks=3,
        what="slot 1 in sync",
    )

    await _set_slot_1_pin(hass, lcm_config_entry, NEW_PIN)
    assert _in_sync_state(hass, lcm_config_entry, 1) == STATE_OFF
    await async_tick_until(
        hass,
        freezer,
        lambda: _in_sync_state(hass, lcm_config_entry, 1) == STATE_ON,
        max_ticks=3,
        what="slot 1 back in sync",
    )
    await async_advance_sync_ticks(hass, freezer, 3)

    assert door_lock.codes == {**CONFIGURED_PINS, 1: NEW_PIN}
    assert [
        (request.user_id, request.pin_code)
        for request in door_lock.requests
        if request.command.name == "set_pin_code"
    ] == [(1, NEW_PIN)]
    assert _in_sync_state(hass, lcm_config_entry, 1) == STATE_ON


async def test_a_reply_delay_holds_the_answer_that_long_on_the_clock(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    lcm_config_entry: MockConfigEntry,
    door_lock: DoorLockStandIn,
) -> None:
    """The stand-in answers a delayed command once the clock has moved that far."""
    lock = _zha_lock(lcm_config_entry)
    await async_advance_sync_ticks(hass, freezer)
    door_lock.reply_delays["get_pin_code"] = 30
    try:
        read = hass.async_create_task(lock.async_get_usercodes({1}))
        await async_advance_clock(hass, freezer, 29, settle=False)
        assert not read.done()
        await async_advance_clock(hass, freezer, 1, settle=False)
        assert read.done()
        assert read.result()[1] == SlotCredential.known(CONFIGURED_PINS[1])
    finally:
        door_lock.reply_delays.clear()
        await async_advance_clock(hass, freezer, 30, settle=False)


# Within what zigpy can spend on one command at the end of its retries,
# and three of them past the flat operation deadline.
_SLOW_REPLY = 300.0


@pytest.mark.parametrize(
    ("lcm_slots", "door_lock_codes"),
    [({1: ZHA_LCM_CONFIG_SLOTS[1]}, {1: CONFIGURED_PINS[1]})],
)
async def test_adding_users_reads_a_slow_lock_for_as_long_as_the_read_walks(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    lcm_config_entry: MockConfigEntry,
    door_lock: DoorLockStandIn,
) -> None:
    """
    Regression for #1528: a read walking several slow slots gets a deadline per slot.

    Adding three users to a one-user entry reads three numbers off the
    lock, one command each. The deadline was sized by how many slots the
    entry manages rather than how many the read walks, so a lock answering
    every command, slowly, was cut off partway and the users could not be
    added.
    """
    await async_tick_until(
        hass,
        freezer,
        lambda: _in_sync_state(hass, lcm_config_entry, 1) == STATE_ON,
        max_ticks=3,
        what="slot 1 in sync",
    )
    door_lock.reply_delays["get_pin_code"] = _SLOW_REPLY
    try:
        flow = await hass.config_entries.subentries.async_init(
            (lcm_config_entry.entry_id, SUBENTRY_TYPE_USER),
            context={"source": SOURCE_USER},
        )
        flow = await hass.config_entries.subentries.async_configure(
            flow["flow_id"], {"next_step_id": "several"}
        )
        adding = hass.async_create_task(
            hass.config_entries.subentries.async_configure(
                flow["flow_id"],
                {
                    CONF_USERS: {
                        f"user{number}": {CONF_ENABLED: True, CONF_PIN: f"{number}" * 4}
                        for number in (2, 3, 4)
                    }
                },
            )
        )
        await async_advance_clock(hass, freezer, 4 * _SLOW_REPLY, settle=False)

        assert adding.done()
        result = adding.result()
        assert result.get("reason") == "users_added", result.get("errors")
        # One read of the three numbers, each its own slow exchange.
        assert [
            request.user_id
            for request in door_lock.requests
            if request.command.name == "get_pin_code" and request.user_id != 1
        ] == [2, 3, 4]
        assert {
            subentry.title: subentry.data[CONF_SLOT]
            for subentry in lcm_config_entry.subentries.values()
        } == {"slot1": 1, "user2": 2, "user3": 3, "user4": 4}
    finally:
        # A slow reply still on its way waits on the clock, and so would the
        # unload after the test.
        door_lock.reply_delays.clear()
        await async_advance_clock(hass, freezer, _SLOW_REPLY, settle=False)


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
        assert door_lock.codes[2] == CONFIGURED_PINS[2]
        assert _in_sync_state(hass, lcm_config_entry, 2) == STATE_ON

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


async def _rendered_repair(hass: HomeAssistant, issue: ir.IssueEntry) -> str:
    """Return the repair's description as Home Assistant shows it, in English."""
    translations = await translation.async_get_translations(
        hass, "en", "issues", {DOMAIN}
    )
    template = translations[
        f"component.{DOMAIN}.issues.{issue.translation_key}"
        ".fix_flow.step.init.description"
    ]
    placeholders = issue.translation_placeholders or {}
    return re.sub(r"\{(\w+)\}", lambda match: placeholders[match[1]], template)


def _clear_slot_1_at_the_keypad(door_lock: DoorLockStandIn) -> None:
    """Remove slot 1's code at the lock, which reports the deletion as it does."""
    del door_lock.codes[1]
    event = lock_frame(
        door_lock.cluster,
        "programming_event_notification",
        program_event_source=DoorLock.OperationEventSource.Keypad,
        program_event_code=DoorLock.ProgrammingEvent.PINCodeDeleted,
        user_id=1,
        pin=CONFIGURED_PINS[1],
        user_type=DoorLock.UserType.Unrestricted,
        user_status=DoorLock.UserStatus.Available,
        local_time=0,
        data="",
    )
    door_lock.cluster.listener_event(
        "cluster_command",
        0,
        DoorLock.ClientCommandDefs.programming_event_notification.id,
        event.payload,
    )


@_CLUSTER_TYPES
class TestRefusedClear:
    """A clear the lock refuses in its own reply suspends the slot, once."""

    @pytest.fixture
    def lcm_slots(self, hass: HomeAssistant) -> dict[int, dict[str, Any]]:
        """Gate slot 1 on a condition that starts on, so it can be turned off."""
        hass.states.async_set(SLOT_1_CONDITION, STATE_ON)
        return {
            **ZHA_LCM_CONFIG_SLOTS,
            1: {**ZHA_LCM_CONFIG_SLOTS[1], CONF_CONDITION: SLOT_1_CONDITION},
        }

    @staticmethod
    async def _refuse_every_clear_of_slot_1(
        hass: HomeAssistant,
        lcm_config_entry: MockConfigEntry,
        door_lock: DoorLockStandIn,
        make_slot_1_clear: Any,
    ) -> None:
        """Sync every slot, then have the lock refuse each clear of slot 1."""
        await _run_ticks(hass)
        assert door_lock.codes == CONFIGURED_PINS
        door_lock.answers["clear_pin_code"] = _for_slot_1(
            lock_frame(
                door_lock.cluster,
                "clear_pin_code_response",
                status=_DOOR_LOCK_GENERAL_FAILURE,
            )
        )
        await make_slot_1_clear(hass, lcm_config_entry)
        for _ in range(4 * MAX_SYNC_ATTEMPTS):
            await async_advance_time(hass, TICK_INTERVAL)

    @pytest.mark.parametrize(
        ("make_slot_1_clear", "enabled_after"),
        [
            pytest.param(_disable_slot_1, STATE_OFF, id="slot_disabled"),
            # Emptying an enabled slot's PIN turns the slot off by itself.
            pytest.param(_remove_slot_1_pin, STATE_OFF, id="pin_removed"),
            pytest.param(_turn_slot_1_condition_off, STATE_ON, id="condition_off"),
        ],
    )
    async def test_a_clear_the_lock_keeps_refusing_suspends_the_slot(
        self,
        hass: HomeAssistant,
        lcm_config_entry: MockConfigEntry,
        door_lock: DoorLockStandIn,
        make_slot_1_clear: Any,
        enabled_after: str,
    ) -> None:
        """
        A refused clear is sent once, then the slot is suspended and reported.

        Disabling the slot for the refusal asked for the same clear again,
        and reset the slot breaker as it did, so the clear went out on every
        tick for as long as the lock kept refusing it. It also switched off
        a slot whose condition, not its user, wanted it empty. The code is
        still on the lock, so the repair says so.
        """
        await self._refuse_every_clear_of_slot_1(
            hass, lcm_config_entry, door_lock, make_slot_1_clear
        )

        assert _writes(door_lock, "clear_pin_code").count(1) == 1
        suspension = _slot_suspension(hass, lcm_config_entry, 1)
        assert suspension is not None
        text = await _rendered_repair(hass, suspension)
        assert "couldn't remove the code" in text
        assert "status FAILURE (1)" in text
        assert "may still be on the lock" in text
        assert "delete it at the lock itself" in text
        assert "cannot set code" not in text
        assert "<Status." not in text
        assert text.count("resume") == 1
        assert "resumes once the lock reports the slot empty" in text
        assert _slot_1_disabled_repair(hass, lcm_config_entry) is None
        assert _in_sync_state(hass, lcm_config_entry, 1) != STATE_ON
        assert door_lock.codes[1] == CONFIGURED_PINS[1]
        assert _slot_enabled_state(hass, lcm_config_entry, 1) == enabled_after
        assert door_lock.codes[2] == CONFIGURED_PINS[2]
        assert _in_sync_state(hass, lcm_config_entry, 2) == STATE_ON

    @pytest.mark.parametrize(
        ("make_slot_1_clear", "change_slot_1_target", "pin_after"),
        [
            pytest.param(
                _disable_slot_1,
                _enable_slot_1,
                CONFIGURED_PINS[1],
                id="slot_re_enabled",
            ),
            pytest.param(
                _remove_slot_1_pin,
                _give_slot_1_a_new_pin_and_enable_it,
                NEW_PIN,
                id="pin_re_entered",
            ),
            pytest.param(
                _turn_slot_1_condition_off,
                _turn_slot_1_condition_on,
                CONFIGURED_PINS[1],
                id="condition_on",
            ),
        ],
    )
    async def test_a_slot_suspended_for_a_refused_clear_resumes_on_a_new_target(
        self,
        hass: HomeAssistant,
        lcm_config_entry: MockConfigEntry,
        door_lock: DoorLockStandIn,
        make_slot_1_clear: Any,
        change_slot_1_target: Any,
        pin_after: str,
    ) -> None:
        """A new PIN or active state for the slot ends the suspension, and it syncs."""
        await self._refuse_every_clear_of_slot_1(
            hass, lcm_config_entry, door_lock, make_slot_1_clear
        )
        assert _slot_suspension(hass, lcm_config_entry, 1) is not None

        await change_slot_1_target(hass, lcm_config_entry)
        await _run_ticks(hass)

        assert door_lock.codes[1] == pin_after
        assert _in_sync_state(hass, lcm_config_entry, 1) == STATE_ON
        assert _slot_suspension(hass, lcm_config_entry, 1) is None
        assert _writes(door_lock, "clear_pin_code").count(1) == 1

    @pytest.mark.parametrize(
        "make_slot_1_clear",
        [
            pytest.param(_disable_slot_1, id="slot_disabled"),
            pytest.param(_remove_slot_1_pin, id="pin_removed"),
            pytest.param(_turn_slot_1_condition_off, id="condition_off"),
        ],
    )
    async def test_a_slot_suspended_for_a_refused_clear_resumes_once_the_lock_is_empty(
        self,
        hass: HomeAssistant,
        lcm_config_entry: MockConfigEntry,
        door_lock: DoorLockStandIn,
        make_slot_1_clear: Any,
    ) -> None:
        """The code removed at the lock itself brings the slot back in sync."""
        await self._refuse_every_clear_of_slot_1(
            hass, lcm_config_entry, door_lock, make_slot_1_clear
        )
        assert _slot_suspension(hass, lcm_config_entry, 1) is not None

        _clear_slot_1_at_the_keypad(door_lock)
        await _run_ticks(hass)

        assert 1 not in door_lock.codes
        assert _in_sync_state(hass, lcm_config_entry, 1) == STATE_ON
        assert _slot_suspension(hass, lcm_config_entry, 1) is None
        assert _writes(door_lock, "clear_pin_code").count(1) == 1
