"""
The lock worker, scenario by scenario, against a fake lock and a fake clock.

Each scenario is a case from the bug catalog or the credential-model
decisions, driven only through the worker's own API.
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection
import contextlib
from dataclasses import dataclass, field

import pytest

from custom_components.lock_code_manager.core.slot import (
    LIMIT,
    WINDOW,
    Holds,
    Reading,
    Repair,
    Seen,
    Status,
)
from custom_components.lock_code_manager.core.worker import (
    BACKOFF_AFTER,
    OFFLINE_AFTER,
    ErrorKind,
    LockError,
    LockWorker,
)

SALT = b"salt"


# Real seconds a scenario may take to settle.
SETTLE_LIMIT = 5


class Clock:
    """A clock that moves only when the worker sleeps."""

    def __init__(self) -> None:
        """Start at zero."""
        self.now = 0.0

    def __call__(self) -> float:
        """Return the time."""
        return self.now

    async def sleep(self, seconds: float) -> None:
        """Move time on, and let others run."""
        self.now += seconds
        await asyncio.sleep(0)


@dataclass
class FakeLock:
    """A lock whose behaviour each scenario sets."""

    codes: dict[int, str] = field(default_factory=dict)
    # How reads show a slot holding a code.
    masked: bool = False
    # Reads say nothing about any slot.
    silent: bool = False
    refuse_value: set[str] = field(default_factory=set)
    # Refusals come from the integration, before anything is sent.
    refuse_unsent: bool = False
    refuse_clear: set[int] = field(default_factory=set)
    # Writes are acknowledged and not applied.
    ignore_writes: bool = False
    writes_unanswered: bool = False
    reads_unanswered: bool = False
    writes_never_sent: bool = False
    # The next this many writes fail before leaving for the lock.
    unsent_writes: int = 0
    # The next this many reads are answered with an error.
    read_errors: int = 0
    # Every call fails before leaving: the driver in front of the lock is down.
    driver_down: bool = False
    transient: int = 0
    hang: asyncio.Event | None = None
    # Raise something that is not a LockError on writes.
    broken: bool = False
    writes: list[tuple[int, str | None]] = field(default_factory=list)
    during_write: list = field(default_factory=list)
    after_read: list = field(default_factory=list)
    # Slots the lock does not answer for: writes go unanswered, and reads of
    # only these go unanswered too; with other slots, they are left out.
    ignored: set[int] = field(default_factory=set)
    # Reads with any ignored slot in them go unanswered, not just those slots.
    strict: bool = False
    # The host is gone: every call raises the network's own error.
    unreachable: bool = False
    # The provider says the lock cannot be reached.
    reported_down: bool = False

    def reachable(self) -> bool:
        """Say what the provider knows of the lock."""
        return not self.reported_down

    async def read(self, slots: Collection[int]) -> dict[int, Reading]:
        """Answer a read the way this lock does."""
        readings = await self._read(slots)
        for act in self.after_read:
            act()
        return readings

    async def _read(self, slots: Collection[int]) -> dict[int, Reading]:
        if self.unreachable:
            raise OSError("host unreachable")
        if self.ignored and (
            set(slots) <= self.ignored or (self.strict and self.ignored & set(slots))
        ):
            raise LockError(ErrorKind.NO_ANSWER)
        slots = [n for n in slots if n not in self.ignored]
        if self.driver_down:
            raise LockError(ErrorKind.TRANSIENT, sent=False)
        if self.read_errors:
            self.read_errors -= 1
            raise LockError(ErrorKind.TRANSIENT)
        if self.reads_unanswered:
            raise LockError(ErrorKind.NO_ANSWER)
        if self.silent:
            return {}
        return {slot: self._show(slot) for slot in slots}

    def _show(self, slot: int) -> Reading:
        if slot not in self.codes:
            return Seen.EMPTY
        return Seen.MASKED if self.masked else Holds(self.codes[slot])

    async def write(self, slot: int, pin: str | None) -> None:
        """Answer a write the way this lock does."""
        if self.unreachable:
            raise OSError("host unreachable")
        if slot in self.ignored:
            raise LockError(ErrorKind.NO_ANSWER)
        if self.writes_never_sent or self.driver_down:
            raise LockError(ErrorKind.NO_ANSWER, sent=False)
        if self.unsent_writes:
            self.unsent_writes -= 1
            raise LockError(ErrorKind.TRANSIENT, sent=False)
        self.writes.append((slot, pin))
        for act in self.during_write:
            act()
        if self.hang is not None:
            await self.hang.wait()
        if self.broken:
            raise RuntimeError("the provider broke")
        if self.writes_unanswered:
            raise LockError(ErrorKind.NO_ANSWER)
        if self.transient:
            self.transient -= 1
            raise LockError(ErrorKind.TRANSIENT)
        if pin is None and slot in self.refuse_clear:
            raise LockError(ErrorKind.SLOT_REFUSED)
        if pin is not None and pin in self.refuse_value:
            raise LockError(ErrorKind.VALUE_REFUSED, sent=not self.refuse_unsent)
        if pin is None and slot not in self.codes:
            raise LockError(ErrorKind.ALREADY_SO)
        if self.ignore_writes:
            return
        if pin is None:
            self.codes.pop(slot, None)
        else:
            self.codes[slot] = pin


@dataclass
class Rig:
    """A worker on a fake lock, with what it reported."""

    lock: FakeLock
    clock: Clock
    worker: LockWorker
    disabled: list[int]

    async def settle(self) -> None:
        """
        Run until the worker has nothing left to do.

        A worker that never settles spins the loop on the fake clock, and
        asyncio swallows pytest-timeout's exception there, so the limit is
        set here, in real time.
        """
        async with asyncio.timeout(SETTLE_LIMIT):
            await self.worker.settled()

    async def pass_(self) -> None:
        """Run one more sync pass to the end."""
        self.worker.request_sync()
        await self.settle()

    def status(self, slot: int) -> tuple[Status, Repair | None]:
        """Return a slot's status and repair."""
        return self.worker.status(slot)


@pytest.fixture
async def rig_factory():
    """Build workers on fake locks, and stop them all after the test."""
    rigs: list[Rig] = []

    def _build(lock: FakeLock, **kwargs) -> Rig:
        clock = Clock()
        disabled: list[int] = []
        worker = LockWorker(
            lock,
            SALT,
            clock=clock,
            sleep=clock.sleep,
            on_disable=disabled.append,
            **kwargs,
        )
        rig = Rig(lock, clock, worker, disabled)
        rigs.append(rig)
        return rig

    yield _build
    for rig in rigs:
        await rig.worker.stop(grace=0.1)


async def _started(rig: Rig, wants: dict[int, str | None]) -> Rig:
    for slot, pin in wants.items():
        rig.worker.want(slot, pin)
    rig.worker.start()
    await rig.settle()
    return rig


# Converging --------------------------------------------------------------------


async def test_a_fresh_lock_is_read_written_once_and_left_alone(rig_factory) -> None:
    """Read first, write what differs, confirm, then nothing more."""
    rig = await _started(rig_factory(FakeLock({2: "5678"})), {1: "1234", 2: "5678"})

    assert rig.lock.writes == [(1, "1234")]
    assert rig.status(1) == (Status.IN_SYNC, None)
    assert rig.status(2) == (Status.IN_SYNC, None)
    await rig.pass_()
    assert rig.lock.writes == [(1, "1234")]


async def test_nothing_is_written_before_the_lock_is_read(rig_factory) -> None:
    """A lock that never answers a read is never written."""
    rig = await _started(rig_factory(FakeLock(reads_unanswered=True)), {1: "1234"})

    assert rig.lock.writes == []
    assert rig.status(1) == (Status.UNKNOWN, None)


