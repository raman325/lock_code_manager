"""Common constants for tests."""

import asyncio
from collections import defaultdict
from collections.abc import (
    Awaitable,
    Callable,
    Collection,
    Iterable,
    Iterator,
    Sequence,
)
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
import json
import math
import sys
import time
import traceback
from typing import Any, Literal, Protocol, Self
from unittest.mock import patch
import weakref

import freezegun.api
from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockModule,
    MockPlatform,
    async_fire_mqtt_message,
    mock_integration,
    mock_platform,
)

from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.components.calendar import (
    DOMAIN as CALENDAR_DOMAIN,
    CalendarEntity,
    CalendarEvent,
)
from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN, LockEntity
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN
from homeassistant.components.text import (
    ATTR_VALUE as TEXT_ATTR_VALUE,
    DOMAIN as TEXT_DOMAIN,
    SERVICE_SET_VALUE as TEXT_SERVICE_SET_VALUE,
)
from homeassistant.config_entries import SOURCE_USER, ConfigEntry, ConfigSubentryData
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_ENABLED,
    CONF_ENTITY_ID,
    CONF_NAME,
    CONF_PIN,
    EVENT_HOMEASSISTANT_FINAL_WRITE,
    Platform,
)
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import slugify

from custom_components.lock_code_manager.config_flow import LockCodeManagerFlowHandler
from custom_components.lock_code_manager.const import (
    ATTR_CODE,
    ATTR_IN_SYNC,
    ATTR_PIN_IN_SYNC,
    ATTR_SYNC_STATUS,
    CONF_LOCKS,
    CONF_SLOT,
    CONF_SLOTS,
    CONF_USERS,
    DOMAIN,
    SUBENTRY_TYPE_USER,
)
from custom_components.lock_code_manager.ha.allocation import build_lock_instance
from custom_components.lock_code_manager.ha.runtime import (
    CORE_KEYS,
    REGISTRY_KEY,
    async_setup_core,
)
from custom_components.lock_code_manager.model.config import (
    EntryConfig,
    async_write_entry_config,
    build_slot_unique_id,
)
from custom_components.lock_code_manager.model.credentials import (
    CredentialType,
    CredentialTypeCapability,
    LockCapabilities,
    SlotCredential,
    WriteResult,
)
from custom_components.lock_code_manager.model.exceptions import (
    CodeRejectedError,
    LockDisconnected,
    LockOperationFailed,
)
from custom_components.lock_code_manager.model.queries import get_entry_config
from custom_components.lock_code_manager.model.slot_assignment import identity
from custom_components.lock_code_manager.providers import BaseLock
from custom_components.lock_code_manager.sync.worker import LockWorker

LOCK_1_ENTITY_ID = "lock.test_1"
LOCK_2_ENTITY_ID = "lock.test_2"

BASE_CONFIG = {
    CONF_LOCKS: [LOCK_1_ENTITY_ID, LOCK_2_ENTITY_ID],
    CONF_SLOTS: {
        1: {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True},
        2: {
            CONF_NAME: "test2",
            CONF_PIN: "5678",
            CONF_ENABLED: True,
            # Legacy field name; the migration in async_setup_entry strips it.
            "number_of_uses": 5,
            CONF_ENTITY_ID: "calendar.test_1",
        },
    },
}

# Two plain slots on one lock, holding the codes the mock lock starts with.
TWO_SLOTS_ON_LOCK_1 = {
    CONF_LOCKS: [LOCK_1_ENTITY_ID],
    CONF_SLOTS: {
        1: {CONF_NAME: "test1", CONF_PIN: "1234", CONF_ENABLED: True},
        2: {CONF_NAME: "test2", CONF_PIN: "5678", CONF_ENABLED: True},
    },
}


# What a lock with room for ten PINs reports, and one with no room for any:
# a Z-Wave lock whose interview finished while it slept reports the second.
PIN_CAPABILITIES = LockCapabilities(
    supports_user_management=False,
    max_users=10,
    credential_types={
        CredentialType.PIN: CredentialTypeCapability(
            num_slots=10, min_length=4, max_length=8, supports_learn=False
        )
    },
)
NO_PIN_CAPABILITIES = LockCapabilities(
    supports_user_management=False, max_users=10, credential_types={}
)


UNCLAIMED_IDENTIFIER = "somebridge_1"
UNCLAIMED_UNIQUE_ID = f"{UNCLAIMED_IDENTIFIER}_lock"


async def async_disable_and_reload(
    hass: HomeAssistant, config_entry: ConfigEntry, entity_id: str
) -> None:
    """Disable an entity in the registry and reload the entry, so it never loads."""
    er.async_get(hass).async_update_entity(
        entity_id, disabled_by=er.RegistryEntryDisabler.USER
    )
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id) is None


async def async_enable_and_reload(
    hass: HomeAssistant, config_entry: ConfigEntry, entity_id: str
) -> None:
    """Enable a registry-disabled entity and reload the entry, so it loads."""
    er.async_get(hass).async_update_entity(entity_id, disabled_by=None)
    await hass.config_entries.async_reload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id) is not None


def async_blocking_stub(
    original: Callable[..., Awaitable[Any]] | None = None,
) -> tuple[Callable[..., Awaitable[Any]], asyncio.Event, asyncio.Event]:
    """
    Return ``(stub, entered, release)`` for patching an awaited call mid-flight.

    ``stub`` accepts any arguments, sets ``entered``, then waits on
    ``release``. A caller that never releases it has a device that has
    stopped answering. Once released it forwards its arguments to
    ``original`` when one is given.
    """
    entered, release = asyncio.Event(), asyncio.Event()

    async def stub(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        await release.wait()
        if original is None:
            return None
        return await original(*args, **kwargs)

    return stub, entered, release


# The old sync tick's interval: the unit the time helpers still step in.
TICK_INTERVAL = timedelta(seconds=2)


# -- Driving time ---------------------------------------------------------
#
# The ``freezer`` fixture moves ``time.monotonic`` AND the event loop's clock
# (the harness patches the loop's monotonic source), so ``asyncio.sleep``,
# ``loop.call_at`` timers, pending-write deadlines and the breaker windows all
# follow it. A frozen loop clock also stalls every sleep until the test steps
# it, so ``hass.async_block_till_done`` on a frozen clock can wait forever on
# one. Every helper here steps in bounded loops instead, and fails the test
# naming what it waited for.

# Not a divisor of the tick interval or of the delays the pacing tests set, so
# no step lands exactly on a due time: frozen monotonic readings are
# epoch-sized floats, and a step meeting the due time exactly could round
# either way.
CLOCK_STEP = 0.003
# Bounds on stepping the clock, in steps. Running the test's own operations
# takes a few; letting the work they set off reach the lock takes a few per
# operation. Both stay far short of the two-second sync tick, so settling
# never runs a sync tick of its own.
_MAX_STEPS_FOR_OPERATIONS = 50
_MAX_STEPS_FOR_BACKGROUND = 200
# A guard against something rescheduling itself forever, not a tuning knob:
# the loop goes quiet within a handful of turns here.
_MAX_TURNS_TO_QUIET = 1000
# The same guard for timers: an interval holds a few each for the sync
# ticks, the coordinator's poll, confirmation reads and pending-write
# deadlines.
_MAX_TIMERS_PER_TICK = 1000
# How far past a timer's due time a jump to it lands. The frozen clock moves
# in whole microseconds, so a jump to the exact due time can land just short
# and leave the timer unfired.
_PAST_DUE = 2e-6


async def async_run_until_quiet() -> None:
    """
    Yield until the event loop has nothing else ready to run.

    A wait that comes due wakes its task, the task runs its operation and
    wakes the next waiter, and that one reads the clock and starts its own
    wait -- a loop turn per hop. Running until nothing is ready finishes the
    whole chain at the current time, however many turns it takes.
    """
    loop = asyncio.get_running_loop()
    for _ in range(_MAX_TURNS_TO_QUIET):
        await asyncio.sleep(0)
        if not loop._ready:  # type: ignore[attr-defined]
            return
    pytest.fail(f"event loop still busy after {_MAX_TURNS_TO_QUIET} turns")


# Tasks an integration under test runs for as long as it is loaded -- the
# Z-Wave JS client's listen loop -- registered by the fixture that starts them,
# for as long as they run. They never finish, so waiting for them would never
# end, and cancelling them would take the integration down under the test.
# Any other task, a listen loop nobody registered included, is waited on.
LIFELONG_TASKS: set[asyncio.Future[Any]] = set()


def unfinished_tasks(hass: HomeAssistant) -> list[asyncio.Task[Any]]:
    """Return the tasks Home Assistant tracks that have not finished, lifelong ones aside."""
    # Executor jobs are tracked here too, as bare futures; membership needs
    # nothing a future lacks.
    return [
        task
        for task in hass._tasks | hass._background_tasks
        if not task.done() and task not in LIFELONG_TASKS
    ]


# Executor work each event loop has handed a thread and not yet had back.
# Counted per loop, and only once the result is back on the loop -- not when
# the thread finishes, which comes a moment before the loop hears of it -- so
# a loop that is quiet with nothing counted is waiting on the clock or on
# nothing, never on a thread. ``count_executor_work`` does the counting.
_EXECUTOR_WORK: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, int] = (
    weakref.WeakKeyDictionary()
)
# How long, in real seconds, executor work may run while a test waits on it.
# Generous: it only has to separate a slow machine from a stuck thread.
EXECUTOR_WORK_CAP_SECONDS = 60.0
# How long the loop's thread sleeps between looks at executor work, so the
# threads get the processor.
_EXECUTOR_POLL_SECONDS = 0.001


