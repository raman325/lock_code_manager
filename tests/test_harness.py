"""
Tests for the test harness itself: clock helpers, the time-driven tick, fault knobs.

A helper that silently does nothing makes every test built on it pass for
the wrong reason, so each one is shown here to do what it says -- and to
fail loudly, rather than hang, when what it waits for never comes.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable, Generator
from datetime import datetime
import time
from typing import Any
from unittest.mock import PropertyMock, patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.text import (
    ATTR_VALUE,
    DOMAIN as TEXT_DOMAIN,
    SERVICE_SET_VALUE as TEXT_SERVICE_SET_VALUE,
)
from homeassistant.const import ATTR_ENTITY_ID, STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from custom_components.lock_code_manager.const import (
    DOMAIN,
    MAX_SYNC_ATTEMPTS,
    TICK_INTERVAL,
)
from custom_components.lock_code_manager.domain.credentials import WriteResult
from custom_components.lock_code_manager.domain.exceptions import (
    CodeRejectedError,
    LockDisconnected,
    LockOperationFailed,
)
from custom_components.lock_code_manager.domain.models import SlotCredential, SyncState
from custom_components.lock_code_manager.domain.sync import SlotSyncManager

from .common import (
    BASE_CONFIG,
    CLOCK_STEP,
    LOCK_1_ENTITY_ID,
    LOCK_2_ENTITY_ID,
    SLOT_1_PIN_ENTITY,
    FaultKind,
    MockLCMLock,
    MockLCMPushLock,
    _async_step_timers_to,
    all_sync_managers,
    async_advance_sync_ticks,
    async_run_until_quiet,
    async_settle_background,
    async_step_clock_until,
    async_tick_until,
    async_trip_slot_breaker,
    in_sync_entity_id,
    mock_lock,
    release_all_faults,
    sync_status,
)
from .conftest import pytest_runtest_teardown

# -- Clock helpers ----------------------------------------------------------


async def test_step_clock_until_fails_and_cancels_instead_of_hanging(
    freezer: FrozenDateTimeFactory, hass: HomeAssistant
) -> None:
    """A wait the clock never reaches fails the test and cancels what waited."""
    sleeper = hass.async_create_task(asyncio.sleep(60))

    with pytest.raises(pytest.fail.Exception, match="after 3 clock steps"):
        await async_step_clock_until(
            hass, freezer, sleeper.done, max_steps=3, what="the sleeper"
        )

    assert sleeper.cancelled()


async def test_run_until_quiet_fails_on_a_loop_that_never_rests(
    hass: HomeAssistant,
) -> None:
    """Something rescheduling itself forever fails the test, not the run."""

    async def spin() -> None:
        while True:
            await asyncio.sleep(0)

    spinner = hass.async_create_task(spin())
    try:
        with pytest.raises(pytest.fail.Exception, match="still busy"):
            await async_run_until_quiet()
    finally:
        spinner.cancel()
        await asyncio.gather(spinner, return_exceptions=True)


async def test_settle_background_steps_the_clock_through_a_sleep(
    freezer: FrozenDateTimeFactory, hass: HomeAssistant
) -> None:
    """A background task asleep on the frozen clock finishes by stepping it."""
    sleeper = hass.async_create_background_task(asyncio.sleep(0.1), "sleeper")

    await async_settle_background(hass, freezer)

    assert sleeper.done()
    assert not sleeper.cancelled()


async def test_advancing_sync_ticks_moves_both_clocks(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    Pending-write deadlines and breaker windows see the time pass.

    Once the first call has lined the ticks up, each call after it moves
    both clocks a whole interval, less the little the settling took.
    """
    await async_advance_sync_ticks(hass, freezer)
    monotonic, wall = time.monotonic(), dt_util.utcnow()

    await async_advance_sync_ticks(hass, freezer, 3)

    moved = time.monotonic() - monotonic
    assert moved > 2 * TICK_INTERVAL.total_seconds()
    assert (dt_util.utcnow() - wall).total_seconds() == pytest.approx(moved, abs=1e-6)