async def test_a_write_only_lock_is_written_once_and_trusted(rig_factory) -> None:
    """A lock that never says what it holds: written once, never rewritten."""
    rig = await _started(rig_factory(FakeLock(silent=True)), {1: "1234"})
    for _ in range(3):
        await rig.pass_()

    assert rig.lock.writes == [(1, "1234")]
    assert rig.status(1) == (Status.IN_SYNC, None)


async def test_a_write_only_lock_clearing_an_empty_slot_settles(rig_factory) -> None:
    """Already so is success: the clear is trusted, never resent."""
    rig = await _started(rig_factory(FakeLock(silent=True)), {1: None})
    for _ in range(2):
        await rig.pass_()

    assert rig.lock.writes == [(1, None)]
    assert rig.status(1) == (Status.IN_SYNC, None)
    assert rig.worker.slots[1].tries == 0


async def test_a_lock_never_reached_is_not_offline(rig_factory) -> None:
    """A lock still coming up is backed off, never reported offline (#1257)."""
    rig = await _started(rig_factory(FakeLock(reads_unanswered=True)), {1: "1234"})
    for _ in range(OFFLINE_AFTER * 2):
        with pytest.raises(LockError):
            await rig.worker.read([1])
        await rig.settle()

    assert not rig.worker.reachable
    assert not rig.worker.offline


async def test_a_masked_lock_is_trusted_for_what_was_written(rig_factory) -> None:
    """A lock that hides PINs: in sync on the write, cleared when emptied."""
    rig = await _started(rig_factory(FakeLock(masked=True)), {1: "1234"})
    assert rig.status(1) == (Status.IN_SYNC, None)

    rig.worker.want(1, None)
    await rig.settle()
    assert rig.lock.writes == [(1, "1234"), (1, None)]
    assert rig.status(1) == (Status.IN_SYNC, None)


async def test_transient_failures_below_the_limit_end_in_sync(rig_factory) -> None:
    """Two transient failures, then the write lands: in sync, not suspended."""
    rig = await _started(rig_factory(FakeLock(transient=2)), {1: "1234"})
    for _ in range(2):
        await rig.pass_()

    assert rig.status(1) == (Status.IN_SYNC, None)
    assert rig.lock.codes == {1: "1234"}


# Holding -----------------------------------------------------------------------


async def test_a_refused_pin_disables_the_slot_then_its_clear_goes_through(
    rig_factory,
) -> None:
    """A refused value is not resent; once the slot is off, its code is cleared."""
    lock = FakeLock({1: "9999"}, refuse_value={"1234"})
    rig = await _started(rig_factory(lock), {1: "1234"})

    assert rig.status(1) == (Status.SUSPENDED, Repair.SLOT_DISABLED)
    assert rig.disabled == [1]
    await rig.pass_()
    assert rig.lock.writes == [(1, "1234")]

    rig.worker.want(1, None)  # the integration turned the slot off
    await rig.settle()
    assert rig.status(1) == (Status.IN_SYNC, None)
    assert rig.lock.codes == {}


async def test_a_refused_clear_is_held_and_warns_the_code_may_remain(
    rig_factory,
) -> None:
    """A clear the lock refuses is never resent in a loop (#1633)."""
    rig = await _started(
        rig_factory(FakeLock({1: "1234"}, refuse_clear={1})), {1: None}
    )
    await rig.pass_()

    assert rig.status(1) == (Status.SUSPENDED, Repair.CODE_MAY_REMAIN)
    assert rig.lock.writes == [(1, None)]


async def test_a_write_the_lock_does_not_keep_is_held_after_three(
    rig_factory,
) -> None:
    """Acknowledged and not applied (#1622, #1641): the read-back says so."""
    rig = await _started(rig_factory(FakeLock(ignore_writes=True)), {1: "1234"})
    for _ in range(5):
        await rig.pass_()

    assert rig.status(1) == (Status.SUSPENDED, Repair.CANT_SET)
    assert len(rig.lock.writes) == 3


async def test_a_keypad_fight_trips_however_often_it_is_read(rig_factory) -> None:
    """Each code taken back counts once (#776); agreement never clears them."""
    rig = await _started(rig_factory(FakeLock()), {1: "1234"})
    for round_ in range(3):
        rig.lock.codes[1] = f"000{round_}"
        rig.worker.push(1, Holds(f"000{round_}"))
        # Polled twice before the rewrite: still one failure.
        await rig.worker.read([1])
        await rig.settle()

    assert rig.status(1) == (Status.SUSPENDED, Repair.CANT_SET)
    assert rig.lock.writes == [(1, "1234")] * 3


async def test_failures_spread_wider_than_the_window_never_hold(rig_factory) -> None:
    """Three failures over more than five minutes are not three in five."""
    rig = await _started(rig_factory(FakeLock()), {1: "1234"})
    for round_ in range(4):
        rig.clock.now += 200
        rig.worker.push(1, Holds(f"000{round_}"))
        await rig.settle()

    assert rig.status(1) == (Status.IN_SYNC, None)


async def test_a_new_target_lifts_a_hold(rig_factory) -> None:
    """Changing what is wanted starts over; so does the user acknowledging."""
    lock = FakeLock(ignore_writes=True)
    rig = await _started(rig_factory(lock), {1: "1234"})
    for _ in range(3):
        await rig.pass_()
    assert rig.status(1)[0] is Status.SUSPENDED

    lock.ignore_writes = False
    rig.worker.want(1, "4321")
    await rig.settle()
    assert rig.status(1) == (Status.IN_SYNC, None)


async def test_a_release_clear_the_lock_ignores_ends_in_a_repair(rig_factory) -> None:
    """A code cleared on its way out gets two tries, then a warning (decision 6)."""
    rig = await _started(
        rig_factory(FakeLock({5: "1234"}, ignore_writes=True)), {5: "1234"}
    )
    rig.worker.want(5, None, release=True)
    for _ in range(3):
        await rig.pass_()

    assert rig.status(5) == (Status.SUSPENDED, Repair.CODE_MAY_REMAIN)
    assert rig.lock.writes == [(5, None)] * 2


async def test_a_release_clear_that_lands_drops_the_slot(rig_factory) -> None:
    """Once its code is off the lock, a released slot is no longer tracked."""
    rig = await _started(rig_factory(FakeLock({5: "1234"})), {5: "1234"})
    rig.worker.want(5, None, release=True)
    await rig.settle()

    assert 5 not in rig.worker.slots
    assert rig.lock.codes == {}


# The lock as a whole -------------------------------------------------------------


async def test_writes_failing_while_reads_answer_hold_the_slot(rig_factory) -> None:
    """Down for writes, up for reads (#1644): the slot holds; the lock is not offline."""
    rig = await _started(rig_factory(FakeLock(writes_unanswered=True)), {1: "1234"})
    for _ in range(4):
        await rig.pass_()

    assert rig.status(1) == (Status.SUSPENDED, Repair.CANT_SET)
    assert rig.worker.reachable
    assert not rig.worker.offline


async def test_a_lock_that_stops_answering_backs_off_then_reports_offline(
    rig_factory,
) -> None:
    """Reached once, then silent: backoff, and offline only after many no-answers."""
    lock = FakeLock({1: "1234"})
    rig = await _started(rig_factory(lock), {1: "1234"})
    lock.reads_unanswered = True
    for _ in range(BACKOFF_AFTER):
        with pytest.raises(LockError):
            await rig.worker.read([1])
    assert not rig.worker.reachable

    for _ in range(OFFLINE_AFTER * 2):
        if rig.worker.offline:
            break
        await asyncio.sleep(0)
        await rig.settle()
    assert rig.worker.offline

    lock.reads_unanswered = False
    for _ in range(50):
        if rig.worker.reachable:
            break
        await asyncio.sleep(0)
        await rig.settle()
    assert rig.worker.reachable
    assert not rig.worker.offline