class ExecutorWorkStuck(Exception):
    """Executor work still running after ``EXECUTOR_WORK_CAP_SECONDS``."""


@contextmanager
def count_executor_work() -> Iterator[None]:
    """
    Count the work every event loop hands its executors, while inside.

    Wraps ``run_in_executor``, the one way in for Home Assistant's executor
    jobs, its imports and its setup alike, so the clock helpers can tell a
    loop waiting on a thread from one waiting on the clock.
    """
    original = asyncio.BaseEventLoop.run_in_executor

    def _finished(loop: asyncio.AbstractEventLoop) -> None:
        _EXECUTOR_WORK[loop] -= 1

    def run_in_executor(
        self: asyncio.BaseEventLoop, executor: Any, func: Any, *args: Any
    ) -> asyncio.Future[Any]:
        future = original(self, executor, func, *args)
        _EXECUTOR_WORK[self] = _EXECUTOR_WORK.get(self, 0) + 1
        future.add_done_callback(lambda _future: _finished(self))
        return future

    with patch.object(asyncio.BaseEventLoop, "run_in_executor", run_in_executor):
        yield


def executor_work_in_flight() -> bool:
    """Return whether the running loop is waiting on executor work."""
    return _EXECUTOR_WORK.get(asyncio.get_running_loop(), 0) > 0


async def async_wait_out_executor_work(what: str) -> None:
    """
    Wait, in real time and without moving the clock, for executor work to come back.

    Real threads do not run faster for the frozen clock being stepped, so
    stepping it while they work spends steps on nothing: a budget of them
    runs out on a slow machine while an import is still under way. Raises
    ``ExecutorWorkStuck`` once it has waited ``EXECUTOR_WORK_CAP_SECONDS``.
    A stuck thread is not the code under test hanging, so it is not
    reported as that.
    """
    started = freezegun.api.real_monotonic()
    while executor_work_in_flight():
        if freezegun.api.real_monotonic() - started >= EXECUTOR_WORK_CAP_SECONDS:
            raise ExecutorWorkStuck(
                f"executor work still running after "
                f"{EXECUTOR_WORK_CAP_SECONDS:.0f}s, waiting for {what}"
            )
        time.sleep(_EXECUTOR_POLL_SECONDS)
        await async_run_until_quiet()


async def async_step_clock_until(
    hass: HomeAssistant | None,
    freezer: FrozenDateTimeFactory,
    done: Callable[[], bool],
    max_steps: int,
    what: str,
    *,
    step: float = CLOCK_STEP,
) -> None:
    """
    Step the frozen clock until ``done``, failing after ``max_steps``.

    A step is taken only while no executor work is in flight; that is waited
    out in real time instead (``async_wait_out_executor_work``). So the
    budget counts only waits on the clock, and what a test sees does not
    depend on how fast the machine's threads are.

    A failure cancels every unfinished task first. Left waiting on the frozen
    clock, they would hang teardown instead of letting the test fail.
    """
    steps = 0
    while not done():
        await async_wait_out_executor_work(what)
        if done():
            return
        if steps == max_steps:
            break
        steps += 1
        freezer.tick(step)
        await async_run_until_quiet()
    else:
        return
    if hass is not None:
        for task in unfinished_tasks(hass):
            task.cancel()
    await async_run_until_quiet()
    pytest.fail(f"{what} still waiting after {max_steps} clock steps")


async def async_run_paced(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory, *operations: Awaitable[Any]
) -> None:
    """
    Run ``operations`` together, stepping the frozen clock until all finish.

    Each starts eagerly, so one that need not wait reaches the lock before
    the clock moves. Raises what the first failing operation raised.
    """
    tasks = [hass.async_create_task(op, eager_start=True) for op in operations]
    await async_step_clock_until(
        hass,
        freezer,
        lambda: all(task.done() for task in tasks),
        _MAX_STEPS_FOR_OPERATIONS,
        "operations",
    )
    await asyncio.gather(*tasks)


async def async_settle_background(
    hass: HomeAssistant, freezer: FrozenDateTimeFactory
) -> None:
    """
    Step the frozen clock until every task Home Assistant tracks finishes.

    What the test's operations set off -- the confirmation read after a
    write, the sync manager putting back the PIN the test overwrote --
    waits its turn on the same clock, so it reaches the lock only as the
    clock moves.
    """
    await async_step_clock_until(
        hass,
        freezer,
        lambda: not unfinished_tasks(hass) and not lock_workers_busy(hass),
        _MAX_STEPS_FOR_BACKGROUND,
        "background lock work",
    )


def lock_workers(hass: HomeAssistant) -> list[LockWorker]:
    """Return the workers of every lock the instance's registry has in service."""
    registry = hass.data.get(DOMAIN, {}).get(REGISTRY_KEY)
    if registry is None:
        return []
    return [entry.worker for entry in registry._locks.values()]


def lock_workers_busy(hass: HomeAssistant) -> bool:
    """Return whether any lock worker still has a job queued or running."""
    return any(
        worker.started and worker._queue._unfinished_tasks  # type: ignore[attr-defined]
        for worker in lock_workers(hass)
    )


def _live_timers(loop: asyncio.AbstractEventLoop) -> list[asyncio.TimerHandle]:
    """Return the event loop's scheduled timers that are not cancelled."""
    scheduled: list[asyncio.TimerHandle] = loop._scheduled  # type: ignore[attr-defined]
    return [handle for handle in scheduled if not handle.cancelled()]


async def _async_jump_clock(freezer: FrozenDateTimeFactory, seconds: float) -> None:
    """Move the frozen clock ``seconds`` ahead, then run until quiet."""
    loop = asyncio.get_running_loop()
    # The jump happens inside one step of the test's own task, which
    # asyncio's debug mode would log as a callback that ran that long.
    threshold = loop.slow_callback_duration
    loop.slow_callback_duration = math.inf
    try:
        freezer.tick(seconds)
        await asyncio.sleep(0)
    finally:
        loop.slow_callback_duration = threshold
    await async_run_until_quiet()


async def async_advance_sync_ticks(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    ticks: int = 1,
    *,
    settle: bool = True,
) -> None:
    """
    Let ``ticks`` scheduled sync ticks fire, by moving the frozen clock past each.

    Use this, not ``async_trigger_sync_tick``, in new tests. That helper
    forces a sync manager's private state and calls its tick method, so a
    test driven through it is pinned to the sync manager's internals and
    never proves the scheduled tick runs at all. This one only moves time:
    each step moves the clock to the last of the sync managers' interval
    timers -- each armed at most one interval ago -- so every one fires
    exactly once, together with whatever else falls due on the way (the
    coordinator's poll, confirmation reads, pending-write deadlines). A
    timer re-arms a whole interval after it fires, past where the step
    stops, so a step never runs a manager's tick twice, whatever the phase
    it starts at. The clock moves from one timer's due time to the next,
    running the loop until quiet at each, so every timer fires at its own
    time and in due order, with the work it set off run before the next one
    fires -- as it would on a clock that was not frozen. Then the work the
    tick set off is settled with ``async_settle_background``, which steps
    the clock a fraction of a second at most and fails the test rather than
    hang. Fails the test if no sync manager has a tick scheduled.

    Pass ``settle=False`` when the test holds work in flight on purpose -- a
    ``hang`` fault on the mock lock -- since settling waits for every task
    to finish and would fail on the one being held. The loop still runs
    until quiet, so everything not held has reached the lock.

    Compared with the other ways to pass time here:

    - ``async_advance_time`` (``tests/conftest.py``) fires due timers through
      ``async_fire_time_changed`` but moves neither clock. A pending write's
      deadline (``time.monotonic``), a breaker window (``dt_util.utcnow``)
      and a rate-limit sleep (the loop clock) all stand still under it.
    - ``freezer.tick`` alone moves both clocks but leaves the woken work
      unfinished, and a long jump fires every timer it passes at once, at
      its end. This one stops at each timer on the way instead.

    The first call after the freezer starts also fires every timer armed
    before it did: freezing jumps the clock to wall time.
    """
    loop = asyncio.get_running_loop()
    for _ in range(ticks):
        await _async_step_timers_to(
            freezer, loop.time() + TICK_INTERVAL.total_seconds()
        )
        if settle:
            await async_settle_background(hass, freezer)


