"""
Tests for the test harness itself: clock helpers, the time-driven tick, fault knobs.

A helper that silently does nothing makes every test built on it pass for
the wrong reason, so each one is shown here to do what it says -- and to
fail loudly, rather than hang, when what it waits for never comes.
"""

import asyncio
from collections.abc import Callable
import logging
import threading
import time
from typing import Any
from unittest.mock import PropertyMock, patch

import freezegun.api
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.components.text import (
    ATTR_VALUE,
    DOMAIN as TEXT_DOMAIN,
    SERVICE_SET_VALUE as TEXT_SERVICE_SET_VALUE,
)
from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_ON,
    STATE_UNAVAILABLE,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from custom_components import lock_code_manager
from custom_components.lock_code_manager.const import (
    BUS_EVENT_CREDENTIAL_USED,
    CONF_LOCKS,
    DOMAIN,
)
from custom_components.lock_code_manager.ha.runtime import lock_registry
from custom_components.lock_code_manager.model.credentials import (
    SlotCredential,
    WriteResult,
)
from custom_components.lock_code_manager.model.exceptions import (
    CodeRejectedError,
    LockDisconnected,
    LockOperationFailed,
    ProviderNotImplementedError,
)
from custom_components.lock_code_manager.providers._base import MIN_OPERATION_DELAY
from custom_components.lock_code_manager.sync.slot import LIMIT, Status