async def test_writes_that_never_leave_while_reads_answer_hold_the_slot(
    rig_factory,
) -> None:
    """
    A lock that answers reads but takes no write is the slot's to give up on.

    Each write is read back, the read answers, and the slot stays out of sync:
    three tries hold it, however the lock's no-answers reset (#1644).
    """
    lock = FakeLock(writes_never_sent=True)
    rig = await _started(rig_factory(lock), {1: "1234"})

    assert rig.status(1) == (Status.SUSPENDED, Repair.CANT_SET)
    assert rig.worker.slots[1].tries == LIMIT


async def test_a_write_that_hangs_while_reads_answer_holds_the_slot(
    rig_factory,
) -> None:
    """
    A hung write is cut off within its budget (#1523) and read back.

    The lock answers the read-back, so the write that got no answer is the
    slot's failure: three, and the slot holds instead of retrying for ever
    (#1644).
    """
    rig = await _started(
        rig_factory(FakeLock(hang=asyncio.Event()), budget=0.05, gap=0.0),
        {1: "1234"},
    )

    assert rig.status(1) == (Status.SUSPENDED, Repair.CANT_SET)
    assert len(rig.lock.writes) == 3
    assert rig.worker.reachable


async def test_a_report_during_the_slot_s_own_write_is_dropped(rig_factory) -> None:
    """The write's read-back is newer than anything said while it was out."""
    lock = FakeLock()
    rig = rig_factory(lock)
    lock.during_write.append(lambda: rig.worker.push(1, Seen.EMPTY))
    await _started(rig, {1: "1234"})
    await rig.settle()

    assert rig.status(1) == (Status.IN_SYNC, None)
    assert lock.writes == [(1, "1234")]


async def test_exchanges_are_paced(rig_factory) -> None:
    """Consecutive exchanges with the lock are at least the gap apart."""
    times: list[float] = []
    lock = FakeLock()
    rig = rig_factory(lock, gap=2.0)
    original = lock.read

    async def _timed(slots):
        times.append(rig.clock.now)
        return await original(slots)

    lock.read = _timed  # type: ignore[method-assign]
    await _started(rig, {1: "1234", 2: "5678"})

    assert all(b - a >= 2.0 for a, b in zip(times, times[1:], strict=False))
    assert len(times) >= 3


async def test_stopping_cuts_off_a_job_past_its_grace(rig_factory) -> None:
    """A job still running when the grace runs out is cancelled."""
    hang = asyncio.Event()
    rig = rig_factory(FakeLock(hang=hang), budget=60.0)
    rig.worker.want(1, "1234")
    rig.worker.start()
    for _ in range(20):
        await asyncio.sleep(0)
    assert rig.lock.writes == [(1, "1234")]

    await asyncio.wait_for(rig.worker.stop(grace=0.05), timeout=5)


# Review round 1 -----------------------------------------------------------------


async def test_what_was_sent_is_what_is_recorded(rig_factory) -> None:
    """A target changed while its write was out is written next; never assumed."""
    lock = FakeLock(masked=True)
    rig = rig_factory(lock)
    lock.during_write.append(
        lambda: rig.worker.want(1, "5555") if len(lock.writes) == 1 else None
    )
    await _started(rig, {1: "1234"})

    assert lock.writes == [(1, "1234"), (1, "5555")]
    assert lock.codes == {1: "5555"}
    assert rig.status(1) == (Status.IN_SYNC, None)


async def test_a_report_queued_before_a_write_is_dropped(rig_factory) -> None:
    """A stale report for a slot the pass then rewrites counts nothing."""
    lock = FakeLock({1: "0000", 2: "0000"})
    rig = rig_factory(lock)
    lock.during_write.append(
        lambda: rig.worker.push(2, Holds("0000")) if lock.writes[-1][0] == 1 else None
    )
    await _started(rig, {1: "1111", 2: "2222"})

    assert rig.worker.slots[2].fights == []
    assert lock.writes == [(1, "1111"), (2, "2222")]
    assert rig.status(2) == (Status.IN_SYNC, None)


async def test_a_broken_provider_fails_its_slot_not_the_pass(rig_factory) -> None:
    """Any error a provider raises counts against that slot; others still sync."""
    lock = FakeLock(broken=True)
    rig = await _started(rig_factory(lock), {1: "1234", 2: "5678"})

    for slot in (1, 2):
        assert rig.status(slot) == (Status.SUSPENDED, Repair.CANT_SET)
        assert lock.writes.count((slot, "1234" if slot == 1 else "5678")) == 3


@pytest.mark.parametrize("reported", [True, False], ids=["reported", "unreported"])
async def test_an_outage_holds_no_slot(rig_factory, reported) -> None:
    """
    A lock that has gone is the lock's problem: no slot is held.

    Reported by the provider, it charges nothing. Unreported, a slot whose
    sync ends before the lock backs off is charged once at most, and the
    first fresh read that agrees after it comes back clears that.
    """
    slots = range(1, 6)
    lock = FakeLock()
    rig = await _started(rig_factory(lock), dict.fromkeys(slots, "1234"))
    lock.writes_unanswered = True
    lock.reads_unanswered = True
    lock.reported_down = reported
    for n in slots:
        rig.worker.want(n, "4321")
    for _ in range(20):
        await rig.settle()
        await asyncio.sleep(0)

    assert not rig.worker.reachable
    assert all(rig.status(n) == (Status.OUT_OF_SYNC, None) for n in slots)
    tries = [rig.worker.slots[n].tries for n in slots]
    assert max(tries) == (0 if reported else 1)

    lock.writes_unanswered = lock.reads_unanswered = lock.reported_down = False
    for _ in range(50):
        if all(rig.status(n)[0] is Status.IN_SYNC for n in slots):
            break
        await rig.settle()
        await asyncio.sleep(0)
    assert all(rig.status(n) == (Status.IN_SYNC, None) for n in slots)
    assert all(rig.worker.slots[n].tries == 0 for n in slots)


async def test_stopping_answers_waiting_reads_and_refuses_new_ones(rig_factory) -> None:
    """Nobody is left waiting on a stopped worker."""
    hang = asyncio.Event()
    lock = FakeLock(hang=hang)
    rig = rig_factory(lock)
    rig.worker.want(1, "1234")
    rig.worker.start()
    for _ in range(20):
        await asyncio.sleep(0)
    waiting = asyncio.ensure_future(rig.worker.read([1]))
    await asyncio.sleep(0)

    await rig.worker.stop(grace=0.05)
    with pytest.raises(LockError):
        await waiting
    with pytest.raises(LockError):
        await rig.worker.read([1])


async def test_a_caller_that_gave_up_is_not_an_error(rig_factory, caplog) -> None:
    """A read whose caller stopped waiting is answered quietly."""
    rig = await _started(rig_factory(FakeLock()), {1: "1234"})
    task = asyncio.ensure_future(rig.worker.read([1]))
    await asyncio.sleep(0)
    task.cancel()
    await rig.settle()

    assert "failed" not in caplog.text


async def test_every_job_has_a_time_limit(rig_factory) -> None:
    """A slot's sync that hangs is cut off, and the next job runs (#1523)."""
    hang = asyncio.Event()
    lock = FakeLock(hang=hang)
    rig = rig_factory(lock, budget=0.05, gap=0.0)
    await _started(rig, {1: "1234"})

    lock.hang = None
    assert await rig.worker.read([1]) == {1: Seen.EMPTY}


# Review round 2 -----------------------------------------------------------------


async def test_drift_found_by_a_poll_is_put_right(rig_factory) -> None:
    """A code changed at the keypad and found by a read is rewritten."""
    lock = FakeLock()
    rig = await _started(rig_factory(lock), {1: "1234"})
    del lock.codes[1]
    await rig.worker.read()
    await rig.settle()

    assert lock.codes == {1: "1234"}
    assert rig.status(1) == (Status.IN_SYNC, None)