async def _async_step_timers_to(freezer: FrozenDateTimeFactory, end: float) -> None:
    """
    Move the frozen clock to ``end``, stopping at every timer due on the way.

    Each jump lands just past the next timer's due time, or past ``end``,
    whichever is first, and runs the loop until quiet there. Always jumps at
    least once, so a timer already overdue fires even when ``end`` is now.
    """
    loop = asyncio.get_running_loop()
    for _ in range(_MAX_TIMERS_PER_TICK):
        timers = _live_timers(loop)
        goal = min([end, *(handle.when() for handle in timers)]) + _PAST_DUE
        await _async_jump_clock(freezer, max(goal - loop.time(), 0.0))
        if loop.time() >= end:
            return
    pytest.fail(
        f"more than {_MAX_TIMERS_PER_TICK} timers came due within one sync tick"
    )


async def async_advance_clock(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    seconds: float,
    *,
    settle: bool = True,
) -> None:
    """
    Move the frozen clock ``seconds`` ahead, firing every timer due on the way.

    For waits that are not measured in sync ticks -- an operation deadline,
    the stop grace -- and for stretches with no sync tick scheduled at all,
    like an entry still setting up or one unloading. Each timer fires at its
    own time and the work it set off runs before the next, as in
    ``async_advance_sync_ticks``; the sync ticks on the way fire too. Then
    settles as that helper does, ``settle=False`` included.

    Moves a sync tick's interval at a time, so the bound on timers per step
    guards against a runaway timer however far the clock goes.
    """
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    step = TICK_INTERVAL.total_seconds()
    while loop.time() < end:
        await _async_step_timers_to(freezer, min(loop.time() + step, end))
    if settle:
        await async_settle_background(hass, freezer)


async def async_advance_across_deadline(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    *,
    started_at: float,
    deadline: float,
    held: Callable[[], bool],
    what: str,
) -> None:
    """
    Check ``held`` a sync tick short of a deadline, then not ``held`` a tick past it.

    ``started_at`` is when the deadline's wait began, on the monotonic clock,
    and ``deadline`` how long it is. Bracketing it pins both halves of a
    bound: a wait cut short lets go before the first check, and one that is
    not bounded at all is still held at the second. Fails the test, rather
    than checking nothing, if the deadline is already within a tick.
    Doesn't settle, since what was held may leave work in flight.
    """
    tick = TICK_INTERVAL.total_seconds()
    seconds = started_at + deadline - tick - time.monotonic()
    if seconds <= 0:
        pytest.fail(f"{what}: its deadline is {seconds + tick:.3f}s away, under a tick")
    await async_advance_clock(hass, freezer, seconds, settle=False)
    if not held():
        pytest.fail(f"{what} let go a tick before its deadline")
    await async_advance_clock(hass, freezer, 2 * tick, settle=False)
    if held():
        pytest.fail(f"{what} still held a tick after its deadline")


async def async_tick_until(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    done: Callable[[], bool],
    *,
    max_ticks: int,
    what: str,
    settle: bool = True,
) -> int:
    """
    Advance sync ticks until ``done``; return how many it took.

    Fails the test after ``max_ticks`` with ``what`` in the message, so a
    condition that never comes reports itself instead of spinning. Checks
    ``done`` before the first tick too, so a condition already met costs
    nothing. ``settle`` is passed to each ``async_advance_sync_ticks``.
    """
    taken = await _async_tick_until(hass, freezer, done, max_ticks, settle=settle)
    if taken is None:
        pytest.fail(f"{what} not reached after {max_ticks} sync ticks")
    return taken


async def _async_tick_until(
    hass: HomeAssistant,
    freezer: FrozenDateTimeFactory,
    done: Callable[[], bool],
    max_ticks: int,
    *,
    settle: bool = True,
) -> int | None:
    """
    Advance sync ticks until ``done``; return how many it took, None if never.

    Returning rather than failing lets a caller tell the condition never
    coming apart from a tick that failed to settle, which still fails here.
    """
    for taken in range(max_ticks):
        if done():
            return taken
        await async_advance_sync_ticks(hass, freezer, settle=settle)
    return max_ticks if done() else None


def sync_status(hass: HomeAssistant, in_sync_entity: str) -> str | None:
    """Return the ``sync_status`` an in-sync entity publishes, if any."""
    state = hass.states.get(in_sync_entity)
    assert state is not None, f"{in_sync_entity} has no state"
    return state.attributes.get(ATTR_SYNC_STATUS)


def mock_lock(
    config_entry: ConfigEntry, lock_entity_id: str = LOCK_1_ENTITY_ID
) -> MockLCMLock:
    """
    Return the mock lock an entry drives for ``lock_entity_id``.

    The one place a test reaches into the entry for its fake, so a change to
    where the entry keeps its locks is a change here and nowhere else.
    """
    lock = config_entry.runtime_data.locks[lock_entity_id]
    assert isinstance(lock, MockLCMLock), f"{lock_entity_id} is not a mock lock"
    return lock


@contextmanager
def reading_for():
    """
    Record the entry every lock read allocation performs is made on behalf of.

    The real factory still runs, so the caller under test behaves exactly as
    it would unwatched; only the entry it was handed is captured.
    """
    read_for: list[ConfigEntry | None] = []

    def _spy(hass, dev_reg, ent_reg, config_entry, lock_entity_id):
        read_for.append(config_entry)
        return build_lock_instance(hass, dev_reg, ent_reg, config_entry, lock_entity_id)

    with patch(
        "custom_components.lock_code_manager.ha.allocation.build_lock_instance",
        _spy,
    ):
        yield read_for


async def async_discover_unclaimed_mqtt_lock(
    hass: HomeAssistant, suffix: str = ""
) -> er.RegistryEntry:
    """
    Discover an mqtt lock whose device identifier no provider recognizes.

    Going through real discovery is what puts the bridge's identifier on the
    device registry entry -- the very field dispatch reads -- so a hand-built
    registry row would be testing this test's idea of the payload.

    ``suffix`` distinguishes a second such lock from the first, for the tests
    that need to tell one the entry already holds from one being added now.
    """
    identifier = f"{UNCLAIMED_IDENTIFIER}{suffix}"
    unique_id = f"{identifier}_lock"
    async_fire_mqtt_message(
        hass,
        f"homeassistant/lock/{identifier}/lock/config",
        json.dumps(
            {
                "name": None,
                "command_topic": f"somebridge/lock1{suffix}/set",
                "state_topic": f"somebridge/lock1{suffix}",
                "unique_id": unique_id,
                "device": {
                    "identifiers": [identifier],
                    "name": f"Unclaimed Bridge Lock{suffix}",
                },
            }
        ),
    )
    await hass.async_block_till_done()
    ent_reg = er.async_get(hass)
    entity_id = ent_reg.async_get_entity_id("lock", "mqtt", unique_id)
    assert entity_id is not None, "discovery did not create the lock entity"
    lock_entry = ent_reg.async_get(entity_id)
    assert lock_entry is not None
    return lock_entry


def code_entity_id(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    slot_num: int,
    lock_entity_id: str = LOCK_1_ENTITY_ID,
) -> str:
    """Return the code sensor for one slot on one lock."""
    return _per_lock_entity_id(
        hass, SENSOR_DOMAIN, config_entry, slot_num, ATTR_CODE, lock_entity_id
    )


def in_sync_entity_id(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    slot_num: int,
    lock_entity_id: str = LOCK_1_ENTITY_ID,
) -> str:
    """
    Return the in-sync entity for one slot on one lock.

    Looked up by unique ID rather than spelled out. The entity ID is derived
    from the config entry's title and the lock's name, so writing it into a
    test ties that test to both -- and a test that renames either then looks
    for an entity that was never going to exist.
    """
    return _per_lock_entity_id(
        hass, BINARY_SENSOR_DOMAIN, config_entry, slot_num, ATTR_IN_SYNC, lock_entity_id
    )


def pin_in_sync_entity_id(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    slot_num: int,
    lock_entity_id: str = LOCK_1_ENTITY_ID,
) -> str:
    """Return the PIN in-sync entity for one slot on one lock, enabled or not."""
    return _per_lock_entity_id(
        hass,
        BINARY_SENSOR_DOMAIN,
        config_entry,
        slot_num,
        ATTR_PIN_IN_SYNC,
        lock_entity_id,
    )


def _per_lock_entity_id(
    hass: HomeAssistant,
    platform: str,
    config_entry: ConfigEntry,
    slot_num: int,
    key: str,
    lock_entity_id: str,
) -> str:
    """Resolve a per-lock entity by unique ID."""
    entity_id = er.async_get(hass).async_get_entity_id(
        platform,
        DOMAIN,
        build_slot_unique_id(config_entry.entry_id, slot_num, key, lock_entity_id),
    )
    assert entity_id, f"No {key} entity for slot {slot_num} on {lock_entity_id}"
    return entity_id


def slot_entity_id(
    hass: HomeAssistant,
    platform: str,
    config_entry: ConfigEntry,
    slot_num: int,
    key: str,
) -> str:
    """
    Resolve a slot's entity by unique ID.

    Spelling the entity ID out ties the test to the name of whoever holds the
    slot, since that is what the device -- and so the slug -- is named after.
    """
    entity_id = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, build_slot_unique_id(config_entry.entry_id, slot_num, key)
    )
    assert entity_id, f"No {key} entity for slot {slot_num}"
    return entity_id