async def test_advancing_sync_ticks_fails_with_no_sync_tick_scheduled(
    freezer: FrozenDateTimeFactory, hass: HomeAssistant
) -> None:
    """With no entry loaded there is no tick to advance to, and it says so."""
    with pytest.raises(pytest.fail.Exception, match="no sync tick scheduled"):
        await async_advance_sync_ticks(hass, freezer)


async def test_advancing_sync_ticks_fails_on_a_timer_that_never_stops(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """A timer rearming itself every millisecond fails the test, not the run."""
    await async_advance_sync_ticks(hass, freezer)
    loop = asyncio.get_running_loop()
    timers: list[asyncio.TimerHandle] = []

    def rearm() -> None:
        timers.append(loop.call_later(0.001, rearm))

    rearm()
    try:
        with pytest.raises(pytest.fail.Exception, match="timers came due"):
            await async_advance_sync_ticks(hass, freezer)
    finally:
        timers[-1].cancel()


async def test_stepping_may_reach_the_end_on_its_last_allowed_jump(
    freezer: FrozenDateTimeFactory, hass: HomeAssistant
) -> None:
    """The bound on jumps counts jumps: reaching the end on the last one is success."""
    loop = asyncio.get_running_loop()
    end = loop.time() + 0.5

    with patch("tests.common._MAX_TIMERS_PER_TICK", 1):
        await _async_step_timers_to(freezer, end)

    assert loop.time() >= end


async def test_tick_until_fails_on_a_condition_that_never_comes(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """The bound is the failure message, not a hang."""
    with pytest.raises(pytest.fail.Exception, match="the impossible not reached"):
        await async_tick_until(
            hass, freezer, lambda: False, max_ticks=2, what="the impossible"
        )


# -- The time-driven sync tick ------------------------------------------------


async def _async_change_pin(hass: HomeAssistant, pin: str) -> None:
    """Set slot 1's PIN through its text entity."""
    await hass.services.async_call(
        TEXT_DOMAIN,
        TEXT_SERVICE_SET_VALUE,
        {ATTR_VALUE: pin},
        target={ATTR_ENTITY_ID: SLOT_1_PIN_ENTITY},
        blocking=True,
    )


async def test_scheduled_tick_writes_a_changed_pin(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    Moving time is enough for the sync to write a new PIN and report in sync.

    Nothing here touches the sync manager: the write can only come from the
    tick its interval timer schedules.
    """
    entry = lock_code_manager_config_entry
    lock = mock_lock(entry)
    in_sync = in_sync_entity_id(hass, entry, 1)
    await async_advance_sync_ticks(hass, freezer)
    assert hass.states.get(in_sync).state == STATE_ON

    await _async_change_pin(hass, "4321")
    assert hass.states.get(in_sync).state == STATE_OFF

    await async_tick_until(
        hass,
        freezer,
        lambda: hass.states.get(in_sync).state == STATE_ON,
        max_ticks=3,
        what="slot 1 in sync",
    )
    assert lock.codes[1] == "4321"
    assert lock.service_calls["set_usercode"] == [(1, "4321", "test1")]


async def test_without_time_passing_no_tick_writes(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    The control for the test above: settle everything, but move no time.

    Only a tick writes, so a PIN change with the clock standing still stays
    out of sync and never reaches the lock.
    """
    entry = lock_code_manager_config_entry
    lock = mock_lock(entry)
    in_sync = in_sync_entity_id(hass, entry, 1)
    await async_advance_sync_ticks(hass, freezer)

    await _async_change_pin(hass, "4321")
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hass.states.get(in_sync).state == STATE_OFF
    assert lock.codes[1] == "1234"
    assert not lock.service_calls["set_usercode"]


async def test_a_timer_due_within_a_tick_fires_at_its_own_time(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    A timer due half a second in fires then, with the tick that writes still to come.

    Production never runs a confirmation read or a pending-write deadline
    at the moment of a tick due a second and a half later, so neither may
    the helper: the clock stops at each timer on the way to the tick.
    """
    entry = lock_code_manager_config_entry
    lock = mock_lock(entry)
    await async_advance_sync_ticks(hass, freezer)
    await _async_change_pin(hass, "4321")
    loop = asyncio.get_running_loop()
    due = loop.time() + 0.5
    seen: list[tuple[float, int]] = []

    @callback
    def _record(_now: Any) -> None:
        seen.append((loop.time(), len(lock.service_calls["set_usercode"])))

    async_call_later(hass, 0.5, _record)
    await async_advance_sync_ticks(hass, freezer)

    [(fired_at, sets_before)] = seen
    assert due <= fired_at < due + CLOCK_STEP
    assert sets_before == 0
    assert lock.service_calls["set_usercode"] == [(1, "4321", "test1")]


@pytest.fixture
def interval_ticks() -> Generator[list[tuple[int, float]]]:
    """
    Record every interval tick a sync manager runs: which manager, and when.

    Requested before the entry sets up, so every manager's interval timer
    calls the recorder. The interval timer passes the time it fired; the
    first tick, which the manager runs as it starts, passes nothing and is
    not recorded.
    """
    fired: list[tuple[int, float]] = []
    tick = SlotSyncManager._async_tick

    async def _recorded(self: SlotSyncManager, now: datetime | None = None) -> None:
        if now is not None:
            fired.append((id(self), asyncio.get_running_loop().time()))
        await tick(self, now)

    with patch.object(SlotSyncManager, "_async_tick", _recorded):
        yield fired


@pytest.mark.parametrize(
    "move",
    [
        pytest.param(lambda now, due: due - now + 0.5, id="overdue"),
        pytest.param(lambda now, due: due - now, id="due-as-it-starts"),
        pytest.param(lambda now, due: due - now - 0.0005, id="due-within-a-step"),
        pytest.param(lambda now, due: due - now - CLOCK_STEP, id="due-a-step-in"),
        pytest.param(lambda now, due: 1.996, id="ticked-1.996"),
        pytest.param(lambda now, due: 1.9995, id="ticked-1.9995"),
        pytest.param(lambda now, due: 0.0, id="no-time-between"),
    ],
)
async def test_each_advance_runs_every_sync_tick_exactly_once(
    interval_ticks: list[tuple[int, float]],
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    move: Callable[[float, float], float],
) -> None:
    """
    One call, one tick per sync manager, whatever the clock's phase when it starts.

    A tick that fired early in a call re-arms a whole interval later; if
    the call ran on past that, the same manager would tick twice, and a
    test counting failures per tick would count two. The calls after the
    first run back to back, as a test looping on ticks does.
    """
    managers = {
        id(manager) for manager in all_sync_managers(lock_code_manager_config_entry)
    }
    loop = asyncio.get_running_loop()

    async def _advance_once() -> None:
        start = len(interval_ticks)
        await async_advance_sync_ticks(hass, freezer)
        fired = Counter(manager for manager, _ in interval_ticks[start:])
        assert fired == dict.fromkeys(managers, 1)

    await _advance_once()
    next_due = max(at for _, at in interval_ticks) + TICK_INTERVAL.total_seconds()
    freezer.tick(move(loop.time(), next_due))
    for _ in range(6):
        await _advance_once()


# -- Breakers through real failures -------------------------------------------


async def test_breaker_trips_on_real_failures_and_recovers(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    Repeated failed writes, one per tick, suspend the slot; a new PIN resumes it.

    The breaker is never touched: what suspends the slot is the sync's own
    accounting of failures the lock really raised on ticks that really ran.
    """
    entry = lock_code_manager_config_entry
    lock = mock_lock(entry)
    in_sync = in_sync_entity_id(hass, entry, 1)
    issue_id = f"slot_suspended_{entry.entry_id}_{LOCK_1_ENTITY_ID}_1"
    await async_advance_sync_ticks(hass, freezer)
    await _async_change_pin(hass, "4321")

    fault = await async_trip_slot_breaker(hass, freezer, entry, 1)

    assert fault.hits == MAX_SYNC_ATTEMPTS
    assert sync_status(hass, in_sync) == SyncState.SUSPENDED.value
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    assert lock.codes[1] == "1234"

    # Suspended means no more attempts, however long the lock is left alone.
    await async_advance_sync_ticks(hass, freezer, 5)
    assert fault.hits == MAX_SYNC_ATTEMPTS

    fault.clear()
    await _async_change_pin(hass, "5555")
    await async_tick_until(
        hass,
        freezer,
        lambda: hass.states.get(in_sync).state == STATE_ON,
        max_ticks=3,
        what="slot 1 back in sync",
    )
    assert lock.codes[1] == "5555"
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is None


async def test_lock_breaker_trips_on_real_disconnects_and_recovers(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    A lock that answers nothing suspends its out-of-sync slot; answering resumes it.

    Every call failing is what makes the lock unreachable: a read that
    succeeds is the lock's breaker's recovery signal.
    """
    entry = lock_code_manager_config_entry
    lock = mock_lock(entry)
    in_sync = in_sync_entity_id(hass, entry, 1)
    await async_advance_sync_ticks(hass, freezer)
    await _async_change_pin(hass, "4321")

    with lock.fail(None, error=LockDisconnected):
        await async_tick_until(
            hass,
            freezer,
            lambda: sync_status(hass, in_sync) == SyncState.SUSPENDED.value,
            max_ticks=5,
            what="slot 1 suspended",
        )
    assert lock.codes[1] == "1234"

    await async_tick_until(
        hass,
        freezer,
        lambda: hass.states.get(in_sync).state == STATE_ON,
        max_ticks=5,
        what="slot 1 back in sync",
    )
    assert lock.codes[1] == "4321"


async def test_trip_slot_breaker_fails_loudly_on_a_slot_already_in_sync(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """A slot with nothing to write never fails, and the helper says so."""
    entry = lock_code_manager_config_entry
    await async_advance_sync_ticks(hass, freezer)

    with pytest.raises(pytest.fail.Exception, match="after 0 failed sets"):
        await async_trip_slot_breaker(hass, freezer, entry, 1)

    assert not mock_lock(entry).faults


async def test_trip_slot_breaker_reports_a_tick_that_never_settles_as_itself(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """A write held at the lock is a settle failure, not a slot that never failed."""
    entry = lock_code_manager_config_entry
    await async_advance_sync_ticks(hass, freezer)
    await _async_change_pin(hass, "4321")
    mock_lock(entry).hang("set", 1)

    with pytest.raises(pytest.fail.Exception, match="background lock work"):
        await async_trip_slot_breaker(hass, freezer, entry, 1)


# -- Fault knobs ----------------------------------------------------------------


@pytest.fixture
def lock(hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry):
    """Return lock 1's mock, for calling the fake directly."""
    return mock_lock(lock_code_manager_config_entry)


async def test_ignore_acknowledges_a_set_without_applying_it(lock) -> None:
    """The set reports a change and is recorded; the lock keeps its code."""
    fault = lock.ignore("set", 1)

    assert await lock.async_set_usercode(1, "9999", "test1") is WriteResult.CONFIRMED

    assert lock.codes[1] == "1234"
    assert lock.service_calls["set_usercode"][-1] == (1, "9999", "test1")
    assert fault.hits == 1


async def test_ignore_acknowledges_a_clear_without_applying_it(lock) -> None:
    """The clear reports a change; the code stays."""
    lock.ignore("clear", 1)

    assert await lock.async_clear_usercode(1) is True

    assert lock.codes[1] == "1234"
    assert lock.service_calls["clear_usercode"][-1] == (1,)


async def test_ignore_lets_a_write_that_changes_nothing_through_uncounted(
    lock,
) -> None:
    """
    Writing the code a slot holds, or clearing an empty one, spends no ``times``.

    The lock answers those as it always does, so the fault is still there for
    the write that would have changed the slot.
    """
    set_fault = lock.ignore("set", 1, times=1)
    clear_fault = lock.ignore("clear", 3, times=1)

    assert await lock.async_set_usercode(1, "1234") is WriteResult.NO_CHANGE
    assert await lock.async_clear_usercode(3) is False
    assert (set_fault.hits, clear_fault.hits) == (0, 0)

    assert await lock.async_set_usercode(1, "9999") is WriteResult.CONFIRMED
    assert lock.codes[1] == "1234"
    lock.codes[3] = "3333"
    assert await lock.async_clear_usercode(3) is True
    assert lock.codes[3] == "3333"
    assert (set_fault.hits, clear_fault.hits) == (1, 1)
    assert not lock.faults


async def test_refuse_raises_the_providers_rejection(lock) -> None:
    """A refused set and a refused clear both raise ``CodeRejectedError``."""
    lock.refuse("set", 1)
    lock.refuse("clear", 2)

    with pytest.raises(CodeRejectedError) as set_err:
        await lock.async_set_usercode(1, "9999")
    with pytest.raises(CodeRejectedError) as clear_err:
        await lock.async_clear_usercode(2)

    assert (set_err.value.code_slot, set_err.value.lock_entity_id) == (
        1,
        LOCK_1_ENTITY_ID,
    )
    assert clear_err.value.code_slot == 2
    assert lock.codes == {1: "1234", 2: "5678"}


async def test_fail_raises_before_the_lock_acts(lock) -> None:
    """The default error, a given class, and a given instance."""
    lock.fail("set", 1, times=1)
    lock.fail("clear", 1, error=LockDisconnected, times=1)
    instance = LockOperationFailed("this one")
    lock.fail("get", error=instance, times=1)

    with pytest.raises(LockOperationFailed, match="injected set failure"):
        await lock.async_set_usercode(1, "9999")
    with pytest.raises(LockDisconnected, match="injected clear failure"):
        await lock.async_clear_usercode(1)
    with pytest.raises(LockOperationFailed) as read_err:
        await lock.async_get_usercodes()

    assert read_err.value is instance
    assert lock.codes[1] == "1234"


async def test_times_limits_a_fault_then_it_removes_itself(lock) -> None:
    """Two failures, then the lock answers again."""
    fault = lock.fail("set", 1, times=2)

    for _ in range(2):
        with pytest.raises(LockOperationFailed):
            await lock.async_set_usercode(1, "9999")
    assert await lock.async_set_usercode(1, "9999") is WriteResult.CONFIRMED

    assert fault.hits == 2
    assert fault not in lock.faults


async def test_a_fault_matches_only_its_slot_and_operation(lock) -> None:
    """A set fault on slot 2 leaves slot 2's clears and slot 1's sets alone."""
    lock.fail("set", 2)

    assert await lock.async_set_usercode(1, "9999") is WriteResult.CONFIRMED
    assert await lock.async_clear_usercode(2) is True
    with pytest.raises(LockOperationFailed):
        await lock.async_set_usercode(2, "8888")


async def test_a_read_fault_matches_reads_whose_scope_holds_its_slot(lock) -> None:
    """A full read covers every managed slot; a read of slot 2 does not cover 1."""
    lock.fail("get", 1)

    assert await lock.async_get_usercodes([2]) == {
        2: SlotCredential.known("5678"),
        1: SlotCredential.known("1234"),
    }
    with pytest.raises(LockOperationFailed):
        await lock.async_get_usercodes()


async def _async_answer(task: asyncio.Task[Any]) -> Any:
    """Return what a released call answered, failing if it is still waiting."""
    await async_run_until_quiet()
    assert task.done(), "the call is still waiting"
    return task.result()


async def test_hang_holds_a_write_until_cleared(hass: HomeAssistant, lock) -> None:
    """Held, nothing is applied; cleared, the write runs as normal."""
    fault = lock.hang("set", 1)
    write = hass.async_create_task(lock.async_set_usercode(1, "9999"))

    await fault.async_wait_held()
    assert not write.done()
    assert lock.codes[1] == "1234"

    fault.clear()
    assert await _async_answer(write) is WriteResult.CONFIRMED
    assert lock.codes[1] == "9999"


async def test_drop_reply_applies_the_write_and_never_answers(
    hass: HomeAssistant, lock
) -> None:
    """The lock holds the new code while the caller waits; cleared, it times out."""
    fault = lock.drop_reply("set", 1)
    write = hass.async_create_task(lock.async_set_usercode(1, "9999"))

    await fault.async_wait_held()
    assert not write.done()
    assert lock.codes[1] == "9999"

    fault.clear()
    with pytest.raises(LockDisconnected, match="no reply"):
        await _async_answer(write)


async def test_drop_reply_on_a_read(hass: HomeAssistant, lock) -> None:
    """A read whose answer never comes, until the fault is cleared."""
    with lock.drop_reply("get") as fault:
        read = hass.async_create_task(lock.async_get_usercodes())
        await fault.async_wait_held()
        assert not read.done()

    with pytest.raises(LockDisconnected):
        await _async_answer(read)


async def test_wait_held_fails_when_nothing_is_held(lock) -> None:
    """Waiting on a hold that never happens fails instead of waiting forever."""
    fault = lock.hang("set", 1)

    with pytest.raises(pytest.fail.Exception, match="no set held"):
        await fault.async_wait_held()


async def test_wait_held_fails_once_the_held_call_is_released(
    hass: HomeAssistant, lock
) -> None:
    """Having held a call once is not holding one now."""
    fault = lock.hang("set", 1, times=1)
    write = hass.async_create_task(lock.async_set_usercode(1, "9999"))
    await fault.async_wait_held()

    fault.clear()
    await _async_answer(write)

    with pytest.raises(pytest.fail.Exception, match="no set held"):
        await fault.async_wait_held()


async def test_wait_held_fails_once_the_held_call_is_cancelled(
    hass: HomeAssistant, lock
) -> None:
    """A held call its caller gave up on is held no longer, though the fault stays."""
    fault = lock.hang("set", 1)
    write = hass.async_create_task(lock.async_set_usercode(1, "9999"))
    await fault.async_wait_held()

    write.cancel()
    await async_run_until_quiet()

    assert write.cancelled()
    with pytest.raises(pytest.fail.Exception, match="no set held"):
        await fault.async_wait_held()


@pytest.mark.parametrize("kind", [FaultKind.HANG, FaultKind.DROP_REPLY])
async def test_clear_faults_releases_a_call_a_spent_fault_holds(
    hass: HomeAssistant, lock, kind
) -> None:
    """A fault whose ``times`` ran out matches nothing more, yet holds its last call."""
    fault = lock.add_fault(kind, "set", 1, times=1)
    write = hass.async_create_task(lock.async_set_usercode(1, "9999"))
    await fault.async_wait_held()
    assert fault not in lock.faults

    lock.clear_faults()

    await asyncio.gather(_async_answer(write), return_exceptions=True)
    assert write.done()


async def test_clear_faults_releases_held_calls_in_install_order(
    hass: HomeAssistant, lock
) -> None:
    """Held writes answer in the order their faults were installed."""
    slots = range(3, 23)
    faults = [lock.hang("set", slot) for slot in slots]
    writes = [
        hass.async_create_task(lock.async_set_usercode(slot, f"{slot:04}"))
        for slot in slots
    ]
    for fault in faults:
        await fault.async_wait_held()

    lock.clear_faults()
    await asyncio.gather(*writes)

    assert [call[0] for call in lock.service_calls["set_usercode"]] == list(slots)


async def test_a_held_read_answers_for_the_slots_managed_on_release(
    hass: HomeAssistant, lock
) -> None:
    """A slot managed while the read was held is in its answer, as empty."""
    fault = lock.hang("get")
    read = hass.async_create_task(lock.async_get_usercodes())
    await fault.async_wait_held()

    with patch.object(
        MockLCMLock, "managed_slots", new_callable=PropertyMock, return_value={1, 2, 3}
    ):
        fault.clear()
        answer = await _async_answer(read)

    assert answer[3] == SlotCredential.empty()


async def test_run_times_record_calls_when_they_reach_the_lock(
    freezer: FrozenDateTimeFactory, hass: HomeAssistant, lock
) -> None:
    """
    A failed call never reached the lock; a refused one did; a held one did on release.

    Pacing tests read the gaps between these, so a call recorded that never
    reached the lock, or recorded when it was asked rather than when it
    arrived, is a gap the lock never saw.
    """
    await async_advance_sync_ticks(hass, freezer)
    lock.run_times.clear()

    lock.fail("set", 1, times=1)
    with pytest.raises(LockOperationFailed):
        await lock.async_set_usercode(1, "9999")
    assert lock.run_times == []

    lock.refuse("set", 1, times=1)
    with pytest.raises(CodeRejectedError):
        await lock.async_set_usercode(1, "9999")
    assert lock.run_times == [time.monotonic()]

    fault = lock.hang("set", 1, times=1)
    write = hass.async_create_task(lock.async_set_usercode(1, "9999"))
    await fault.async_wait_held()
    freezer.tick(CLOCK_STEP)
    fault.clear()
    await _async_answer(write)
    assert lock.run_times[1:] == [time.monotonic()]


async def test_clearing_faults_releases_every_held_call(
    hass: HomeAssistant, lock
) -> None:
    """``clear_faults`` and ``release_all_faults`` both let a held call go."""
    first = lock.hang("set", 1)
    held_first = hass.async_create_task(lock.async_set_usercode(1, "9999"))
    await first.async_wait_held()
    lock.clear_faults()
    assert await _async_answer(held_first) is WriteResult.CONFIRMED
    assert not lock.faults

    second = lock.hang("clear", 2)
    held_second = hass.async_create_task(lock.async_clear_usercode(2))
    await second.async_wait_held()
    release_all_faults()
    assert await _async_answer(held_second) is True


async def test_a_hang_holds_a_sync_write_in_flight(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    A tick's write held at the lock leaves the slot syncing until released.

    ``settle=False`` is what lets the tick run while the write stays held:
    settling waits for every task, the held one included.
    """
    entry = lock_code_manager_config_entry
    lock = mock_lock(entry)
    in_sync = in_sync_entity_id(hass, entry, 1)
    await async_advance_sync_ticks(hass, freezer)
    await _async_change_pin(hass, "4321")
    fault = lock.hang("set", 1)

    await async_advance_sync_ticks(hass, freezer, settle=False)
    await fault.async_wait_held()

    assert sync_status(hass, in_sync) == SyncState.SYNCING.value
    assert lock.codes[1] == "1234"

    fault.clear()
    await async_tick_until(
        hass,
        freezer,
        lambda: hass.states.get(in_sync).state == STATE_ON,
        max_ticks=3,
        what="slot 1 in sync",
    )
    assert lock.codes[1] == "4321"


async def test_a_write_left_held_does_not_hang_teardown(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    End the test with a sync write still held, and leave it to the harness.

    Unloading the entry stops the sync managers, which wait on the in-flight
    tick, and on a frozen clock that wait never ends. The conftest hook
    releases every fault between the test body and teardown; without it this
    test passes its body and then hangs until pytest-timeout errors it.
    """
    entry = lock_code_manager_config_entry
    await async_advance_sync_ticks(hass, freezer)
    await _async_change_pin(hass, "4321")
    fault = mock_lock(entry).hang("set", 1)

    await async_advance_sync_ticks(hass, freezer, settle=False)

    await fault.async_wait_held()


async def test_teardown_releases_faults_before_the_fixtures_and_forgets_after(
    hass: HomeAssistant, lock, request: pytest.FixtureRequest
) -> None:
    """
    The teardown hook lets go of a held call before any fixture is torn down.

    The fixtures' finalizers -- the config entries' unload among them -- run
    where the hook yields, so a call still held there would hang them.
    Teardown runs even when setup failed, which is what lets go of a fault a
    fixture installed. A fault installed while the finalizers run is only
    forgotten once they end, so it cannot reach the next test; what it
    holds is left alone, since the event loop may be closed by then.
    ``test_a_write_left_held_does_not_hang_teardown`` is the end-to-end
    check that pytest runs the release before the finalizers.
    """
    fault = lock.hang("set", 1)
    write = hass.async_create_task(lock.async_set_usercode(1, "9999"))
    await fault.async_wait_held()

    teardown = pytest_runtest_teardown(request.node, None)
    next(teardown)
    assert await _async_answer(write) is WriteResult.CONFIRMED

    late = lock.hang("clear", 2)
    clear = hass.async_create_task(lock.async_clear_usercode(2))
    with pytest.raises(StopIteration):
        teardown.send(None)
    await late.async_wait_held()
    lock.clear_faults()
    await late.async_wait_held()

    late.clear()
    assert await _async_answer(clear) is True


async def test_a_fault_used_as_a_context_manager_clears_on_exit(lock) -> None:
    """Leaving the block puts the lock back to answering."""
    with lock.refuse("set", 1):
        with pytest.raises(CodeRejectedError):
            await lock.async_set_usercode(1, "9999")

    assert not lock.faults
    assert await lock.async_set_usercode(1, "9999") is WriteResult.CONFIRMED


@pytest.mark.parametrize("kind", [FaultKind.IGNORE, FaultKind.REFUSE])
@pytest.mark.parametrize("operation", ["get", None])
async def test_write_only_faults_need_a_write(lock, kind, operation) -> None:
    """A read cannot be ignored or refused, so asking for that is an error."""
    with pytest.raises(ValueError, match="set or clear"):
        lock.add_fault(kind, operation)


# -- Recording ----------------------------------------------------------------


async def test_noop_clears_are_recorded_apart_from_clears(lock) -> None:
    """A clear of an empty slot is counted, and never as a clear that changed it."""
    assert await lock.async_clear_usercode(3) is False
    assert await lock.async_clear_usercode(3) is False
    assert await lock.async_clear_usercode(1) is True

    assert lock.service_calls["noop_clear_usercode"] == [(3,), (3,)]
    assert lock.service_calls["clear_usercode"] == [(1,)]


async def test_provider_setup_is_recorded(
    hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry
) -> None:
    """Each lock's provider setup ran once, for this entry."""
    entry = lock_code_manager_config_entry
    for lock_entity_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID):
        assert mock_lock(entry, lock_entity_id).service_calls["setup"] == [
            (entry.entry_id,)
        ]


def _push_lock(entry: MockConfigEntry, lock_entity_id: str) -> MockLCMPushLock:
    """Return the push mock an entry drives for ``lock_entity_id``."""
    lock = mock_lock(entry, lock_entity_id)
    assert isinstance(lock, MockLCMPushLock), f"{lock_entity_id} is not a push mock"
    return lock


async def test_push_subscriptions_are_counted(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Setting an entry up subscribes once per lock; unloading it unsubscribes."""
    entry = MockConfigEntry(domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title")
    entry.add_to_hass(hass)
    with patch.dict(
        "custom_components.lock_code_manager.providers.INTEGRATIONS_CLASS_MAP",
        {"test": MockLCMPushLock},
    ):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    locks = [
        _push_lock(entry, lock_id) for lock_id in (LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID)
    ]
    assert [(lock.subscribe_calls, lock.unsubscribe_calls) for lock in locks] == [
        (1, 0),
        (1, 0),
    ]

    locks[0].subscribe_push_updates()
    assert locks[0].subscribe_calls == 2

    await hass.config_entries.async_unload(entry.entry_id)
    assert [lock.unsubscribe_calls for lock in locks] == [1, 1]