async def test_a_job_cut_off_by_stop_still_answers_its_caller(rig_factory) -> None:
    """A read running when stop's grace runs out is answered, not left hanging."""
    lock = FakeLock()
    rig = await _started(rig_factory(lock), {1: "1234"})
    hang = asyncio.Event()

    async def _hung(slots):
        await hang.wait()

    lock.read = _hung  # type: ignore[method-assign]
    waiting = asyncio.ensure_future(rig.worker.read([1]))
    for _ in range(5):
        await asyncio.sleep(0)
    await rig.worker.stop(grace=0.05)

    with pytest.raises(LockError):
        await asyncio.wait_for(waiting, timeout=5)


async def test_a_lock_back_after_backoff_syncs_what_it_dropped(rig_factory) -> None:
    """Any answer after a backoff, even a failure, brings the slots back."""
    lock = FakeLock({1: "1234"})
    rig = await _started(rig_factory(lock), {1: "1234"})
    lock.reads_unanswered = True
    for _ in range(BACKOFF_AFTER):
        with pytest.raises(LockError):
            await rig.worker.read([1])
    rig.worker.want(1, "4321")  # dropped while backing off
    await rig.settle()
    assert lock.writes == []

    lock.reads_unanswered = False
    original = lock.read
    calls = 0

    async def _first_fails(slots):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise LockError(ErrorKind.TRANSIENT)
        return await original(slots)

    lock.read = _first_fails  # type: ignore[method-assign]
    for _ in range(50):
        if lock.codes.get(1) == "4321":
            break
        await asyncio.sleep(0)
        await rig.settle()
    assert lock.codes == {1: "4321"}


async def test_a_provider_timeout_on_a_probe_tries_again_later(rig_factory) -> None:
    """
    A provider's own timeout is no answer: the lock stays down, and a later probe
    brings it back.
    """
    lock = FakeLock({1: "1234"})
    rig = await _started(rig_factory(lock), {1: "1234"})
    lock.reads_unanswered = True
    for _ in range(BACKOFF_AFTER):
        with pytest.raises(LockError):
            await rig.worker.read([1])
    original = lock.read
    calls = 0

    async def _flaky(slots):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("provider timeout")
        lock.reads_unanswered = False
        return await original(slots)

    lock.read = _flaky  # type: ignore[method-assign]
    for _ in range(50):
        if rig.worker.reachable:
            break
        await asyncio.sleep(0)
        await rig.settle()
    assert rig.worker.reachable
    assert calls == 2


async def test_the_first_read_comes_first(rig_factory) -> None:
    """Slots registered before start are read once, together, then synced."""
    lock = FakeLock({1: "1", 2: "2", 3: "3"})
    reads: list[tuple[int, ...]] = []
    original = lock.read

    async def _note(slots):
        reads.append(tuple(slots))
        return await original(slots)

    lock.read = _note  # type: ignore[method-assign]
    await _started(rig_factory(lock), {1: "1", 2: "2", 3: "3"})

    assert reads == [(1, 2, 3)]


async def test_settled_returns_after_a_stop(rig_factory) -> None:
    """Stopping mid-job leaves nothing for settled() to wait on for ever."""
    hang = asyncio.Event()
    rig = rig_factory(FakeLock(hang=hang), budget=60.0)
    rig.worker.want(1, "1234")
    rig.worker.start()
    for _ in range(20):
        await asyncio.sleep(0)
    stop = asyncio.ensure_future(rig.worker.stop(grace=5))
    await asyncio.sleep(0)
    hang.set()
    await asyncio.wait_for(stop, timeout=5)

    await asyncio.wait_for(rig.worker.settled(), timeout=5)


async def test_a_pin_refused_before_sending_disables_the_slot(rig_factory) -> None:
    """Refused by the provider before it left: a refusal, not a lock no-answer."""
    lock = FakeLock()
    rig = rig_factory(lock)
    original = lock.write

    async def _too_short(slot, pin):
        if pin is not None and len(pin) < 4:
            raise LockError(ErrorKind.VALUE_REFUSED, sent=False)
        return await original(slot, pin)

    lock.write = _too_short  # type: ignore[method-assign]
    await _started(rig, {1: "12"})

    assert rig.status(1) == (Status.SUSPENDED, Repair.SLOT_DISABLED)
    assert rig.disabled == [1]
    assert rig.worker.reachable


async def test_a_slot_claimed_again_mid_release_is_not_dropped(rig_factory) -> None:
    """
    A release finishing after the slot was taken again leaves the new owner alone.

    The lock is silent, so the old slot's own record of its clear is what
    puts it in sync -- reads only ever reach the slot now in place.
    """
    lock = FakeLock({5: "1234"}, silent=True)
    rig = rig_factory(lock)

    def _reclaim() -> None:
        if lock.writes[-1] == (5, None):
            rig.worker.forget(5)
            rig.worker.want(5, "2468")

    lock.during_write.append(_reclaim)
    await _started(rig, {5: "1234"})
    rig.worker.want(5, None, release=True)
    await rig.settle()

    assert 5 in rig.worker.slots
    assert lock.codes == {5: "2468"}


async def test_a_failed_first_read_still_syncs_every_slot(rig_factory) -> None:
    """The first read failing leaves no slot stranded, never read or written."""
    lock = FakeLock(read_errors=1)
    await _started(rig := rig_factory(lock), {1: "1234"})

    assert lock.codes == {1: "1234"}
    assert rig.status(1) == (Status.IN_SYNC, None)


async def test_a_write_that_never_left_is_tried_again(rig_factory) -> None:
    """A write that failed before reaching the lock is retried, not forgotten."""
    lock = FakeLock(unsent_writes=1)
    rig = await _started(rig_factory(lock), {1: "1234"})

    assert lock.codes == {1: "1234"}
    assert rig.status(1) == (Status.IN_SYNC, None)


async def test_a_read_back_answered_with_an_error_is_retried_and_counted(
    rig_factory,
) -> None:
    """A lock that answers every read with an error holds the slot, not loops."""
    lock = FakeLock()
    rig = await _started(rig_factory(lock), {})
    lock.read_errors = 1_000
    rig.worker.want(1, "1234")
    await rig.settle()

    assert rig.status(1) == (Status.SUSPENDED, Repair.CANT_SET)
    assert rig.worker.reachable


async def test_a_release_of_an_empty_slot_is_dropped(rig_factory) -> None:
    """A slot given up that the lock already shows empty is let go at once."""
    rig = await _started(rig_factory(FakeLock()), {})
    rig.worker.want(2, None, release=True)
    await rig.settle()

    assert 2 not in rig.worker.slots


async def test_a_release_found_empty_by_the_first_read_is_dropped(rig_factory) -> None:
    """A release registered before start, already empty, is let go too."""
    rig = rig_factory(FakeLock())
    rig.worker.want(2, None, release=True)
    rig.worker.start()
    await rig.settle()

    assert 2 not in rig.worker.slots


async def test_a_driver_that_sends_nothing_backs_the_lock_off(rig_factory) -> None:
    """Failures that never leave for the lock are the lock not answering."""
    lock = FakeLock({1: "1234"})
    rig = await _started(rig_factory(lock), {1: "1234"})
    lock.driver_down = True
    for _ in range(BACKOFF_AFTER):
        with pytest.raises(LockError):
            await rig.worker.read([1])

    assert not rig.worker.reachable


async def test_a_refusal_before_sending_is_an_answer(rig_factory) -> None:
    """The integration refusing a PIN is not the lock going quiet."""
    lock = FakeLock(refuse_value={"1"}, refuse_unsent=True)
    slots = range(1, BACKOFF_AFTER + 2)
    # Read already, so the refusals come back to back with no read between.
    rig = await _started(rig_factory(lock), dict.fromkeys(slots))
    for slot in slots:
        rig.worker.want(slot, "1")
    await rig.settle()

    assert rig.worker.reachable