async def async_set_slot_pin(
    hass: HomeAssistant, config_entry: ConfigEntry, slot_num: int, pin: str
) -> None:
    """Set a slot's PIN through its text entity, as the user does."""
    await hass.services.async_call(
        TEXT_DOMAIN,
        TEXT_SERVICE_SET_VALUE,
        {TEXT_ATTR_VALUE: pin},
        target={
            ATTR_ENTITY_ID: slot_entity_id(
                hass, TEXT_DOMAIN, config_entry, slot_num, CONF_PIN
            )
        },
        blocking=True,
    )


async def async_write_slot_pin(
    hass: HomeAssistant, config_entry: ConfigEntry, slot_num: int, pin: str
) -> None:
    """
    Write a slot's PIN into the configuration, past the entity's checks.

    For a configuration the entity would refuse, such as one saved before a
    rule existed: the lock sees it through the entry's update listener.
    """
    async_write_entry_config(
        hass,
        config_entry,
        get_entry_config(config_entry).with_slot_field_set(slot_num, CONF_PIN, pin),
    )
    await hass.async_block_till_done()


async def async_switch_slot(
    hass: HomeAssistant, config_entry: ConfigEntry, slot_num: int, service: str
) -> None:
    """Turn a slot's enabled switch on or off, as the user does."""
    await hass.services.async_call(
        SWITCH_DOMAIN,
        service,
        target={
            ATTR_ENTITY_ID: slot_entity_id(
                hass, SWITCH_DOMAIN, config_entry, slot_num, CONF_ENABLED
            )
        },
        blocking=True,
    )


# The version Lock Code Manager writes its entries at today.
CONFIG_ENTRY_VERSION = LockCodeManagerFlowHandler.VERSION

# The integration the mock lock entities belong to.
LOCK_DEVICE_DOMAIN = "test"

SLOT_1_ACTIVE_ENTITY = "binary_sensor.mock_title_test1_active"
SLOT_1_ENABLED_ENTITY = "switch.mock_title_test1_enabled"
SLOT_1_EVENT_ENTITY = "event.mock_title_test1_credential_used"
SLOT_1_NAME_ENTITY = "text.mock_title_test1_name"
SLOT_1_PIN_ENTITY = "text.mock_title_test1_pin"
SLOT_1_IN_SYNC_ENTITY = "binary_sensor.mock_title_test1_test_1_in_sync"

SLOT_2_ENABLED_ENTITY = "switch.mock_title_test2_enabled"
SLOT_2_ACTIVE_ENTITY = "binary_sensor.mock_title_test2_active"
SLOT_2_EVENT_ENTITY = "event.mock_title_test2_credential_used"
SLOT_2_PIN_ENTITY = "text.mock_title_test2_pin"
SLOT_2_NAME_ENTITY = "text.mock_title_test2_name"
SLOT_2_IN_SYNC_ENTITY = "binary_sensor.mock_title_test2_test_1_in_sync"


# ``setup`` is the provider's own setup, which the entry runs when it builds
# the lock and again whenever the lock's integration comes back. ``probe`` is
# the device availability check every operation runs first: device
# input/output on a provider like Schlage, whose check is a cloud round trip.
LockOperation = Literal["set", "clear", "get", "setup", "probe"]
# What a fault naming no operation matches: the calls that reach the lock's
# code table. Setup and the probe are faulted only by name, so a test failing
# every operation keeps the lifecycle it did not ask about.
_TABLE_OPERATIONS = frozenset({"set", "clear", "get"})
# Operations that touch no slot, so a fault naming one cannot name a slot.
_SLOTLESS_OPERATIONS = frozenset({"setup", "probe"})


class FaultKind(StrEnum):
    """How a call to ``MockLCMLock`` that a fault matches misbehaves."""

    # The lock answers success and keeps what it held.
    IGNORE = "ignore"
    # The lock answers that it will not: ``CodeRejectedError``, which is what
    # providers raise for a refused set and a refused clear alike.
    REFUSE = "refuse"
    # The call raises the fault's error (``LockOperationFailed`` unless
    # given) and never reaches the lock.
    FAIL = "fail"
    # The call waits, having done nothing, until the fault is cleared; then
    # it runs as if nothing had happened.
    HANG = "hang"
    # The lock does what it was asked, and the answer never arrives: the call
    # waits until the caller's own deadline cancels it or the fault is
    # cleared, which raises ``LockDisconnected`` as a provider's own reply
    # timeout does.
    DROP_REPLY = "drop_reply"
    # The call takes the fault's ``seconds`` on the clock before it reaches
    # the lock, or until the fault is cleared, then runs as if nothing had
    # happened: a lock that answers, slowly.
    SLOW = "slow"


# A read has nothing to ignore and nothing a lock refuses.
_WRITE_ONLY_FAULTS = frozenset({FaultKind.IGNORE, FaultKind.REFUSE})


class Clearable(Protocol):
    """A fault the teardown hook can release: anything with ``clear``."""

    def clear(self) -> None:
        """Stop misbehaving, and let any call held go."""


# Every fault installed and not yet cleared, spent ones included, so
# ``release_all_faults`` can let go of a held call the test forgot to
# release: teardown stops the sync managers, which wait on an in-flight
# tick, and a frozen clock never ends that wait. A dict, so they are
# released in the order installed.
_INSTALLED_FAULTS: dict[Clearable, None] = {}


def track_fault(fault: Clearable) -> None:
    """
    Register a fault from another harness for release at teardown.

    The Z-Wave JS node fake's faults hold calls the way ``hang`` and
    ``drop_reply`` do here, so the same hook has to let them go.
    """
    _INSTALLED_FAULTS[fault] = None


def untrack_fault(fault: Clearable) -> None:
    """Forget a fault registered with ``track_fault``, once it is cleared."""
    _INSTALLED_FAULTS.pop(fault, None)


def release_all_faults() -> None:
    """Clear every fault installed on any mock lock, releasing held calls."""
    for fault in list(_INSTALLED_FAULTS):
        fault.clear()
    _INSTALLED_FAULTS.clear()


def forget_all_faults() -> None:
    """
    Drop every installed fault without releasing what it holds.

    For after teardown, once the event loop a held call waits on may be
    closed: releasing would wake that call on a loop that cannot run it.
    """
    _INSTALLED_FAULTS.clear()


@dataclass(eq=False)
class LockFault:
    """
    One way a ``MockLCMLock`` misbehaves, until cleared.

    Matches calls of ``operation`` (any set, clear or read when None) that
    touch ``slot`` (any when None; a read touches every slot in its scope,
    and a setup or a probe touches none). The first installed
    fault that matches a call is the one it meets. ``times`` limits how
    many calls it matches before it removes itself; None matches until the
    test clears it. A spent fault still holds the last call it met until
    cleared.

    ``hits`` counts the calls it matched, and ``held`` how many a ``hang``
    or ``drop_reply`` is holding right now; ``async_wait_held`` lets a test
    wait for a write to be in flight before acting. Usable as a context
    manager that clears it on exit.
    """

    lock: MockLCMLock
    kind: FaultKind
    operation: LockOperation | None
    slot: int | None
    error: type[Exception] | Exception | None = None
    times: int | None = None
    seconds: float = 0.0
    hits: int = 0
    held: int = 0
    _released: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        """Refuse a fault that cannot happen to the operation it names."""
        if self.kind in _WRITE_ONLY_FAULTS and self.operation not in ("set", "clear"):
            raise ValueError(f"a {self.kind} fault needs a set or clear operation")
        if self.operation in _SLOTLESS_OPERATIONS and self.slot is not None:
            # It would match nothing, and a test relying on it would pass
            # for the wrong reason.
            raise ValueError(f"a {self.operation} touches no slot to name")

    def __enter__(self) -> Self:
        """Return the fault, to clear on exit."""
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Clear the fault."""
        self.clear()

    def matches(
        self, operation: LockOperation, slots: Collection[int], changes: bool
    ) -> bool:
        """
        Return whether a call of ``operation`` touching ``slots`` meets this.

        ``changes`` is whether the call would change what the lock holds. An
        ``ignore`` has nothing to ignore in a call that would not, so it lets
        that call through without counting it; every other kind of fault
        misbehaves the same either way.
        """
        return (
            (
                self.operation == operation
                or (self.operation is None and operation in _TABLE_OPERATIONS)
            )
            and (self.slot is None or self.slot in slots)
            and (changes or self.kind is not FaultKind.IGNORE)
        )

    async def async_wait_held(self) -> None:
        """
        Run the loop until quiet, then fail unless this fault is holding a call now.

        Works on a frozen clock, where a timeout around a wait would never
        fire: whatever was on its way to the lock without waiting on the
        clock has arrived once the loop is quiet. A call that has to wait on
        the clock first needs the clock stepped before this. A call already
        released, or cancelled while held, is not held.
        """
        await async_run_until_quiet()
        if not self.held:
            pytest.fail(
                f"no {self.operation or 'call'} held by the {self.kind} fault "
                f"(matched {self.hits} calls)"
            )

    def clear(self) -> None:
        """Stop matching calls, and let any call this is holding go."""
        if self in self.lock.faults:
            self.lock.faults.remove(self)
        _INSTALLED_FAULTS.pop(self, None)
        self._released.set()

    def raise_error(self, operation: LockOperation, slot: int | None) -> None:
        """Raise what a ``fail`` or ``refuse`` fault raises."""
        if self.kind is FaultKind.REFUSE:
            # Only a set or a clear can be refused, and both name their slot.
            assert slot is not None
            raise CodeRejectedError(
                code_slot=slot,
                lock_entity_id=self.lock.lock.entity_id,
                reason=f"the lock refused the {operation}",
            )
        error = self.error or LockOperationFailed
        if isinstance(error, Exception):
            raise error
        raise error(f"injected {operation} failure on {self.lock.lock.entity_id}")

    async def _async_hold(self) -> None:
        """Hold the call until the fault is cleared, counting it while held."""
        self.held += 1
        try:
            await self._released.wait()
        finally:
            self.held -= 1

    async def async_before(self, operation: LockOperation, slot: int | None) -> None:
        """Act out what happens to a call before it reaches the lock."""
        if self.kind is FaultKind.FAIL:
            self.raise_error(operation, slot)
        if self.kind is FaultKind.HANG:
            await self._async_hold()
        if self.kind is FaultKind.SLOW:
            # Counted as held while it waits, so a test can tell it is under
            # way. Clearing ends the wait, so teardown can let it go on a
            # frozen clock.
            self.held += 1
            try:
                with suppress(TimeoutError):
                    async with asyncio.timeout(self.seconds):
                        await self._released.wait()
            finally:
                self.held -= 1

    def at_the_lock(self, operation: LockOperation, slot: int | None) -> None:
        """Act out the lock's own answer to a call that reached it."""
        if self.kind is FaultKind.REFUSE:
            self.raise_error(operation, slot)

    async def async_after(self, operation: LockOperation) -> None:
        """Act out what happens after the lock has done what it was asked."""
        if self.kind is FaultKind.DROP_REPLY:
            await self._async_hold()
            raise LockDisconnected(
                f"no reply to the {operation} on {self.lock.lock.entity_id}"
            )