from .common import (
    BASE_CONFIG,
    CLOCK_STEP,
    CONFIG_ENTRY_VERSION,
    LIFELONG_TASKS,
    LOCK_1_ENTITY_ID,
    LOCK_2_ENTITY_ID,
    MOCK_CALENDARS_KEY,
    NO_PIN_CAPABILITIES,
    PIN_CAPABILITIES,
    SLOT_1_PIN_ENTITY,
    TICK_INTERVAL,
    ExecutorWorkStuck,
    FaultKind,
    LiveMockCalendars,
    MockLCMLock,
    MockLCMPushLock,
    _async_step_timers_to,
    async_advance_across_deadline,
    async_advance_clock,
    async_advance_sync_ticks,
    async_fire_mock_lock_use,
    async_run_until_quiet,
    async_settle_background,
    async_step_clock_until,
    async_tick_until,
    current_entry,
    in_sync_entity_id,
    mock_lock,
    release_all_faults,
    sync_status,
    when_mock_lock_built,
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


async def test_settling_waits_out_executor_work_without_spending_steps(
    freezer: FrozenDateTimeFactory, hass: HomeAssistant
) -> None:
    """
    Work on an executor thread is waited out in real time, not in clock steps.

    The thread takes far longer than two steps of the loop, and the clock
    does not move while it works.
    """

    async def _waits_on_a_thread() -> None:
        await hass.async_add_executor_job(time.sleep, 0.3)

    waiter = hass.async_create_task(_waits_on_a_thread())
    before = time.monotonic()

    await async_step_clock_until(
        hass, freezer, waiter.done, max_steps=2, what="the thread"
    )

    assert waiter.done()
    assert time.monotonic() == before


async def test_a_wait_on_the_clock_still_fails_in_steps_and_fast(
    freezer: FrozenDateTimeFactory, hass: HomeAssistant
) -> None:
    """With no executor work in flight, a wait the clock never reaches fails fast."""
    sleeper = hass.async_create_task(asyncio.sleep(3600))
    started = freezegun.api.real_monotonic()

    with pytest.raises(pytest.fail.Exception, match="after 50 clock steps"):
        await async_step_clock_until(
            hass, freezer, sleeper.done, max_steps=50, what="the sleeper"
        )

    assert freezegun.api.real_monotonic() - started < 1


async def test_a_stuck_thread_fails_as_stuck_executor_work(
    freezer: FrozenDateTimeFactory, hass: HomeAssistant
) -> None:
    """Executor work past its cap fails as that, not as the code under test hanging."""
    release = threading.Event()
    stuck = hass.async_add_executor_job(release.wait)
    try:
        with (
            patch("tests.common.EXECUTOR_WORK_CAP_SECONDS", 0.2),
            pytest.raises(ExecutorWorkStuck, match="still running after"),
        ):
            await async_step_clock_until(
                hass, freezer, stuck.done, max_steps=50, what="the stuck thread"
            )
    finally:
        release.set()
        await stuck


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


async def test_settle_background_leaves_a_lifelong_task_running(
    freezer: FrozenDateTimeFactory, hass: HomeAssistant
) -> None:
    """A registered listen loop never finishes, so settling neither waits on it nor ends it."""
    listening = hass.async_create_background_task(
        asyncio.Event().wait(), "zwave_js_entry_client_listen"
    )
    LIFELONG_TASKS.add(listening)
    sleeper = hass.async_create_background_task(asyncio.sleep(0.1), "sleeper")
    try:
        await async_settle_background(hass, freezer)

        assert sleeper.done()
        assert not listening.done()
    finally:
        LIFELONG_TASKS.discard(listening)
        listening.cancel()
        await asyncio.gather(listening, return_exceptions=True)


async def test_settle_background_waits_on_a_listen_loop_nobody_registered(
    freezer: FrozenDateTimeFactory, hass: HomeAssistant
) -> None:
    """Only registered tasks are skipped: one stuck under the same name is waited on."""
    stuck = hass.async_create_background_task(
        asyncio.Event().wait(), "zwave_js_entry_client_listen"
    )

    with pytest.raises(pytest.fail.Exception, match="background lock work"):
        await async_settle_background(hass, freezer)

    assert stuck.cancelled()


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


async def test_advancing_the_clock_fires_each_timer_at_its_own_time(
    freezer: FrozenDateTimeFactory, hass: HomeAssistant
) -> None:
    """Both clocks move the whole way, and a timer on the way fires when it is due."""
    loop = asyncio.get_running_loop()
    start, wall = loop.time(), dt_util.utcnow()
    fired: list[float] = []
    async_call_later(hass, 30, callback(lambda _now: fired.append(loop.time())))

    await async_advance_clock(hass, freezer, 45)

    [fired_at] = fired
    assert start + 30 <= fired_at < start + 30 + CLOCK_STEP
    assert loop.time() == pytest.approx(start + 45, abs=CLOCK_STEP)
    assert (dt_util.utcnow() - wall).total_seconds() == pytest.approx(
        45, abs=CLOCK_STEP
    )


async def test_advancing_the_clock_far_is_not_a_runaway_timer(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    An hour of sync ticks is more timers than one step allows, and still not a failure.

    The bound on timers is per sync tick's worth of clock, so it still
    catches a timer rearming itself, while a long wait -- an operation
    deadline, the stop grace -- gets through.
    """
    await async_advance_sync_ticks(hass, freezer)
    start = time.monotonic()

    await async_advance_clock(hass, freezer, 3600)

    assert time.monotonic() - start == pytest.approx(3600, abs=CLOCK_STEP)


async def test_ticking_until_without_settling_leaves_a_held_write_held(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """``settle=False`` reaches the condition with a write held, where settling would fail."""
    entry = lock_code_manager_config_entry
    in_sync = in_sync_entity_id(hass, entry, 1)
    await async_advance_sync_ticks(hass, freezer)
    await _async_change_pin(hass, "4321")
    fault = mock_lock(entry).hang("set", 1)

    await async_tick_until(
        hass,
        freezer,
        lambda: sync_status(hass, in_sync) == "syncing",
        max_ticks=2,
        what="slot 1 syncing",
        settle=False,
    )

    await fault.async_wait_held()


async def test_a_deadline_is_bracketed_by_a_tick_either_side(
    freezer: FrozenDateTimeFactory, hass: HomeAssistant
) -> None:
    """Held a tick short of the deadline and let go a tick past it passes."""
    fired: list[Any] = []
    started_at = time.monotonic()

    @callback
    def _fire(now: Any) -> None:
        fired.append(now)

    async_call_later(hass, 60, _fire)

    await async_advance_across_deadline(
        hass,
        freezer,
        started_at=started_at,
        deadline=60,
        held=lambda: not fired,
        what="the timer",
    )

    assert time.monotonic() - started_at == pytest.approx(
        60 + TICK_INTERVAL.total_seconds(), abs=CLOCK_STEP
    )


@pytest.mark.parametrize(
    ("fires_after", "deadline", "message"),
    [
        pytest.param(30, 60, "let go a tick before", id="cut-short"),
        pytest.param(90, 60, "still held a tick after", id="unbounded"),
        pytest.param(90, 1, "under a tick", id="deadline-within-a-tick"),
    ],
)
async def test_a_deadline_bracket_fails_on_either_side(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    fires_after: float,
    deadline: float,
    message: str,
) -> None:
    """A wait that ends early, one that outlasts its deadline, or a deadline too near to check."""
    fired: list[Any] = []

    @callback
    def _fire(now: Any) -> None:
        fired.append(now)

    unsub = async_call_later(hass, fires_after, _fire)

    with pytest.raises(pytest.fail.Exception, match=message):
        await async_advance_across_deadline(
            hass,
            freezer,
            started_at=time.monotonic(),
            deadline=deadline,
            held=lambda: not fired,
            what="the timer",
        )
    unsub()


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


# -- Settling waits for the lock worker ---------------------------------------


async def _async_change_pin(hass: HomeAssistant, pin: str) -> None:
    """Set slot 1's PIN through its text entity."""
    await hass.services.async_call(
        TEXT_DOMAIN,
        TEXT_SERVICE_SET_VALUE,
        {ATTR_VALUE: pin},
        target={ATTR_ENTITY_ID: SLOT_1_PIN_ENTITY},
        blocking=True,
    )


async def test_block_till_done_waits_for_the_lock_worker(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    Settling, with no time moved, lets the worker write a changed PIN and read it back.

    ``hass.async_block_till_done`` waits on the lock workers' queues too, so
    a test that settles sees the sync the change set off finished.
    """
    entry = lock_code_manager_config_entry
    lock = mock_lock(entry)
    in_sync = in_sync_entity_id(hass, entry, 1)
    await async_advance_sync_ticks(hass, freezer)
    lock.service_calls.clear()

    await _async_change_pin(hass, "4321")
    await hass.async_block_till_done(wait_background_tasks=True)

    assert hass.states.get(in_sync).state == STATE_ON
    assert lock.codes[1] == "4321"
    assert lock.service_calls["set_usercode"] == [(1, "4321", "test1")]
    assert not lock_registry(hass).worker(LOCK_1_ENTITY_ID)._queue._unfinished_tasks


async def test_a_timer_due_within_a_tick_fires_at_its_own_time(
    freezer: FrozenDateTimeFactory, hass: HomeAssistant
) -> None:
    """
    A timer due half a second in fires then, before one due a second later.

    The helper stops the clock at each timer on the way to the end of the
    step, so each fires at its own time and in due order.
    """
    loop = asyncio.get_running_loop()
    start = loop.time()
    seen: list[tuple[str, float]] = []

    # Callbacks run on the loop: a plain function would go to an executor
    # thread, which a busy runner can start only after a later jump.
    async_call_later(
        hass, 0.5, callback(lambda _now: seen.append(("early", loop.time())))
    )
    async_call_later(
        hass, 1.5, callback(lambda _now: seen.append(("late", loop.time())))
    )
    await async_advance_sync_ticks(hass, freezer)

    assert [name for name, _ in seen] == ["early", "late"]
    [(_, early_at), (_, late_at)] = seen
    assert start + 0.5 <= early_at < start + 0.5 + CLOCK_STEP
    assert start + 1.5 <= late_at < start + 1.5 + CLOCK_STEP


# -- Holds through real failures ----------------------------------------------


async def test_a_slot_is_held_on_real_failures_and_a_new_pin_resumes_it(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    A lock that acknowledges a PIN it never keeps holds the slot; a new PIN resumes it.

    Nothing is forced: what holds the slot is the worker's own accounting of
    syncs that ended out of sync against a lock that really answered.
    """
    entry = lock_code_manager_config_entry
    lock = mock_lock(entry)
    in_sync = in_sync_entity_id(hass, entry, 1)
    issue_id = f"slot_suspended_{entry.entry_id}_{LOCK_1_ENTITY_ID}_1"
    await async_advance_sync_ticks(hass, freezer)
    fault = lock.ignore("set", 1)

    await _async_change_pin(hass, "4321")
    await async_settle_background(hass, freezer)

    assert fault.hits == LIMIT
    assert sync_status(hass, in_sync) == Status.SUSPENDED.value
    assert ir.async_get(hass).async_get_issue(DOMAIN, issue_id) is not None
    assert lock.codes[1] == "1234"

    # Held means no more attempts, however long the lock is left alone.
    await async_advance_sync_ticks(hass, freezer, 5)
    assert fault.hits == LIMIT

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


async def test_a_lock_that_answers_nothing_backs_off_and_recovers(
    freezer: FrozenDateTimeFactory,
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
) -> None:
    """
    A lock that answers nothing is backed off, its slot left out of sync; answering brings it back.

    Every call failing is what makes the lock unreachable, and nothing is
    charged to the slot while it is: the probe that gets an answer after the
    backoff syncs it.
    """
    entry = lock_code_manager_config_entry
    lock = mock_lock(entry)
    worker = lock_registry(hass).worker(LOCK_1_ENTITY_ID)
    in_sync = in_sync_entity_id(hass, entry, 1)
    await async_advance_sync_ticks(hass, freezer)

    with lock.fail(None, error=LockDisconnected):
        await _async_change_pin(hass, "4321")
        await async_settle_background(hass, freezer)

        assert not worker.reachable
        assert sync_status(hass, in_sync) == Status.OUT_OF_SYNC.value
        assert lock.codes[1] == "1234"

    await async_tick_until(
        hass,
        freezer,
        lambda: hass.states.get(in_sync).state == STATE_ON,
        max_ticks=60,
        what="slot 1 back in sync",
    )
    assert worker.reachable
    assert lock.codes[1] == "4321"


# -- Fault knobs ----------------------------------------------------------------


@pytest.fixture
def lock(hass: HomeAssistant, mock_lock_config_entry, lock_code_manager_config_entry):
    """
    Return lock 1's mock, for calling the fake directly, with its starting codes.

    Setting the entry up syncs it: slot 2's calendar is off, so the worker
    clears the code it starts with. The tests here call the fake directly
    and move no time, so the table is put back as the mock starts it.
    """
    lock = mock_lock(lock_code_manager_config_entry)
    lock.codes = {1: "1234", 2: "5678"}
    lock.service_calls.clear()
    lock.call_order.clear()
    return lock


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

    assert sync_status(hass, in_sync) == "syncing"
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


async def test_slow_takes_its_seconds_on_the_clock_then_answers(
    freezer: FrozenDateTimeFactory, hass: HomeAssistant, lock
) -> None:
    """Held while it waits, it reaches the lock once the clock has moved that far."""
    await async_advance_sync_ticks(hass, freezer)
    fault = lock.slow("set", 1, seconds=30, times=1)
    write = hass.async_create_task(lock.async_set_usercode(1, "9999"))
    await fault.async_wait_held()

    await async_advance_clock(hass, freezer, 29, settle=False)
    assert not write.done()
    assert lock.codes[1] == "1234"
    await async_advance_clock(hass, freezer, 1, settle=False)

    assert await _async_answer(write) is WriteResult.CONFIRMED
    assert lock.codes[1] == "9999"
    assert not fault.held


async def test_clearing_a_slow_fault_ends_its_wait(hass: HomeAssistant, lock) -> None:
    """The call answers without the clock moving, so teardown can let it go."""
    fault = lock.slow("set", 1, seconds=30)
    write = hass.async_create_task(lock.async_set_usercode(1, "9999"))
    await fault.async_wait_held()

    fault.clear()

    assert await _async_answer(write) is WriteResult.CONFIRMED


async def test_setup_and_probe_faults_match_only_by_name(lock) -> None:
    """A fault naming no operation leaves setup and the probe alone; naming them reaches them."""
    lock.fail(None)

    await lock.async_setup(lock._lcm_config_entry)
    assert await lock.async_is_device_available() is True
    lock.clear_faults()

    lock.fail("setup", error=LockDisconnected, times=1)
    lock.fail("probe", times=1)
    with pytest.raises(LockDisconnected, match="injected setup failure"):
        await lock.async_setup(lock._lcm_config_entry)
    with pytest.raises(LockOperationFailed, match="injected probe failure"):
        await lock.async_is_device_available()
    assert await lock.async_is_device_available() is True


async def test_a_hung_probe_holds_the_availability_check(
    hass: HomeAssistant, lock
) -> None:
    """The check waits until the fault is cleared, and never reaches the code table."""
    fault = lock.hang("probe")
    check = hass.async_create_task(lock.async_is_device_available())
    await fault.async_wait_held()
    assert not check.done()

    fault.clear()
    assert await _async_answer(check) is True


async def test_when_mock_lock_built_acts_on_each_lock_the_entry_builds(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    Each lock built inside gets the act; none built after does.

    The entry's own locks, and the throwaway ones it reads through.
    """
    entry = MockConfigEntry(domain=DOMAIN, data=BASE_CONFIG, unique_id="Built")
    entry.add_to_hass(hass)
    with when_mock_lock_built(lambda built: built.lock.entity_id) as built:
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert set(built) == {LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID}
    built_inside = list(built)

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert built == built_inside
    await hass.config_entries.async_unload(entry.entry_id)


@pytest.mark.paced
async def test_a_paced_test_keeps_the_real_delay_between_operations(
    freezer: FrozenDateTimeFactory, hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """Under the marker, the second of two operations waits the providers' delay."""
    entry = MockConfigEntry(domain=DOMAIN, data=BASE_CONFIG, unique_id="Paced")
    entry.add_to_hass(hass)
    setup = hass.async_create_task(hass.config_entries.async_setup(entry.entry_id))
    await async_advance_clock(hass, freezer, 5 * MIN_OPERATION_DELAY, settle=False)
    assert setup.done()
    lock = mock_lock(entry)
    await async_advance_clock(hass, freezer, 5 * MIN_OPERATION_DELAY, settle=False)
    lock.run_times.clear()

    worker = lock_registry(hass).worker(LOCK_1_ENTITY_ID)
    reads = hass.async_create_task(worker.read())
    await async_run_until_quiet()
    second = hass.async_create_task(worker.read())
    await async_advance_clock(hass, freezer, 2 * MIN_OPERATION_DELAY, settle=False)

    assert reads.done()
    assert second.done()
    first_at, second_at = lock.run_times
    assert second_at - first_at == pytest.approx(MIN_OPERATION_DELAY, abs=CLOCK_STEP)
    unload = hass.async_create_task(hass.config_entries.async_unload(entry.entry_id))
    await async_advance_clock(hass, freezer, 5 * MIN_OPERATION_DELAY, settle=False)
    assert unload.done()


@pytest.mark.parametrize("operation", ["setup", "probe"])
async def test_a_setup_or_probe_fault_cannot_name_a_slot(lock, operation) -> None:
    """Neither touches a slot, so a fault naming one would match nothing."""
    with pytest.raises(ValueError, match="touches no slot"):
        lock.fail(operation, 1)


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


async def test_noop_sets_are_recorded_apart_from_sets(lock) -> None:
    """A set of the code a slot holds is counted, and never as a set that changed it."""
    assert await lock.async_set_usercode(1, "1234", "test1") is WriteResult.NO_CHANGE
    assert await lock.async_set_usercode(1, "9999", "test1") is WriteResult.CONFIRMED

    assert lock.service_calls["noop_set_usercode"] == [(1, "1234", "test1")]
    assert lock.service_calls["set_usercode"] == [(1, "9999", "test1")]


async def test_call_order_names_each_recorded_call_in_turn(lock) -> None:
    """Reads, sets and clears, as they reached the lock, the ones changing nothing included."""
    await lock.async_get_usercodes()
    await lock.async_set_usercode(1, "1234")
    await lock.async_clear_usercode(2)

    assert lock.call_order[-3:] == [
        "get_usercodes",
        "noop_set_usercode",
        "clear_usercode",
    ]


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


async def test_a_subscribe_error_set_as_the_lock_is_built_is_raised_uncounted(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    A build act can set ``subscribe_error``, and every subscription then raises it.

    It survives the push lock's own initializer, and a subscription it stops
    is not counted; cleared, subscribing counts again.
    """
    error = LockDisconnected("subscription refused")

    def _refuse_subscriptions(lock: MockLCMPushLock) -> MockLCMPushLock:
        lock.subscribe_error = error
        return lock

    entry = current_entry("Refuses Subscriptions", {1: "1234"})
    entry.add_to_hass(hass)
    with (
        patch.dict(
            "custom_components.lock_code_manager.providers.INTEGRATIONS_CLASS_MAP",
            {"test": MockLCMPushLock},
        ),
        when_mock_lock_built(_refuse_subscriptions) as built,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    [lock] = built
    assert lock.subscribe_error is error
    assert lock.subscribe_calls == 0
    with pytest.raises(LockDisconnected):
        lock.setup_push_subscription()
    assert lock.subscribe_calls == 0

    lock.subscribe_error = None
    lock.setup_push_subscription()
    assert lock.subscribe_calls == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_a_push_subscription_hears_its_lock_until_it_unsubscribes(
    hass: HomeAssistant, mock_lock_config_entry
) -> None:
    """
    A use the lock integration reports reaches a subscribed push mock, once.

    Subscribing twice still listens once, and unsubscribing stops it.
    """
    uses: list[Event] = []
    hass.bus.async_listen(BUS_EVENT_CREDENTIAL_USED, uses.append)
    entry = current_entry("Hears Uses", {1: "1234"})
    entry.add_to_hass(hass)
    with patch.dict(
        "custom_components.lock_code_manager.providers.INTEGRATIONS_CLASS_MAP",
        {"test": MockLCMPushLock},
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    lock = _push_lock(entry, LOCK_1_ENTITY_ID)
    lock.setup_push_subscription()

    async_fire_mock_lock_use(hass, LOCK_2_ENTITY_ID, 1)
    async_fire_mock_lock_use(hass, LOCK_1_ENTITY_ID, 1)
    await hass.async_block_till_done()
    assert [event.data["config_entry_id"] for event in uses] == [entry.entry_id]

    lock.teardown_push_subscription()
    async_fire_mock_lock_use(hass, LOCK_1_ENTITY_ID, 1)
    await hass.async_block_till_done()
    assert len(uses) == 1
    assert await hass.config_entries.async_unload(entry.entry_id)


async def test_the_mock_lock_entry_reloads_with_its_calendars(
    hass: HomeAssistant,
    mock_lock_config_entry,
    mock_calendars: LiveMockCalendars,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Reloading the mock lock integration brings its calendars back, events kept.

    The fixture follows the reload: an event made through it afterwards is on
    the calendar Home Assistant has now.
    """
    start = dt_util.utcnow()
    before_reload = mock_calendars[0]
    before_reload.create_event(
        dtstart=start, dtend=start + TICK_INTERVAL, summary="kept"
    )
    caplog.clear()

    assert await hass.config_entries.async_reload(mock_lock_config_entry.entry_id)
    await hass.async_block_till_done()

    assert [
        record for record in caplog.records if record.levelno >= logging.ERROR
    ] == []
    live = hass.data[MOCK_CALENDARS_KEY]
    assert mock_calendars[0] is live[0] is not before_reload
    for calendar in mock_calendars:
        state = hass.states.get(calendar.entity_id)
        assert state is not None
        assert state.state != STATE_UNAVAILABLE

    mock_calendars[1].create_event(
        dtstart=start, dtend=start + TICK_INTERVAL, summary="after"
    )
    assert [event.summary for event in live[0]._events] == ["kept"]
    assert [event.summary for event in live[1]._events] == ["after"]
    assert hass.states.get(live[1].entity_id).attributes["message"] == "after"


async def test_a_lock_reports_no_capabilities_unless_given_some(lock) -> None:
    """The mock is slot-only until a test gives it capabilities to report."""
    assert not lock.supports_native_users
    with pytest.raises(ProviderNotImplementedError):
        await lock.async_get_capabilities()

    lock.capabilities = NO_PIN_CAPABILITIES
    assert lock.supports_native_users
    assert await lock.async_get_capabilities() is NO_PIN_CAPABILITIES
    lock.capabilities = PIN_CAPABILITIES
    assert await lock.async_get_capabilities() is PIN_CAPABILITIES


@pytest.mark.parametrize(
    ("make_entry", "migrated", "read_for_migration"),
    [
        pytest.param(
            lambda: current_entry("Current", {1: "1234", 3: "0123"}),
            False,
            0,
            id="current",
        ),
        # Both of its locks are read to migrate it.
        pytest.param(
            lambda: MockConfigEntry(
                domain=DOMAIN, data=BASE_CONFIG, unique_id="Old Shape", version=1
            ),
            True,
            2,
            id="old_shape",
        ),
    ],
)
async def test_a_current_entry_sets_up_without_a_migration(
    hass: HomeAssistant,
    mock_lock_config_entry,
    make_entry: Callable[[], MockConfigEntry],
    migrated: bool,
    read_for_migration: int,
) -> None:
    """
    Only an entry in an older shape is migrated, and has its locks read for it.

    Home Assistant asks the integration to migrate an entry whose version is
    not the current one, and only then. A migration from before version 4
    also reads each lock, through an instance of its own built before the
    ones the entry keeps; an entry already current builds just those, and
    the first call each makes to its lock is the entry's setup. The
    old-shape case shows both checks do see a migration.
    """
    entry = make_entry()
    entry.add_to_hass(hass)
    assert (entry.version == CONFIG_ENTRY_VERSION) is not migrated
    with (
        patch.object(
            lock_code_manager,
            "async_migrate_entry",
            wraps=lock_code_manager.async_migrate_entry,
        ) as migrate,
        when_mock_lock_built(lambda lock: lock) as built,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    assert migrate.called is migrated
    kept = [mock_lock(entry, lock_id) for lock_id in entry.data[CONF_LOCKS]]
    # By identity: two instances of one lock compare equal.
    migration_reads = [lock for lock in built if not any(lock is k for k in kept)]
    assert len(migration_reads) == read_for_migration
    assert all(lock.call_order == ["get_usercodes"] for lock in migration_reads)
    assert all(lock.call_order[0] == "setup" for lock in kept)
    assert entry.version == CONFIG_ENTRY_VERSION
    assert await hass.config_entries.async_unload(entry.entry_id)