async def test_a_report_during_a_write_survives_a_failed_read_back(
    rig_factory,
) -> None:
    """
    A push is dropped only for a read-back that actually came back.

    On a masked lock a failed read-back leaves the slot trusting its write,
    so the lock's own report that the slot is empty is the only news: it is
    a fight, and the slot is written again.
    """
    lock = FakeLock(masked=True)
    rig = await _started(rig_factory(lock), {1: "0000"})

    def _report_empty() -> None:
        lock.during_write.clear()
        lock.read_errors = 1
        rig.worker.push(1, Seen.EMPTY)

    lock.during_write.append(_report_empty)
    rig.worker.want(1, "1234")
    await rig.settle()

    assert lock.writes[-2:] == [(1, "1234"), (1, "1234")]
    assert len(rig.worker.slots[1].fights) == 1


async def test_a_provider_bug_is_logged(rig_factory, caplog) -> None:
    """Something a provider raises that is not a LockError leaves a traceback."""
    await _started(rig_factory(FakeLock(broken=True)), {1: "1234"})

    assert "Lock provider failed" in caplog.text
    assert "the provider broke" in caplog.text


async def test_a_refusal_of_a_released_write_spares_the_new_owner(
    rig_factory,
) -> None:
    """A write refused after its slot changed hands does not turn off the new one."""
    lock = FakeLock(refuse_value={"1111"})
    rig = rig_factory(lock)

    def _hand_over() -> None:
        lock.during_write.clear()
        rig.worker.forget(3)
        rig.worker.want(3, "1111")

    lock.during_write.append(_hand_over)
    await _started(rig, {3: "1111"})

    assert rig.disabled == [3]


async def test_settled_returns_after_a_stop_past_its_grace(rig_factory) -> None:
    """A stop that had to cancel leaves nothing behind for settled() to wait on."""
    hang = asyncio.Event()
    rig = rig_factory(FakeLock(hang=hang), budget=60.0)
    rig.worker.want(1, "1234")
    rig.worker.start()
    for _ in range(20):
        await asyncio.sleep(0)
    await rig.worker.stop(grace=0.01)

    await rig.settle()


async def test_a_second_stop_waits_for_the_first(rig_factory) -> None:
    """Two unloads stopping together both return only once the worker has."""
    hang = asyncio.Event()
    rig = rig_factory(FakeLock(hang=hang), budget=60.0)
    rig.worker.want(1, "1234")
    rig.worker.start()
    for _ in range(20):
        await asyncio.sleep(0)
    first = asyncio.ensure_future(rig.worker.stop(grace=5))
    await asyncio.sleep(0)
    second = asyncio.ensure_future(rig.worker.stop(grace=5))
    for _ in range(5):
        await asyncio.sleep(0)

    assert not second.done()
    hang.set()
    await asyncio.wait_for(asyncio.gather(first, second), 5)


async def test_writes_answered_with_reads_unanswered_hold_the_slot(rig_factory) -> None:
    """
    A lock that takes writes but never answers a read-back holds the slot.

    Each answered write resets the lock's no-answers, so the lock never backs
    off; the slot's tries do not reset, so it stops after three.
    """
    lock = FakeLock({1: "0000"})
    rig = await _started(rig_factory(lock), {1: "0000"})
    lock.reads_unanswered = True
    rig.worker.want(1, "1234")
    await rig.settle()

    assert rig.status(1) == (Status.SUSPENDED, Repair.CANT_SET)
    assert len(lock.writes) == LIMIT


async def test_many_slots_failing_in_turn_all_hold(rig_factory) -> None:
    """
    Tries do not age out, however long a round of many slots takes.

    Forty slots each failing once a round would never put three failures
    inside five minutes; counted as tries, every one holds after three.
    """
    slots = range(1, 41)
    lock = FakeLock(transient=1_000_000)
    rig = await _started(rig_factory(lock), dict.fromkeys(slots, "1234"))

    assert rig.clock.now > WINDOW
    assert all(rig.status(n) == (Status.SUSPENDED, Repair.CANT_SET) for n in slots)
    assert len(lock.writes) == LIMIT * len(slots)


async def test_a_stop_cut_off_does_not_hang_the_next(rig_factory) -> None:
    """A stop() whose caller is cancelled still lets a later stop() return."""
    hang = asyncio.Event()
    rig = rig_factory(FakeLock(hang=hang), budget=60.0)
    rig.worker.want(1, "1234")
    rig.worker.start()
    for _ in range(20):
        await asyncio.sleep(0)
    first = asyncio.ensure_future(rig.worker.stop(grace=60))
    await asyncio.sleep(0)
    first.cancel()

    # Straight after the cancel: the worker task has not ended yet.
    await asyncio.wait_for(rig.worker.stop(grace=0.01), 5)
    assert rig.worker._task is not None and rig.worker._task.done()
    await asyncio.gather(first, return_exceptions=True)
    await rig.settle()


async def test_a_write_retargeted_while_out_is_not_read_back(rig_factory) -> None:
    """The old target's write is not checked: the new one is synced on its own."""
    lock = FakeLock({1: "0000"})
    rig = await _started(rig_factory(lock), {1: "0000"})
    reads: list[tuple[int, ...]] = []
    original = lock.read

    async def _note(slots):
        reads.append(tuple(slots))
        return await original(slots)

    lock.read = _note  # type: ignore[method-assign]

    def _retarget() -> None:
        lock.during_write.clear()
        rig.worker.want(1, "5555")

    lock.during_write.append(_retarget)
    rig.worker.want(1, "1234")
    await rig.settle()

    assert lock.writes[-2:] == [(1, "1234"), (1, "5555")]
    # The new target's sync reads first -- the old write may have landed --
    # then reads its own write back.
    assert reads == [(1,), (1,)]
    assert rig.status(1) == (Status.IN_SYNC, None)


@pytest.mark.parametrize("lift", ["resume", "new target"])
async def test_lifting_a_hold_starts_the_tries_over(rig_factory, lift) -> None:
    """After a hold is lifted, two more failed tries do not hold again."""
    lock = FakeLock(transient=LIMIT)
    rig = await _started(rig_factory(lock), {1: "1234"})
    assert rig.status(1) == (Status.SUSPENDED, Repair.CANT_SET)

    lock.transient = LIMIT - 1
    if lift == "resume":
        rig.worker.resume(1)
    else:
        rig.worker.want(1, "4321")
    await rig.settle()

    assert rig.status(1) == (Status.IN_SYNC, None)


async def test_agreeing_starts_the_tries_over(rig_factory) -> None:
    """Tries count toward one stretch out of sync, not a slot's whole life."""
    lock = FakeLock(transient=LIMIT - 1)
    rig = await _started(rig_factory(lock), {1: "1234"})
    assert rig.status(1) == (Status.IN_SYNC, None)

    lock.codes[1] = "9999"
    lock.transient = LIMIT - 1
    rig.worker.push(1, Holds("9999"))
    await rig.settle()

    assert rig.status(1) == (Status.IN_SYNC, None)


async def test_a_retarget_mid_write_charges_the_new_target_nothing(
    rig_factory,
) -> None:
    """The old target's failed write is not a try against the new one."""
    lock = FakeLock({1: "0000"})
    rig = await _started(rig_factory(lock), {1: "0000"})

    def _retarget() -> None:
        lock.during_write.clear()
        rig.worker.want(1, "5555")
        # This write fails too, then the new target's first LIMIT - 1 do.
        lock.transient = LIMIT

    lock.during_write.append(_retarget)
    rig.worker.want(1, "1234")
    await rig.settle()

    assert rig.status(1) == (Status.IN_SYNC, None)


async def test_a_fight_drops_trust_in_the_write(rig_factory) -> None:
    """
    After a masked lock reports the slot empty, its next masked report is
    someone else's code, not the PIN that was written.
    """
    lock = FakeLock(masked=True)
    rig = await _started(rig_factory(lock), {1: "1234"})
    writes = len(lock.writes)

    rig.worker.push(1, Seen.EMPTY)
    rig.worker.push(1, Seen.MASKED)
    await rig.settle()

    assert len(lock.writes) == writes + 1