# The BaseLock method that asks the device availability check on the
# connection check's own cadence; every other asker is an operation's gate.
_CONNECTION_CHECK = "async_internal_is_reachable"

# What ``when_mock_lock_built`` runs on each mock lock as it is built.
_ON_BUILD: list[Callable[[MockLCMLock], None]] = []


@contextmanager
def when_mock_lock_built(act: Callable[[MockLCMLock], Any]) -> Iterator[list[Any]]:
    """
    Run ``act`` on each mock lock as an entry builds it, while inside.

    For what has to be true of a lock before the entry's own setup reaches
    it -- a fault on its setup -- and for what a real lock keeps across a
    restart that a rebuilt mock would forget, like the codes it holds.
    Yields what each call returned, in build order, so a test can clear a
    fault it installed this way.
    """
    returned: list[Any] = []

    def _act(lock: MockLCMLock) -> None:
        returned.append(act(lock))

    _ON_BUILD.append(_act)
    try:
        yield returned
    finally:
        _ON_BUILD.remove(_act)


@dataclass(repr=False, eq=False)
class MockLCMLock(BaseLock):
    """
    Mock Lock Code Manager lock instance.

    Answers like a slot-only provider over an in-memory code table. With no
    fault installed every call succeeds; a test makes the lock misbehave on
    one operation and slot with the fault knobs -- ``ignore``, ``refuse``,
    ``fail``, ``hang``, ``drop_reply`` and ``slow`` -- each returning the
    ``LockFault`` to clear when done.

    ``service_calls`` records what reached the lock: ``set_usercode`` and
    ``clear_usercode`` for every write the lock acknowledged as a change
    (an ignored one included -- the lock said yes), ``noop_set_usercode``
    for a set of the code the slot already held, ``noop_clear_usercode``
    for a clear of a slot that held nothing, ``get_usercodes`` for every
    read, and ``setup`` and ``unload`` for the provider lifecycle.
    ``call_order`` names each of those in the order it was recorded.

    ``attempt_times`` records when each operation -- a set, a clear or a
    read -- asked the device availability check, the gate it passes before
    it can reach the lock. An attempt on a lock that is not there stops at
    that gate, so it shows up there and nowhere else. The connection check
    asks the same question on its own cadence and is not an attempt, so its
    asks are left out.
    """

    def __init__(self, *args, **kwargs):
        """Initialize mock lock."""
        super().__init__(*args, **kwargs)
        self._connected = True
        self._device_available = True
        self._hard_refresh_interval: timedelta | None = None
        self.codes: dict[int, str] = {1: "1234", 2: "5678"}
        # Slots this lock reports as occupied without giving up the value,
        # the way a lock with masked Personal Identification Numbers does.
        # Reported regardless of ``codes``, so a slot keeps reading occupied
        # after a clear -- which is the state a clear can never confirm.
        self.write_only: set[int] = set()
        # Slots this lock answers every read without saying anything about,
        # the way an API-only Z-Wave JS UI lock does with a slot it cannot
        # read. Told to the core as that provider tells it.
        self.unanswered: set[int] = set()
        # Slots this lock answers it cannot tell what they hold, as a Zigbee
        # lock's user status Not Supported does (#1581).
        self.cannot_tell: set[int] = set()
        self.service_calls: defaultdict[str, list] = defaultdict(list)
        self.call_order: list[str] = []
        # When each set, clear, and read reached the lock, on the monotonic
        # clock, so pacing tests can check the gaps between them. A call a
        # ``fail`` stopped never reached it, a held one reached it when
        # released, and a refused one reached it and was turned away.
        self.run_times: list[float] = []
        # When each operation asked the device availability check, on the
        # monotonic clock: its first step, whether or not it goes further.
        self.attempt_times: list[float] = []
        self.faults: list[LockFault] = []
        # What the lock reports it can hold. Setting it makes the mock a
        # native-user lock as far as setup's capability check is concerned --
        # BaseLock runs that check for native-user providers only -- but not
        # in its user lifecycle: sets, clears and reads stay this mock's
        # slot-level ones. The flag also sizes the operation deadlines as for
        # a native-user lock: three exchanges per write, and one per managed
        # slot plus one per clear. None, the default, is a slot-only lock
        # that is checked for nothing.
        #
        # BaseLock caches the first answer it gets, for the instance's
        # lifetime, whichever caller asked -- allocation's slot-range ask and
        # the write path's capacity checks keep even an answer with no PIN
        # room. Only setup's check drops an answer that fails it. So fixing
        # this after a failed setup check is seen at the lock integration's
        # next reload only if nothing else asked in between.
        self.capabilities: LockCapabilities | None = None
        for act in _ON_BUILD:
            act(self)

    def add_fault(
        self,
        kind: FaultKind,
        operation: LockOperation | None,
        slot: int | None = None,
        *,
        error: type[Exception] | Exception | None = None,
        times: int | None = None,
        seconds: float = 0.0,
    ) -> LockFault:
        """Install a fault; the knobs below are the usual way in."""
        fault = LockFault(
            self, kind, operation, slot, error=error, times=times, seconds=seconds
        )
        self.faults.append(fault)
        _INSTALLED_FAULTS[fault] = None
        return fault

    def ignore(
        self,
        operation: Literal["set", "clear"],
        slot: int | None = None,
        *,
        times: int | None = None,
    ) -> LockFault:
        """
        Acknowledge writes without applying them.

        The call reports success -- ``CONFIRMED`` for a set, True for a clear
        -- and the code table keeps what it held. Only a write that would
        change the slot is ignored: a set of the code the slot already holds,
        or a clear of an empty slot, answers as usual and is not counted
        against ``times``. On ``MockLCMPushLock`` an acknowledged set still
        pushes the value, as a provider that pushes on the device's success
        status does.
        """
        return self.add_fault(FaultKind.IGNORE, operation, slot, times=times)

    def refuse(
        self,
        operation: Literal["set", "clear"],
        slot: int | None = None,
        *,
        times: int | None = None,
    ) -> LockFault:
        """Refuse writes with ``CodeRejectedError``, the provider's rejection."""
        return self.add_fault(FaultKind.REFUSE, operation, slot, times=times)

    def fail(
        self,
        operation: LockOperation | None,
        slot: int | None = None,
        *,
        error: type[Exception] | Exception = LockOperationFailed,
        times: int | None = None,
    ) -> LockFault:
        """
        Fail calls with ``error`` before they reach the lock.

        ``LockOperationFailed`` (the default) charges the slot's breaker;
        ``LockDisconnected`` charges the lock's. A class is raised with a
        message naming the lock; an instance is raised as given.
        """
        return self.add_fault(FaultKind.FAIL, operation, slot, error=error, times=times)

    def hang(
        self,
        operation: LockOperation | None,
        slot: int | None = None,
        *,
        times: int | None = None,
    ) -> LockFault:
        """
        Hold calls before the lock does anything, until the fault is cleared.

        A held write keeps the lock's turn, so everything queued behind it
        waits too. Await ``fault.async_wait_held()`` to know one is held.
        """
        return self.add_fault(FaultKind.HANG, operation, slot, times=times)

    def drop_reply(
        self,
        operation: LockOperation | None,
        slot: int | None = None,
        *,
        times: int | None = None,
    ) -> LockFault:
        """
        Apply calls, then never answer them.

        The code table changes as asked, and the caller waits: until its own
        deadline gives up on the lock, or until the test clears the fault,
        which raises ``LockDisconnected`` the way a provider's own reply
        timeout does.
        """
        return self.add_fault(FaultKind.DROP_REPLY, operation, slot, times=times)

    def slow(
        self,
        operation: LockOperation | None,
        slot: int | None = None,
        *,
        seconds: float,
        times: int | None = None,
    ) -> LockFault:
        """
        Make calls take ``seconds`` on the clock, then answer as usual.

        A slow write keeps the lock's turn for that long, as a lock that
        answers slowly does, so everything queued behind it waits too.
        Clearing the fault ends the wait early.
        """
        return self.add_fault(
            FaultKind.SLOW, operation, slot, times=times, seconds=seconds
        )

    def clear_faults(self) -> None:
        """
        Clear every fault installed on this lock, releasing any call one holds.

        Spent faults included: one whose ``times`` ran out matches nothing
        more, but may still be holding the last call it met. Released in the
        order installed.
        """
        for fault in [
            fault
            for fault in _INSTALLED_FAULTS
            if isinstance(fault, LockFault) and fault.lock is self
        ]:
            fault.clear()

    def _meet_fault(
        self, operation: LockOperation, slots: Collection[int], changes: bool
    ) -> LockFault | None:
        """Return the fault a call meets, counting the hit and expiring it."""
        fault = next(
            (f for f in self.faults if f.matches(operation, slots, changes)), None
        )
        if fault is None:
            return None
        fault.hits += 1
        if fault.times is not None and fault.hits >= fault.times:
            self.faults.remove(fault)
        return fault

    async def _async_reach_lock(
        self,
        operation: LockOperation,
        slots: Collection[int],
        slot: int | None = None,
        *,
        changes: bool = True,
    ) -> LockFault | None:
        """
        Carry a call to the lock through the fault it meets; return that fault.

        A ``fail`` stops the call on the way and a ``hang`` holds it there,
        so a call is recorded in ``run_times`` only once it arrives. A
        ``refuse`` is the lock's own answer, given after it arrived.
        """
        fault = self._meet_fault(operation, slots, changes)
        if fault is not None:
            await fault.async_before(operation, slot)
        self.run_times.append(time.monotonic())
        if fault is not None:
            fault.at_the_lock(operation, slot)
        return fault

    async def _async_through_fault(self, operation: Literal["setup", "probe"]) -> None:
        """
        Carry a setup or a probe through the fault it meets, if any.

        Neither touches the code table, so neither is recorded in
        ``run_times``: the pacing between operations is not theirs.
        """
        if (fault := self._meet_fault(operation, (), True)) is not None:
            await fault.async_before(operation, None)
            await fault.async_after(operation)

    def _record(self, call: str, details: Any) -> None:
        """Record a call that reached the lock, and where it came in the order."""
        self.service_calls[call].append(details)
        self.call_order.append(call)

    def _changes(self, code_slot: int, usercode: str | None) -> bool:
        """Return whether writing ``usercode`` (None clears) changes the slot."""
        return self.codes.get(code_slot) != usercode

    def _read_scope(self, slots: Collection[int] | None) -> Collection[int]:
        """Return the slots a read covers: the managed slots when it names none."""
        return self.managed_slots if slots is None else slots

    @property
    def domain(self) -> str:
        """Return integration domain."""
        return "test"

    @property
    def hard_refresh_interval(self) -> timedelta | None:
        """Return configurable hard refresh interval."""
        return self._hard_refresh_interval

    def set_connected(self, connected: bool) -> None:
        """Set connection state for testing."""
        self._connected = connected

    def set_device_available(self, available: bool) -> None:
        """Set device (node) availability for testing."""
        self._device_available = available

    @property
    def supports_native_users(self) -> bool:
        """
        Return whether this mock answers as a native-user lock does.

        Setup then checks its capabilities, and each write's deadline is
        sized for three exchanges and each clear's for one per managed slot
        plus one. Its writes and reads stay slot-level.
        """
        return self.capabilities is not None

    async def async_get_capabilities(self) -> LockCapabilities:
        """Return what the lock reports it can hold, when a test has given it that."""
        if self.capabilities is None:
            return await super().async_get_capabilities()
        return self.capabilities

    async def async_is_integration_connected(self) -> bool:
        """Return whether the integration's client/driver/broker is connected."""
        return self._connected

    async def async_is_device_available(self) -> bool:
        """Return whether the physical device (node) is reachable: the ``probe``."""
        # Asked by the method that called this one, before anything awaits.
        if sys._getframe(1).f_code.co_name != _CONNECTION_CHECK:
            self.attempt_times.append(time.monotonic())
        await self._async_through_fault("probe")
        return self._device_available

    async def async_setup(self, config_entry: ConfigEntry) -> None:
        """Record the provider setup, which runs again on every reconnect."""
        self._record("setup", (config_entry.entry_id,))
        await self._async_through_fault("setup")
        await super().async_setup(config_entry)

    async def async_unload(self, remove_permanently: bool) -> None:
        """Record the teardown so tests can assert on remove_permanently."""
        self._record("unload", (remove_permanently,))
        await super().async_unload(remove_permanently)

    async def async_hard_refresh_codes(
        self, slots: Collection[int] | None = None
    ) -> dict[int, SlotCredential]:
        """Perform hard refresh; records the scope it was asked for."""
        self._record("hard_refresh_codes", (slots,))
        return await self.async_get_usercodes()

    async def async_set_usercode(
        self,
        code_slot: int,
        usercode: str,
        name: str | None = None,
        source: Literal["sync", "direct"] = "direct",
    ) -> WriteResult:
        """
        Set a usercode on a code slot.

        Returns CONFIRMED if the value was changed, NO_CHANGE if already set.
        """
        fault = await self._async_reach_lock(
            "set", (code_slot,), code_slot, changes=self._changes(code_slot, usercode)
        )
        # Asked again: a held call answers for what the slot holds on release.
        if not self._changes(code_slot, usercode):
            self._record("noop_set_usercode", (code_slot, usercode, name))
            result = WriteResult.NO_CHANGE
        else:
            if fault is None or fault.kind is not FaultKind.IGNORE:
                self.codes[code_slot] = usercode
            self._record("set_usercode", (code_slot, usercode, name))
            result = WriteResult.CONFIRMED
        if fault is not None:
            await fault.async_after("set")
        return result

    async def async_clear_usercode(self, code_slot: int) -> bool:
        """
        Clear a usercode on a code slot.

        Returns True if the value was changed, False if already cleared.
        """
        fault = await self._async_reach_lock(
            "clear", (code_slot,), code_slot, changes=self._changes(code_slot, None)
        )
        if not self._changes(code_slot, None):
            self._record("noop_clear_usercode", (code_slot,))
            changed = False
        else:
            if fault is None or fault.kind is not FaultKind.IGNORE:
                self.codes.pop(code_slot)
            self._record("clear_usercode", (code_slot,))
            changed = True
        if fault is not None:
            await fault.async_after("clear")
        return changed

    async def async_get_usercodes(
        self, slots: Collection[int] | None = None
    ) -> dict[int, SlotCredential]:
        """Return dictionary of code slots and usercodes."""
        fault = await self._async_reach_lock("get", self._read_scope(slots))
        # Taken once the read reaches the lock, as a provider walking the
        # slots would: a read held on the way answers for the slots managed
        # when it is released, not when it was asked.
        scope = self._read_scope(slots)
        snapshot = self.codes.copy()
        self._record("get_usercodes", snapshot)
        codes = {slot: SlotCredential.known(pin) for slot, pin in snapshot.items()}
        codes.update({slot: SlotCredential.unreadable() for slot in self.write_only})
        # Mirrors the base projection, including the part that matters: a slot
        # in the scope -- the managed slots, when the caller names none -- that
        # holds nothing is empty, and a slot the lock holds OUTSIDE the scope
        # is still reported. Answering with exactly the scope would model the
        # one shape where a caller's own bounds check is a no-op; omitting an
        # empty managed slot would hide the read the pending-write machinery
        # waits on.
        result = {**dict.fromkeys(scope, SlotCredential.empty()), **codes}
        self.last_cannot_tell_slots = frozenset(self.cannot_tell)
        self.last_silent_slots = frozenset(self.unanswered | self.cannot_tell)
        if fault is not None:
            await fault.async_after("get")
        return result