async def test_a_report_from_before_a_write_is_dropped_without_a_read_back(
    rig_factory,
) -> None:
    """A report older than the write is stale even if the read-back fails."""
    lock = FakeLock(masked=True)
    rig = await _started(rig_factory(lock), {1: "0000"})

    lock.during_write.append(lambda: setattr(lock, "read_errors", 1))
    rig.worker.want(1, "1234")
    # Taken in after the sync was queued, before its write started.
    rig.worker.push(1, Seen.EMPTY)
    await rig.settle()

    assert lock.writes[-1:] == [(1, "1234")]
    assert len([w for w in lock.writes if w == (1, "1234")]) == 1
    assert rig.status(1) == (Status.IN_SYNC, None)


async def test_an_unreachable_host_backs_off_and_holds_nothing(rig_factory) -> None:
    """A provider's own network error is no answer, not the lock refusing."""
    lock = FakeLock({1: "1234"})
    rig = await _started(rig_factory(lock), {1: "1234"})
    lock.unreachable = True
    rig.worker.want(1, "4321")
    for _ in range(20):
        await rig.settle()
        await asyncio.sleep(0)

    assert not rig.worker.reachable
    assert rig.status(1)[0] is Status.OUT_OF_SYNC


async def test_a_slot_the_lock_ignores_is_held(rig_factory) -> None:
    """
    One slot going unanswered while the lock answers for others is the
    slot's problem: probes bring the lock back, and the slot's tries add up.
    """
    lock = FakeLock(ignored={7})
    rig = await _started(rig_factory(lock), {1: "1111"})
    rig.worker.want(7, "7777")
    for _ in range(50):
        if rig.status(7)[0] is Status.SUSPENDED:
            break
        await rig.settle()
        await asyncio.sleep(0)

    assert rig.status(7) == (Status.SUSPENDED, Repair.CANT_SET)
    assert rig.status(1) == (Status.IN_SYNC, None)


async def test_a_push_never_starts_the_tries_over(rig_factory) -> None:
    """
    A push may come from a cache: agreeing, it does not wipe the tries.

    The lock takes no write, then stops answering: a cached report that the
    slot agrees arrives while it backs off, with no fresh read after it.
    """
    lock = FakeLock({1: "0000"}, ignore_writes=True)
    rig = await _started(rig_factory(lock), {1: "0000"})

    def _go_quiet() -> None:
        if lock.writes:
            lock.after_read.clear()
            lock.writes_unanswered = True
            lock.reads_unanswered = True

    lock.after_read.append(_go_quiet)
    rig.worker.want(1, "1234")
    await rig.settle()
    assert not rig.worker.reachable
    tries = rig.worker.slots[1].tries
    assert tries > 0

    rig.worker.push(1, Holds("1234"))
    await rig.settle()

    assert rig.worker.slots[1].tries == tries


async def test_a_release_reclaimed_during_its_read_back_spares_the_new_owner(
    rig_factory,
) -> None:
    """A release ending after its slot was taken again leaves the new slot be."""
    lock = FakeLock({5: "1234"}, silent=True)
    rig = await _started(rig_factory(lock), {5: "1234"})

    def _reclaim() -> None:
        if lock.writes[-1:] == [(5, None)]:
            lock.after_read.clear()
            rig.worker.forget(5)
            rig.worker.want(5, "2468")

    lock.after_read.append(_reclaim)
    rig.worker.want(5, None, release=True)
    await rig.settle()

    assert 5 in rig.worker.slots
    assert lock.codes == {5: "2468"}


async def test_a_release_flip_mid_sync_is_not_charged(rig_factory) -> None:
    """Turning a clear into a release mid-write gives the release its full limit."""
    lock = FakeLock({3: "1111"}, ignore_writes=True)
    rig = await _started(rig_factory(lock), {3: "1111"})

    def _release() -> None:
        lock.during_write.clear()
        rig.worker.want(3, None, release=True)

    lock.during_write.append(_release)
    rig.worker.want(3, None)
    await rig.settle()

    assert rig.status(3) == (Status.SUSPENDED, Repair.CODE_MAY_REMAIN)
    assert lock.writes[-3:] == [(3, None)] * 3


async def test_a_failing_change_callback_does_not_stop_the_lock(rig_factory) -> None:
    """The integration's callback raising is logged; the worker carries on."""

    def _boom() -> None:
        raise RuntimeError("entity update failed")

    lock = FakeLock()
    await _started(rig_factory(lock, on_change=_boom), {1: "1234"})

    assert lock.codes == {1: "1234"}


async def test_no_read_back_once_the_lock_backs_off(rig_factory) -> None:
    """A write that tips the lock into backoff is not followed by a read."""
    lock = FakeLock({1: "0000"})
    rig = await _started(rig_factory(lock), {1: "0000"})
    reads = 0
    original = lock.read

    async def _count(slots):
        nonlocal reads
        reads += 1
        return await original(slots)

    lock.read = _count  # type: ignore[method-assign]
    lock.writes_unanswered = True
    lock.reads_unanswered = True
    rig.worker.want(1, "1234")
    await rig.settle()

    # One write and its read-back, then a write that tips the lock over.
    assert reads == 1
    assert not rig.worker.reachable


async def test_a_report_from_before_a_read_is_dropped(rig_factory) -> None:
    """A report taken in before a fresh read of its slot is older than it."""
    lock = FakeLock({1: "1234"})
    rig = rig_factory(lock)
    rig.worker.want(1, "1234")
    rig.worker.start()
    rig.worker.push(1, Holds("0000"))
    await rig.settle()

    assert lock.writes == []
    assert rig.worker.slots[1].fights == []


@pytest.mark.parametrize("reported", [True, False], ids=["reported", "unreported"])
@pytest.mark.parametrize(
    ("added", "release"),
    [({9: None}, True), ({5: "5555"}, False), ({5: "5555", 6: "6666"}, False)],
    ids=["release", "new slot", "two new slots"],
)
async def test_an_outage_holds_no_slot_never_read(
    rig_factory, added, release, reported
) -> None:
    """Slots added during an outage, never read, are not held either."""
    lock = FakeLock({1: "1111"})
    rig = await _started(rig_factory(lock), {1: "1111"})
    lock.writes_unanswered = True
    lock.reads_unanswered = True
    lock.reported_down = reported
    for n, pin in added.items():
        rig.worker.want(n, pin, release=release)
    for _ in range(30):
        await rig.settle()
        await asyncio.sleep(0)

    assert rig.worker.offline
    assert all(rig.worker.slots[n].tries <= (0 if reported else 1) for n in added)
    assert all(rig.status(n)[1] is None for n in added)


@pytest.mark.parametrize(
    ("ignored", "strict"),
    [({7}, False), ({7, 8}, False), ({7, 8}, True)],
    ids=["one", "several", "several spoiling batch reads"],
)
async def test_slots_the_lock_ignores_are_held_without_polling(
    rig_factory, ignored, strict
) -> None:
    """
    However many slots the lock ignores, each is held, with nothing but the
    worker's own exchanges -- not every provider polls -- and the lock is
    never taken for gone: its probes read a slot that answers.
    """
    lock = FakeLock(ignored=ignored, strict=strict)
    rig = await _started(rig_factory(lock), {1: "1111"})
    for n in ignored:
        rig.worker.want(n, str(n) * 4)
    for _ in range(100):
        if all(rig.status(n)[0] is Status.SUSPENDED for n in ignored):
            break
        assert not rig.worker.offline
        await rig.settle()
        await asyncio.sleep(0)

    assert all(rig.status(n) == (Status.SUSPENDED, Repair.CANT_SET) for n in ignored)
    assert rig.status(1) == (Status.IN_SYNC, None)
    assert rig.worker.reachable


async def test_a_lock_that_ignores_its_only_slot_goes_offline(rig_factory) -> None:
    """With no other slot to ask, the lock is gone: the offline repair says so."""
    lock = FakeLock({1: "1111"})
    rig = await _started(rig_factory(lock), {1: "1111"})
    lock.ignored = {1}
    rig.worker.want(1, "2222")
    for _ in range(30):
        await rig.settle()
        await asyncio.sleep(0)

    assert rig.worker.offline
    assert rig.status(1) == (Status.OUT_OF_SYNC, None)


async def test_a_provider_bug_on_one_slot_holds_only_that_slot(rig_factory) -> None:
    """A bug that raises for one slot does not take the whole lock down."""
    lock = FakeLock({1: "1111"})
    rig = await _started(rig_factory(lock), {1: "1111"})
    original = lock.read

    async def _buggy(slots):
        if 7 in slots:
            raise KeyError(7)
        return await original(slots)

    lock.read = _buggy  # type: ignore[method-assign]
    rig.worker.want(7, "7777")
    await rig.settle()
    rig.worker.want(1, "2222")
    await rig.settle()

    assert rig.status(7) == (Status.SUSPENDED, Repair.CANT_SET)
    assert rig.status(1) == (Status.IN_SYNC, None)
    assert rig.worker.reachable


async def test_a_report_taken_in_during_a_read_is_kept(rig_factory) -> None:
    """A keypad change pushed while a read is out may be newer than the read."""
    lock = FakeLock({1: "1234"})
    rig = await _started(rig_factory(lock), {1: "1234"})

    def _keypad() -> None:
        # After the lock answered, before the read is back.
        lock.after_read.clear()
        lock.codes[1] = "9999"
        rig.worker.push(1, Holds("9999"))

    lock.after_read.append(_keypad)
    await rig.worker.read([1])
    await rig.settle()

    assert lock.writes == [(1, "1234")]
    assert lock.codes == {1: "1234"}


async def test_a_witness_the_lock_stops_answering_for_is_not_asked_again(
    rig_factory,
) -> None:
    """
    A slot that answered once, then is ignored, stops being a witness;
    another one is asked, and the ignored slot is still held.
    """
    lock = FakeLock({1: "1111", 2: "2222"})
    rig = await _started(rig_factory(lock), {1: "1111", 2: "2222"})
    lock.ignored = {1, 7}
    rig.worker.want(7, "7777")
    for _ in range(60):
        if rig.status(7)[0] is Status.SUSPENDED:
            break
        await rig.settle()
        await asyncio.sleep(0)

    assert rig.status(7) == (Status.SUSPENDED, Repair.CANT_SET)


async def test_a_sync_that_tips_the_lock_into_backoff_is_not_charged(
    rig_factory,
) -> None:
    """The lock is gone: the sync spends nothing more on it, and charges nothing."""
    lock = FakeLock({1: "0000", 2: "2222"})
    rig = await _started(rig_factory(lock), {1: "0000", 2: "2222"})
    lock.writes_unanswered = True
    lock.reads_unanswered = True
    for _ in range(BACKOFF_AFTER - 1):
        with pytest.raises(LockError):
            await rig.worker.read([2])
    reads = 0
    original = lock.read

    async def _count(slots):
        nonlocal reads
        reads += 1
        return await original(slots)

    lock.read = _count  # type: ignore[method-assign]
    rig.worker.want(1, "1234")
    await rig.settle()

    assert not rig.worker.reachable
    assert reads == 0
    assert rig.worker.slots[1].tries == 0


async def test_a_provider_bug_in_a_probe_does_not_keep_the_lock_down(
    rig_factory,
) -> None:
    """A probe that hits a provider bug got an answer: the lock is back."""
    lock = FakeLock({1: "1111"})
    rig = await _started(rig_factory(lock), {1: "1111"})
    original = lock.read

    async def _buggy(slots):
        if 7 in slots:
            raise KeyError(7)
        return await original(slots)

    lock.read = _buggy  # type: ignore[method-assign]
    rig.worker.want(7, "7777")
    await rig.settle()
    assert rig.status(7)[0] is Status.SUSPENDED

    lock.unreachable = True
    for _ in range(BACKOFF_AFTER):
        with pytest.raises(LockError):
            await rig.worker.read([1])
    assert not rig.worker.reachable
    lock.unreachable = False
    for _ in range(20):
        if rig.worker.reachable:
            break
        await rig.settle()
        await asyncio.sleep(0)

    assert rig.worker.reachable


async def test_a_resume_mid_sync_charges_nothing(rig_factory) -> None:
    """A sync begun before the user lifted the counts is not charged after."""
    lock = FakeLock({1: "0000"}, ignore_writes=True)
    rig = await _started(rig_factory(lock), {1: "0000"})

    def _resume() -> None:
        lock.during_write.clear()
        rig.worker.resume(1)

    lock.during_write.append(_resume)
    rig.worker.want(1, "1234")
    await rig.settle()

    assert rig.status(1) == (Status.SUSPENDED, Repair.CANT_SET)
    assert lock.writes.count((1, "1234")) == LIMIT + 1


async def test_a_read_that_leaves_a_slot_out_keeps_its_reports(rig_factory) -> None:
    """A push-only slot: a read that says nothing of it is not newer than a push."""
    lock = FakeLock(silent=True)
    rig = await _started(rig_factory(lock), {1: "1234"})
    assert rig.status(1) == (Status.IN_SYNC, None)

    read = asyncio.create_task(rig.worker.read([1]))
    await asyncio.sleep(0)
    # Taken in after the read was queued, before it was sent.
    rig.worker.push(1, Seen.EMPTY)
    await read
    await rig.settle()

    assert lock.writes == [(1, "1234")] * 2


async def test_a_probe_reads_one_slot_that_answered(rig_factory) -> None:
    """Probing a lock that is down costs one exchange, not one per slot."""
    slots = dict.fromkeys(range(1, 6), "1234")
    lock = FakeLock(dict.fromkeys(range(1, 6), "1234"))
    rig = await _started(rig_factory(lock), slots)
    lock.unreachable = True
    for _ in range(BACKOFF_AFTER):
        with pytest.raises(LockError):
            await rig.worker.read([1])
    probes: list[tuple[int, ...]] = []
    original = lock.read

    async def _record(numbers):
        probes.append(tuple(numbers))
        return await original(numbers)

    lock.read = _record  # type: ignore[method-assign]
    for _ in range(OFFLINE_AFTER * 2):
        if rig.worker.offline:
            break
        await rig.settle()
        await asyncio.sleep(0)

    assert rig.worker.offline
    assert len(probes) >= 4
    assert all(len(probe) == 1 for probe in probes)
    # The witnesses take turns; slot 1 stopped being one when it went unanswered.
    assert {n for (n,) in probes} == {2, 3, 4, 5}


async def test_a_witness_ignored_after_the_outage_does_not_keep_it_down(
    rig_factory,
) -> None:
    """The lock comes back ignoring the slot a probe would read: the next is read."""
    lock = FakeLock({1: "1111", 2: "2222"}, strict=True)
    rig = await _started(rig_factory(lock), {1: "1111", 2: "2222"})
    lock.unreachable = True
    for _ in range(BACKOFF_AFTER):
        with pytest.raises(LockError):
            await rig.worker.read([3])
    assert not rig.worker.reachable
    lock.unreachable = False
    lock.ignored = {1}
    for _ in range(20):
        if rig.worker.reachable:
            break
        await rig.settle()
        await asyncio.sleep(0)

    assert rig.worker.reachable


# The invariant: every sync that does not end in sync charges a try or
# answers nothing, and only an answer resets the lock's no-answers.


@pytest.mark.parametrize(
    "lock",
    [
        FakeLock({1: "0000"}, broken=True),
        FakeLock({1: "0000"}, ignore_writes=True),
        FakeLock({1: "0000"}, transient=99),
    ],
    ids=["provider bug", "writes not kept", "answered failures"],
)
async def test_an_answered_sync_is_charged_whatever_the_provider_says(
    rig_factory, lock
) -> None:
    """An answer outranks the provider saying the lock is gone: held, no loop."""
    rig = await _started(rig_factory(lock), {1: "0000"})
    lock.reported_down = True
    rig.worker.want(1, "1234")
    await rig.settle()

    assert rig.status(1) == (Status.SUSPENDED, Repair.CANT_SET)