@dataclass(repr=False, eq=False)
class MockLCMPushLock(MockLCMLock):
    """
    Mock lock that supports push-based updates.

    ``subscribe_error``, when set, is raised by every subscription attempt
    instead of subscribing, which is not counted then. It is set before
    ``when_mock_lock_built`` acts on the lock, so an act can set it.

    A subscription is real: it listens for ``async_fire_mock_lock_use``, the
    lock integration reporting a code use, and records the use as a provider
    does. So an instance that never unsubscribed goes on hearing its lock,
    exactly as a leaked subscription to a real lock would.
    """

    async def async_set_usercode(
        self, code_slot: int, usercode: str, *args: Any, **kwargs: Any
    ) -> WriteResult:
        """Set a code and push it, as every push provider does before CONFIRMED."""
        result = await super().async_set_usercode(code_slot, usercode, *args, **kwargs)
        if result is WriteResult.CONFIRMED:
            self._push_credential_update(code_slot, SlotCredential.known(usercode))
        return result

    def __init__(self, *args, **kwargs):
        """Initialize mock push lock."""
        # Before the base initializer, which runs the build acts: what an act
        # sets here must not be overwritten once it returns.
        self._supports_push = True
        # Counted rather than flagged: a provider's setup must be idempotent
        # and runs again on every reconnect, so the number of times something
        # subscribed is what a double subscription shows up as.
        self.subscribe_calls = 0
        self.unsubscribe_calls = 0
        self.subscribe_error: Exception | None = None
        self._stop_hearing_uses: Callable[[], None] | None = None
        super().__init__(*args, **kwargs)

    @property
    def supports_push(self) -> bool:
        """Return whether this lock supports push-based updates."""
        return self._supports_push

    def setup_push_subscription(self) -> None:
        """Subscribe to push-based value updates, or raise ``subscribe_error``."""
        if self.subscribe_error is not None:
            raise self.subscribe_error
        self.subscribe_calls += 1
        # Idempotent, as a provider's subscription must be.
        if self._stop_hearing_uses is None:
            self._stop_hearing_uses = self.hass.bus.async_listen(
                EVENT_MOCK_LOCK_USE, self._async_on_lock_use
            )

    def teardown_push_subscription(self) -> None:
        """Unsubscribe from push-based value updates."""
        self.unsubscribe_calls += 1
        if self._stop_hearing_uses is not None:
            self._stop_hearing_uses()
            self._stop_hearing_uses = None

    @callback
    def _async_on_lock_use(self, event: Event) -> None:
        """Record a code use the lock integration reported on this lock."""
        if event.data[ATTR_ENTITY_ID] == self.lock.entity_id:
            self.async_fire_code_slot_event(event.data[CONF_SLOT], to_locked=False)