async def test_writes_answered_and_reads_not_while_reported_down_hold(
    rig_factory,
) -> None:
    """Each sync heard the write answered: charged, though its read-back was not."""
    lock = FakeLock({1: "0000"}, ignore_writes=True)
    rig = await _started(rig_factory(lock), {1: "0000"})
    lock.reported_down = True
    original = lock.read

    async def _unanswered(slots):
        if lock.writes:
            raise LockError(ErrorKind.NO_ANSWER)
        return await original(slots)

    lock.read = _unanswered  # type: ignore[method-assign]
    rig.worker.want(1, "1234")
    for _ in range(50):
        if rig.status(1)[0] is Status.SUSPENDED or rig.worker.offline:
            break
        await rig.settle()
        await asyncio.sleep(0)

    assert rig.status(1) == (Status.SUSPENDED, Repair.CANT_SET)


async def test_a_provider_that_cannot_say_if_the_lock_is_there_says_nothing(
    rig_factory,
) -> None:
    """A bug in the reachability signal is no signal: an outage costs one try."""
    lock = FakeLock({1: "0000"})
    rig = await _started(rig_factory(lock), {1: "0000"})

    def _boom() -> bool:
        raise RuntimeError("the provider broke")

    lock.reachable = _boom  # type: ignore[method-assign]
    lock.writes_unanswered = lock.reads_unanswered = True
    rig.worker.want(1, "1234")
    for _ in range(20):
        await rig.settle()
        await asyncio.sleep(0)

    assert rig.worker.slots[1].tries == 1
    assert rig.status(1) == (Status.OUT_OF_SYNC, None)


async def test_a_reported_outage_mid_sync_charges_nothing(rig_factory) -> None:
    """The write was answered, then the lock went, and its provider says so."""
    lock = FakeLock({1: "0000"})
    rig = await _started(rig_factory(lock), {1: "0000"})

    def _gone() -> None:
        # Once the write is answered: the next exchange finds the lock gone.
        lock.during_write.clear()
        lock.reads_unanswered = True

    original = lock.read

    async def _read(slots):
        if lock.reads_unanswered:
            lock.reported_down = lock.writes_unanswered = True
        return await original(slots)

    lock.read = _read  # type: ignore[method-assign]
    lock.during_write.append(_gone)
    rig.worker.want(1, "1234")
    for _ in range(20):
        await rig.settle()
        await asyncio.sleep(0)

    assert rig.worker.slots[1].tries == 0


# Witnesses and probes -----------------------------------------------------------


async def _poll(rig: Rig) -> None:
    with contextlib.suppress(LockError):
        await rig.worker.read()


async def test_a_failed_poll_leaves_the_witnesses_for_an_ignored_slot(
    rig_factory,
) -> None:
    """A batch failing over one ignored slot says nothing of the others."""
    lock = FakeLock({1: "1111", 2: "2222"}, strict=True)
    rig = await _started(rig_factory(lock), {1: "1111", 2: "2222"})
    lock.ignored = {7}
    rig.worker.want(7, "7777")
    await _poll(rig)
    for _ in range(100):
        if rig.status(7)[0] is Status.SUSPENDED:
            break
        assert not rig.worker.offline
        await rig.settle()
        await asyncio.sleep(0)

    assert rig.status(7) == (Status.SUSPENDED, Repair.CANT_SET)
    assert not rig.worker.offline


def _bug_on(lock: FakeLock, number: int) -> None:
    original = lock.read

    async def _buggy(slots):
        if number in slots:
            raise KeyError(number)
        return await original(slots)

    lock.read = _buggy  # type: ignore[method-assign]


@pytest.mark.parametrize("silent", [False, True], ids=["reads", "push only"])
async def test_a_held_buggy_slot_does_not_make_an_outage_hold_others(
    rig_factory, silent
) -> None:
    """A provider bug answering for a dead lock never brings it back."""
    lock = FakeLock({1: "1111", 2: "2222"}, silent=silent)
    rig = await _started(rig_factory(lock), {1: "1111", 2: "2222"})
    _bug_on(lock, 7)
    rig.worker.want(7, "7777")
    await rig.settle()
    assert rig.status(7)[0] is Status.SUSPENDED
    await _poll(rig)

    lock.unreachable = True
    rig.worker.want(1, "1234")
    for _ in range(60):
        await rig.settle()
        await asyncio.sleep(0)

    assert rig.worker.offline
    assert rig.status(1) == (Status.OUT_OF_SYNC, None)


# Retargets and reports ----------------------------------------------------------


@pytest.mark.parametrize("answer_lost", [False, True], ids=["answered", "answer lost"])
@pytest.mark.parametrize(
    ("before", "codes"), [(None, {}), ("1111", {1: "1111"})], ids=["empty", "pin"]
)
async def test_a_target_changed_and_back_mid_write_is_put_right(
    rig_factory, before, codes, answer_lost
) -> None:
    """The write landed for a target no longer wanted: read, then undone."""
    lock = FakeLock(dict(codes))
    rig = await _started(rig_factory(lock), {1: before})
    if answer_lost:
        original = lock.write

        async def _lost(slot, pin):
            await original(slot, pin)
            if pin == "1234":
                raise LockError(ErrorKind.NO_ANSWER)

        lock.write = _lost  # type: ignore[method-assign]

    def _back() -> None:
        lock.during_write.clear()
        rig.worker.want(1, before)

    lock.during_write.append(_back)
    rig.worker.want(1, "1234")
    await rig.settle()

    assert lock.codes == codes
    assert rig.status(1) == (Status.IN_SYNC, None)


async def test_a_report_taken_in_while_a_read_waits_its_turn_is_dropped(
    rig_factory,
) -> None:
    """A read is newer than reports taken in before it is sent, gap included."""
    lock = FakeLock({1: "1234"})
    rig = await _started(rig_factory(lock), {1: "1234"})
    real_sleep = rig.worker.sleep

    async def _sleep(seconds: float) -> None:
        rig.worker.sleep = real_sleep
        rig.worker.push(1, Seen.EMPTY)
        await real_sleep(seconds)

    rig.worker.sleep = _sleep
    rig.clock.now = rig.worker._last_exchange or 0.0
    await rig.worker.read([1])
    await rig.settle()

    assert lock.writes == []
    assert rig.worker.slots[1].fights == []


async def test_a_provider_wrongly_saying_down_does_not_spare_an_ignored_slot(
    rig_factory,
) -> None:
    """The lock answering for others while its provider says it is gone wins."""
    lock = FakeLock({1: "1111"}, ignored={7})
    rig = await _started(rig_factory(lock), {1: "1111"})
    lock.reported_down = True
    rig.worker.want(7, "7777")
    for _ in range(100):
        if rig.status(7)[0] is Status.SUSPENDED:
            break
        await rig.settle()
        await asyncio.sleep(0)

    assert rig.status(7) == (Status.SUSPENDED, Repair.CANT_SET)
    assert not rig.worker.offline


async def test_a_reported_outage_after_a_doubt_is_believed_again(rig_factory) -> None:
    """Once the provider says the lock is up, its next "down" is believed."""
    lock = FakeLock({1: "1111"})
    rig = await _started(rig_factory(lock), {1: "1111"})
    lock.reported_down = True
    await rig.worker.read([1])  # answered while reported down: doubted
    lock.reported_down = False
    await rig.worker.read([1])
    lock.reported_down = True
    lock.writes_unanswered = lock.reads_unanswered = True
    rig.worker.want(1, "2222")
    for _ in range(20):
        await rig.settle()
        await asyncio.sleep(0)

    assert rig.worker.slots[1].tries == 0