# What the mock lock integration fires when a code is used on one of its locks;
# a push mock's subscription is what hears it.
EVENT_MOCK_LOCK_USE = f"{LOCK_DEVICE_DOMAIN}_lock_use"


@callback
def async_fire_mock_lock_use(
    hass: HomeAssistant, lock_entity_id: str, code_slot: int
) -> None:
    """Report a use of the code in ``code_slot`` on a lock, as its integration does."""
    hass.bus.async_fire(
        EVENT_MOCK_LOCK_USE, {ATTR_ENTITY_ID: lock_entity_id, CONF_SLOT: code_slot}
    )


class MockLockEntity(LockEntity):
    """Mocked lock entity."""

    _attr_has_entity_name = True

    def __init__(self, name: str) -> None:
        """Initialize the lock."""
        self._attr_name = name
        self._attr_unique_id = slugify(name)
        self._attr_is_locked = False
        self._attr_has_entity_name = False
        # The mock lock's device belongs to the mock lock's integration, not
        # to Lock Code Manager. Using Lock Code Manager's domain here would
        # make the device read as one of ours, and the sweep that moves
        # entities off other integrations' devices would never be exercised
        # against the shape it exists for.
        self._attr_device_info = DeviceInfo(
            identifiers={(LOCK_DEVICE_DOMAIN, f"lock.{slugify(name)}")}, name=name
        )
        super().__init__()


class MockCalendarEntity(CalendarEntity):
    """Test Calendar entity."""

    _attr_has_entity_name = True

    def __init__(self, name: str, events: list[CalendarEvent] | None = None) -> None:
        """Initialize entity."""
        self._attr_name = name.capitalize()
        # The list given, empty or not, so a calendar rebuilt over it shares it.
        self._events = [] if events is None else events

        self._attr_unique_id = slugify(name)

    @property
    def event(self) -> CalendarEvent | None:
        """Return the next upcoming event."""
        return self._events[0] if self._events else None

    @callback
    def create_event(self, **kwargs) -> CalendarEvent:
        """Create a new fake event, used by tests."""
        event = CalendarEvent(
            start=kwargs["dtstart"], end=kwargs["dtend"], summary=kwargs["summary"]
        )
        self._events.append(event)
        self.async_write_ha_state()
        return event

    @callback
    def delete_event(
        self,
        uid: str,
        recurrence_id: str | None = None,
        recurrence_range: str | None = None,
    ) -> None:
        """Delete an event on the calendar."""
        for event in self._events:
            if event.uid == uid:
                self._events.remove(event)
                self.async_write_ha_state()
                return

    @callback
    def get_events(
        self,
        hass: HomeAssistant,
        start_date: datetime,
        end_date: datetime,
    ) -> list[CalendarEvent]:
        """Return calendar events within a datetime range."""
        assert start_date < end_date
        events = []
        for event in self._events:
            if event.start_datetime_local >= end_date:
                continue
            if event.end_datetime_local < start_date:
                continue
            events.append(event)
        return events


# Where ``async_setup_mock_lock_entry`` keeps the calendars it added last.
MOCK_CALENDARS_KEY = "lock_code_manager_calendars"


class LiveMockCalendars(Sequence[MockCalendarEntity]):
    """
    The mock lock integration's calendars as added now, looked up on every use.

    Reloading the integration replaces its calendar entities, so holding on to
    the list from before would act on entities Home Assistant has removed.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        """Read the calendars from ``hass``."""
        self._hass = hass

    def _current(self) -> list[MockCalendarEntity]:
        return self._hass.data[MOCK_CALENDARS_KEY]

    def __getitem__(self, index: Any) -> Any:
        """Return the calendar, or the slice of calendars, added now at ``index``."""
        return self._current()[index]

    def __len__(self) -> int:
        """Return how many calendars are added now."""
        return len(self._current())


async def async_setup_mock_lock_entry(hass: HomeAssistant) -> MockConfigEntry:
    """
    Set up the mock lock integration and return its config entry.

    Gives ``hass`` the two lock entities (``LOCK_1_ENTITY_ID`` and
    ``LOCK_2_ENTITY_ID``) and two calendars, the latter also stored under
    ``hass.data["lock_code_manager_calendars"]``. Each setup of the entry
    builds its entities afresh, as a real integration does, so the entry can
    be reloaded; the calendars keep their events across it, and
    ``hass.data`` holds the ones currently added. A coroutine rather than
    only a fixture, so a test that builds its own Home Assistant per example
    -- a Hypothesis state machine -- sets up the same locks the suite does.
    The caller registers a config flow for ``LOCK_DEVICE_DOMAIN`` first
    (``mock_config_flow``), without which the entry does not set up.
    """

    async def async_setup_entry_init(
        hass: HomeAssistant, config_entry: ConfigEntry
    ) -> bool:
        """Set up test config entry."""
        await hass.config_entries.async_forward_entry_setups(
            config_entry, [Platform.CALENDAR, Platform.LOCK]
        )
        return True

    async def async_unload_entry_init(
        hass: HomeAssistant, config_entry: ConfigEntry
    ) -> bool:
        """Unload test config entry."""
        for platform in (Platform.CALENDAR, Platform.LOCK):
            await hass.config_entries.async_forward_entry_unload(config_entry, platform)
        return True

    mock_platform(hass, f"{LOCK_DEVICE_DOMAIN}.config_flow")
    mock_integration(
        hass,
        MockModule(
            LOCK_DEVICE_DOMAIN,
            async_setup_entry=async_setup_entry_init,
            async_unload_entry=async_unload_entry_init,
        ),
    )

    config_entry = MockConfigEntry(domain=LOCK_DEVICE_DOMAIN)

    async def async_setup_entry_lock_platform(
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback,
    ) -> None:
        """Set up test lock platform via config entry."""
        async_add_entities([MockLockEntity("test_1"), MockLockEntity("test_2")])

    mock_platform(
        hass,
        f"{LOCK_DEVICE_DOMAIN}.{LOCK_DOMAIN}",
        MockPlatform(async_setup_entry=async_setup_entry_lock_platform),
    )

    async def async_setup_entry_calendar_platform(
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        async_add_entities: AddEntitiesCallback,
    ) -> None:
        """Set up test calendar platform via config entry."""
        # An entity object is added once in its life, so a reload needs new
        # ones; what the old ones held carries over.
        previous = hass.data.get(MOCK_CALENDARS_KEY, [])
        calendars = hass.data[MOCK_CALENDARS_KEY] = [
            MockCalendarEntity(
                name,
                events=next(
                    (old._events for old in previous if old.unique_id == slugify(name)),
                    None,
                ),
            )
            for name in ("test_1", "test_2")
        ]
        async_add_entities(calendars)

    mock_platform(
        hass,
        f"{LOCK_DEVICE_DOMAIN}.{CALENDAR_DOMAIN}",
        MockPlatform(async_setup_entry=async_setup_entry_calendar_platform),
    )

    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry


def user_subentries(slots: dict[int, dict]) -> tuple[ConfigSubentryData, ...]:
    """
    Build the user subentries an LCM entry holds, from a slot-keyed dict.

    Fixtures that add an entry without setting it up get no migration, so they
    have to supply subentries themselves — users have not lived in entry data
    since version 5. Takes the slot-keyed shape the fixtures already write so
    only the plumbing changes, not what each test says it is configuring.
    """
    return tuple(
        ConfigSubentryData(
            data={
                **{key: value for key, value in fields.items() if key != CONF_NAME},
                CONF_SLOT: slot_num,
            },
            subentry_type=SUBENTRY_TYPE_USER,
            title=fields[CONF_NAME],
            unique_id=None,
        )
        for slot_num, fields in slots.items()
    )


async def async_setup_lcm_entry(
    hass: HomeAssistant, data: dict = TWO_SLOTS_ON_LOCK_1, unique_id: str = "lcm"
) -> MockConfigEntry:
    """Add a Lock Code Manager entry holding ``data`` and set it up."""
    entry = MockConfigEntry(domain=DOMAIN, data=data, unique_id=unique_id)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def async_restart_lcm(
    hass: HomeAssistant, *entries: ConfigEntry, settle: bool = True
) -> None:
    """
    Restart Lock Code Manager's core: everything it kept in memory goes.

    The entries unload, every worker stops (a clear still going included,
    as when Home Assistant stops), the store is written as at Home
    Assistant's final write, and the core starts again from what it holds.
    The entries' locks are rebuilt; ``when_mock_lock_built`` keeps what a
    real lock would hold across it.

    Pass ``settle=False`` when the locks' first reads wait on a frozen clock
    the caller steps: settling here would wait on them without moving it.
    """
    for entry in entries:
        assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    await hass.data[DOMAIN][REGISTRY_KEY].stop()
    hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
    await hass.async_block_till_done()
    for key in CORE_KEYS:
        hass.data[DOMAIN].pop(key, None)
    await async_setup_core(hass)
    for entry in entries:
        assert await hass.config_entries.async_setup(entry.entry_id)
    if settle:
        await hass.async_block_till_done()


def current_entry(
    unique_id: str,
    pins: dict[int, str],
    locks: tuple[str, ...] = (LOCK_1_ENTITY_ID,),
) -> MockConfigEntry:
    """
    Build an unloaded entry in the current shape: one enabled user per slot.

    Setting it up migrates nothing. A migration from an older version reads
    the lock, through a lock instance of its own, so a test watching what an
    entry builds or reads would see that one too. The users are named
    ``user<slot>``, and hold the PINs given.
    """
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: list(locks)},
        subentries_data=user_subentries(
            {
                slot_num: {
                    CONF_NAME: f"user{slot_num}",
                    CONF_PIN: pin,
                    CONF_ENABLED: True,
                }
                for slot_num, pin in pins.items()
            }
        ),
        unique_id=unique_id,
        title=unique_id,
        version=CONFIG_ENTRY_VERSION,
    )


def write_entry_config(hass: HomeAssistant, entry: ConfigEntry, config: dict) -> bool:
    """
    Reconfigure an entry from an old-shape LCM config mapping.

    Tests that used to reconfigure with ``async_update_entry(entry, data=...)``
    have to reach both halves now — the entry and its user subentries — so they
    go through the production write path rather than restating how it splits.
    """
    return async_write_entry_config(hass, entry, EntryConfig.from_mapping(config))


def flow_users(result: dict) -> dict[str, dict]:
    """Return a create-entry flow result's users by name, without their number."""
    return {
        subentry["title"]: {
            key: value for key, value in subentry["data"].items() if key != CONF_SLOT
        }
        for subentry in result["subentries"]
    }


def flow_slots(result: dict) -> dict[str, int]:
    """Return the numbers a create-entry flow result issued, keyed by identity."""
    return {
        identity(subentry["title"]): subentry["data"][CONF_SLOT]
        for subentry in result["subentries"]
    }


def entry_users(entry: ConfigEntry) -> dict[str, dict]:
    """Return a stored entry's users by name, without their number."""
    return {
        subentry.title: {
            key: value for key, value in subentry.data.items() if key != CONF_SLOT
        }
        for subentry in entry.subentries.values()
        if subentry.subentry_type == SUBENTRY_TYPE_USER
    }


def unnumbered_user_subentry(name: str, **fields) -> ConfigSubentryData:
    """
    Build a user subentry carrying no credential position.

    Not something the integration writes -- allocation numbers everybody it
    stores. It is what hand-edited storage or a half-finished write leaves
    behind, and the guards that refuse to act on such a user are what these
    tests are for.
    """
    return ConfigSubentryData(
        data=fields,
        subentry_type=SUBENTRY_TYPE_USER,
        title=name,
        unique_id=None,
    )


# The PINs ``entry_holding_pins_on`` gives its users, every way a PIN can be
# held: by a turned-off user, and by one with no slot number.
TURNED_OFF_USERS_PIN = "2468"
UNNUMBERED_USERS_PIN = "1357"


def entry_holding_pins_on(locks: list[str]) -> MockConfigEntry:
    """
    Build another entry on ``locks`` whose users hold two PINs.

    Ann is turned off, at slot 5 so she collides with no fixture's numbers,
    and Unplaced has no slot number: each still holds their PIN. The caller
    adds it to Home Assistant, and sets it up if the test needs it loaded.
    """
    return MockConfigEntry(
        domain=DOMAIN,
        data={CONF_LOCKS: locks},
        subentries_data=[
            *user_subentries(
                {
                    5: {
                        CONF_NAME: "Ann",
                        CONF_ENABLED: False,
                        CONF_PIN: TURNED_OFF_USERS_PIN,
                    }
                }
            ),
            unnumbered_user_subentry(
                "Unplaced", **{CONF_ENABLED: True, CONF_PIN: UNNUMBERED_USERS_PIN}
            ),
        ],
        unique_id="another-entry",
        title="another entry",
        version=CONFIG_ENTRY_VERSION,
    )


async def async_configure_flow(
    hass: HomeAssistant, flow_id: str, user_input: dict[str, Any] | None = None
) -> Any:
    """
    Submit to a config flow, waiting out any progress step it shows.

    Allocation runs as a progress task (#1536): while a lock read is still
    running, the step that takes the submission shows progress, Home
    Assistant re-enters it when the task is done, and the next result is what
    the user would see. An allocation already finished when it starts -- no
    lock to read, or a read that never waits, as the mock lock's does -- is
    answered in the submitting request itself, so there is nothing to wait
    out (#1639).
    """
    result = await hass.config_entries.flow.async_configure(flow_id, user_input)
    while result["type"] == FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.flow.async_configure(flow_id)
    return result


async def async_configure_options(
    hass: HomeAssistant, flow_id: str, user_input: dict[str, Any] | None = None
) -> Any:
    """Submit to an options flow, waiting out any progress step it shows."""
    result = await hass.config_entries.options.async_configure(flow_id, user_input)
    while result["type"] == FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.options.async_configure(flow_id)
    return result


def lcm_issue_ids(hass: HomeAssistant, prefix: str = "") -> list[str]:
    """Return the ids of Lock Code Manager's repairs, those starting with ``prefix``."""
    return [
        issue.issue_id
        for issue in ir.async_get(hass).issues.values()
        if issue.domain == DOMAIN and issue.issue_id.startswith(prefix)
    ]


async def async_add_users(
    hass: HomeAssistant, entry: ConfigEntry, users: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """
    Add users to an entry through the user subentry flow, as the user does.

    The "several" step reads the locks for free numbers; the result is what
    the user sees once any progress step is waited out (#1536).
    """
    flow = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_USER), context={"source": SOURCE_USER}
    )
    flow = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {"next_step_id": "several"}
    )
    result = await hass.config_entries.subentries.async_configure(
        flow["flow_id"], {CONF_USERS: users}
    )
    while result["type"] == FlowResultType.SHOW_PROGRESS:
        await hass.async_block_till_done()
        result = await hass.config_entries.subentries.async_configure(flow["flow_id"])
    return result


def lcm_records_holding(
    caplog: pytest.LogCaptureFixture, secrets: Iterable[str]
) -> list[str]:
    """
    Return every Lock Code Manager log record that holds one of ``secrets``.

    Searches what a log handler would write: the message, and any exception
    or stack text attached to it, which ``getMessage`` leaves out.
    """
    secrets = tuple(secrets)
    held = []
    for record in caplog.records:
        if not record.name.startswith("custom_components.lock_code_manager"):
            continue
        text = record.getMessage()
        if record.exc_info:
            text += "".join(traceback.format_exception(*record.exc_info))
        if record.stack_info:
            text += record.stack_info
        if any(secret in text for secret in secrets):
            held.append(f"{record.name}: {record.getMessage()}")
    return held
