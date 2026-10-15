"""
The lock worker, scenario by scenario, against a fake lock and a fake clock.

Each scenario is a case from the bug catalog or the credential-model
decisions, driven only through the worker's own API.
"""

import asyncio
from collections.abc import Callable, Collection
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
    fingerprint,
)
from custom_components.lock_code_manager.core.worker import (
    BACKOFF_AFTER,
    BACKOFF_MAX,
    BACKOFF_MIN,
    OFFLINE_AFTER,
    RETRY_AFTER,
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

    async def read(self, slots: Collection[int]) -> dict[int, Reading]:
        """Answer a read the way this lock does."""
        readings = await self._read(slots)
        for act in self.after_read:
            act()
        return readings

    async def _read(self, slots: Collection[int]) -> dict[int, Reading]:
        if not slots:
            # Reading nothing asks the lock nothing.
            return {}
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
            # A slot waiting before its next try: the fake clock lets it go.
            while any(not wait.done() for wait in self.worker._waits.values()):
                await asyncio.sleep(0)
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


def _failing_write(message: str) -> Callable[[], None]:
    def _fail() -> None:
        raise LockError(ErrorKind.TRANSIENT, message=message)

    return _fail


async def test_a_slot_held_after_failed_writes_says_what_failed_last(
    rig_factory,
) -> None:
    """The provider's own words reach the repair, not a generic one."""
    lock = FakeLock(during_write=[_failing_write("user 7 holds this index")])
    rig = await _started(rig_factory(lock), {1: "1234"})
    for _ in range(5):
        await rig.pass_()

    assert rig.status(1) == (Status.SUSPENDED, Repair.CANT_SET)
    assert rig.worker.slots[1].why == "user 7 holds this index"


async def test_a_write_that_goes_through_forgets_why_the_last_failed(
    rig_factory,
) -> None:
    """A later hold for another cause is not given a stale one."""
    lock = FakeLock()
    whys: list[str] = []

    def _fail_once() -> None:
        lock.during_write.clear()
        _failing_write("busy")()

    lock.during_write.append(_fail_once)
    rig = rig_factory(lock)
    lock.after_read.append(lambda: whys.append(rig.worker.slots[1].why))
    await _started(rig, {1: "1234"})

    assert rig.status(1) == (Status.IN_SYNC, None)
    assert "busy" in whys
    assert rig.worker.slots[1].why == ""


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


async def test_an_outage_holds_no_slot(rig_factory) -> None:
    """
    A lock that has gone is the lock's problem: no slot is held.

    Syncs that fail with nothing answering are owed, not charged, while it
    is gone; when it answers again each is charged once at most, and the
    first fresh read that agrees clears that.
    """
    slots = range(1, 6)
    lock = FakeLock()
    rig = await _started(rig_factory(lock), dict.fromkeys(slots, "1234"))
    lock.writes_unanswered = True
    lock.reads_unanswered = True
    for n in slots:
        rig.worker.want(n, "4321")
    for _ in range(20):
        await rig.settle()
        await asyncio.sleep(0)

    assert not rig.worker.reachable
    assert all(rig.status(n) == (Status.OUT_OF_SYNC, None) for n in slots)
    assert all(rig.worker.slots[n].tries == 0 for n in slots)

    lock.writes_unanswered = lock.reads_unanswered = False
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


@pytest.mark.parametrize(
    ("target", "reads_first"),
    [
        # What the lock last said agrees with the new target, but the old
        # write may have landed: read first.
        ("0000", True),
        # It disagrees: the slot is written either way, so it stays out of
        # sync (pending) rather than unknown, and the write goes at once.
        ("5555", False),
    ],
)
async def test_a_write_retargeted_while_out_is_not_read_back(
    rig_factory, target, reads_first
) -> None:
    """The old target's write is not checked: the new one is synced on its own."""
    lock = FakeLock({1: "0000"})
    rig = await _started(rig_factory(lock), {1: "0000"})
    reads: list[tuple[int, ...]] = []
    original = lock.read

    async def _note(slots):
        reads.append(tuple(slots))
        return await original(slots)

    lock.read = _note  # type: ignore[method-assign]
    statuses: list[Status] = []

    def _retarget() -> None:
        lock.during_write.clear()
        rig.worker.want(1, target)
        statuses.append(rig.status(1)[0])

    lock.during_write.append(_retarget)
    rig.worker.want(1, "1234")
    await rig.settle()

    assert lock.writes[-2:] == [(1, "1234"), (1, target)]
    assert statuses == [Status.UNKNOWN if reads_first else Status.OUT_OF_SYNC]
    # Then the new target's own write is read back.
    assert reads == ([(1,), (1,)] if reads_first else [(1,)])
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


async def test_a_failing_send_callback_does_not_stop_the_write(rig_factory) -> None:
    """The integration failing to forget what was written is logged; the write goes."""

    def _boom() -> None:
        raise RuntimeError("store failed")

    lock = FakeLock()
    await _started(rig_factory(lock, on_send=_boom), {1: "1234"})

    assert lock.codes == {1: "1234"}


async def test_a_write_from_an_earlier_run_is_not_made_again(rig_factory) -> None:
    """#405: a slot starts from what was last written, so a masked lock is left alone."""
    lock = FakeLock({1: "1234"}, masked=True)
    recalled = {1: fingerprint("1234", SALT)}
    await _started(rig_factory(lock, recall=recalled.get), {1: "1234"})

    assert lock.writes == []


async def test_a_lock_that_answered_an_earlier_run_can_be_offline_at_once(
    rig_factory,
) -> None:
    """Dead from the start this time, it answered before: it is gone, not coming up."""
    rig = await _started(
        rig_factory(FakeLock(reads_unanswered=True), answered_before=True),
        {1: "1234"},
    )
    for _ in range(OFFLINE_AFTER):
        with pytest.raises(LockError):
            await rig.worker.read([1])
        await rig.settle()

    assert rig.worker.offline


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


@pytest.mark.parametrize(
    ("added", "release"),
    [({9: None}, True), ({5: "5555"}, False), ({5: "5555", 6: "6666"}, False)],
    ids=["release", "new slot", "two new slots"],
)
async def test_an_outage_holds_no_slot_never_read(rig_factory, added, release) -> None:
    """Slots added during an outage, never read, are not held either."""
    lock = FakeLock({1: "1111"})
    rig = await _started(rig_factory(lock), {1: "1111"})
    lock.writes_unanswered = True
    lock.reads_unanswered = True
    for n, pin in added.items():
        rig.worker.want(n, pin, release=release)
    for _ in range(30):
        await rig.settle()
        await asyncio.sleep(0)

    assert rig.worker.offline
    assert all(rig.worker.slots[n].tries == 0 for n in added)
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


async def test_a_slot_the_lock_stops_answering_for_holds_while_others_answer(
    rig_factory,
) -> None:
    """
    A slot that answered once, then is ignored, is held: the lock answers
    for the others.
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
    # The slots take turns.
    assert {n for (n,) in probes} == {1, 2, 3, 4, 5}


async def test_a_slot_ignored_after_the_outage_does_not_keep_it_down(
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
async def test_a_sync_that_fails_answered_is_charged(rig_factory, lock) -> None:
    """A lock that answers and does not take the write: held, never a loop."""
    rig = await _started(rig_factory(lock), {1: "0000"})
    rig.worker.want(1, "1234")
    await rig.settle()

    assert rig.status(1) == (Status.SUSPENDED, Repair.CANT_SET)


async def test_writes_answered_and_reads_not_hold(rig_factory) -> None:
    """Each sync heard the write answered: charged, though its read-back was not."""
    lock = FakeLock({1: "0000"}, ignore_writes=True)
    rig = await _started(rig_factory(lock), {1: "0000"})
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


async def test_an_outage_mid_sync_costs_one_try_and_recovery_clears_it(
    rig_factory,
) -> None:
    """The write was answered, then the lock went: one try at most, then cleared."""
    lock = FakeLock({1: "0000"})
    rig = await _started(rig_factory(lock), {1: "0000"})
    original = lock.write

    async def _answered_then_gone(slot, pin):
        lock.write = original  # type: ignore[method-assign]
        await original(slot, pin)
        lock.reads_unanswered = lock.writes_unanswered = True

    lock.write = _answered_then_gone  # type: ignore[method-assign]
    rig.worker.want(1, "1234")
    for _ in range(20):
        await rig.settle()
        await asyncio.sleep(0)
    assert rig.worker.offline
    assert rig.worker.slots[1].tries == 1

    lock.reads_unanswered = lock.writes_unanswered = False
    for _ in range(50):
        if rig.status(1)[0] is Status.IN_SYNC:
            break
        await rig.settle()
        await asyncio.sleep(0)
    assert rig.status(1) == (Status.IN_SYNC, None)
    assert rig.worker.slots[1].tries == 0


# Probes ------------------------------------------------------------------------


async def _poll(rig: Rig) -> None:
    with contextlib.suppress(LockError):
        await rig.worker.read()


async def test_a_failed_poll_does_not_strand_an_ignored_slot(
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


# Round 9: nothing shared between slots decides a charge.


async def _run(rig: Rig, until, rounds: int = 100) -> None:
    for _ in range(rounds):
        if until():
            return
        await rig.settle()
        await asyncio.sleep(0)


async def test_an_outage_mid_retarget_then_an_ignored_slot_does_not_strand_the_lock(
    rig_factory,
) -> None:
    """Probes take every slot in turn: one the lock now ignores cannot keep it down."""
    lock = FakeLock({1: "1111", 7: "7777"})
    rig = await _started(rig_factory(lock), {1: "1111", 7: "7777"})
    lock.unreachable = True
    rig.worker.want(1, "2222")
    await _run(rig, lambda: not rig.worker.reachable)
    assert not rig.worker.reachable
    lock.unreachable = False
    lock.ignored = {7}
    await _run(rig, lambda: rig.status(1)[0] is Status.IN_SYNC)

    assert rig.status(1) == (Status.IN_SYNC, None)
    assert rig.worker.reachable


async def test_an_answered_error_on_a_lone_read_does_not_strand_an_ignored_slot(
    rig_factory,
) -> None:
    """A row-5 lock after a one-off error: the ignored slot is still held."""
    lock = FakeLock({1: "1111"}, strict=True)
    rig = await _started(rig_factory(lock), {1: "1111"})
    lock.read_errors = 1
    await _poll(rig)
    lock.ignored = {7}
    rig.worker.want(7, "7777")
    await _run(rig, lambda: rig.status(7)[0] is Status.SUSPENDED)

    assert rig.status(7) == (Status.SUSPENDED, Repair.CANT_SET)


async def test_a_provider_bug_during_an_outage_holds_only_its_own_slot(
    rig_factory,
) -> None:
    """A bug's errors say nothing about the lock: the healthy slot is not charged."""
    lock = FakeLock({1: "1111", 7: "7777"})
    rig = await _started(rig_factory(lock), {1: "1111", 7: "7777"})
    read, write = lock.read, lock.write

    async def _read(slots):
        if 7 in slots:
            raise KeyError(7)
        return await read(slots)

    async def _write(slot, pin):
        if slot == 7:
            raise KeyError(7)
        return await write(slot, pin)

    lock.read, lock.write = _read, _write  # type: ignore[method-assign]
    lock.unreachable = True
    rig.worker.want(7, "7778")
    rig.worker.want(1, "2222")
    await _run(rig, lambda: False, 30)

    assert rig.worker.slots[1].tries == 0
    assert rig.status(1) == (Status.OUT_OF_SYNC, None)


async def test_a_poll_during_each_probe_does_not_spare_an_ignored_slot(
    rig_factory,
) -> None:
    """No-answers from other exchanges never excuse a slot: it is held."""
    lock = FakeLock({1: "1111"}, strict=True)
    rig = await _started(rig_factory(lock), {1: "1111", 7: None})
    lock.ignored = {7}
    original = lock.read
    probes = 0

    async def _read(slots):
        nonlocal probes
        if not rig.worker.reachable and tuple(slots) == (1,):
            probes += 1
            asyncio.ensure_future(_poll(rig))
            await asyncio.sleep(0)
        return await original(slots)

    lock.read = _read  # type: ignore[method-assign]
    rig.worker.want(7, "7777")
    await _run(rig, lambda: rig.status(7)[0] is Status.SUSPENDED or probes > 30, 300)

    assert rig.status(7) == (Status.SUSPENDED, Repair.CANT_SET)


async def test_a_provider_bug_on_a_probe_does_not_end_the_probing(rig_factory) -> None:
    """A probe that hits a bug is no answer: the next probe reads the next slot."""
    lock = FakeLock({1: "1111", 7: "7777"})
    rig = await _started(rig_factory(lock), {1: "1111", 7: "7777"})
    _bug_on(lock, 7)
    lock.unreachable = True
    for _ in range(BACKOFF_AFTER):
        with pytest.raises(LockError):
            await rig.worker.read([1])
    lock.unreachable = False
    await _run(rig, lambda: rig.worker.reachable)

    assert rig.worker.reachable


async def test_a_release_cut_off_by_an_outage_mid_sync_is_not_held(rig_factory) -> None:
    """Charged once for the outage, never owed on top: a release has two tries."""
    lock = FakeLock({1: "1111"})
    rig = await _started(rig_factory(lock), {1: "1111"})
    original = lock.write

    async def _answered_then_gone(slot, pin):
        lock.write = original  # type: ignore[method-assign]
        await original(slot, pin)
        lock.codes[slot] = "1111"  # the clear did not take
        lock.reads_unanswered = lock.writes_unanswered = True

    lock.write = _answered_then_gone  # type: ignore[method-assign]
    rig.worker.want(1, None, release=True)
    await _run(rig, lambda: rig.worker.offline, 30)
    lock.reads_unanswered = lock.writes_unanswered = False
    await _run(rig, lambda: 1 not in rig.worker.slots)

    assert 1 not in rig.worker.slots
    assert lock.codes == {}


async def test_a_try_owed_before_a_retarget_is_not_charged_to_it(rig_factory) -> None:
    """The new target gets its full limit after the outage."""
    lock = FakeLock({1: "0000"})
    rig = await _started(rig_factory(lock), {1: "0000"})
    lock.unreachable = True
    rig.worker.want(1, "1111")
    await _run(rig, lambda: not rig.worker.reachable)
    rig.worker.want(1, "2222")
    lock.unreachable = False
    lock.ignore_writes = True
    await _run(rig, lambda: rig.status(1)[0] is Status.SUSPENDED)

    assert rig.status(1) == (Status.SUSPENDED, Repair.CANT_SET)
    assert lock.writes.count((1, "2222")) == LIMIT


async def test_a_try_owed_by_a_slot_since_in_sync_is_not_charged(rig_factory) -> None:
    """
    A report that the slot agrees, while the lock is gone: nothing owed is
    charged when it answers -- the slot later failing for real gets every try.
    """
    lock = FakeLock({1: "0000", 2: "2222"})
    rig = await _started(rig_factory(lock), {1: "0000", 2: "2222"})
    lock.unreachable = True
    rig.worker.want(1, "1111")
    await _run(rig, lambda: not rig.worker.reachable)
    rig.worker.push(1, Holds("1111"))
    await rig.settle()
    lock.unreachable = False
    lock.ignore_writes = True
    await _run(rig, lambda: rig.worker.reachable)
    # The cached report was wrong: the lock still holds the old code.
    rig.worker.push(1, Holds("0000"))
    await _run(rig, lambda: rig.status(1)[0] is Status.SUSPENDED)

    assert rig.status(1) == (Status.SUSPENDED, Repair.CANT_SET)
    assert lock.writes.count((1, "1111")) == LIMIT


async def test_a_try_owed_and_paid_in_the_slot_s_own_sync_is_charged_once(
    rig_factory,
) -> None:
    """
    Owed, then paid by the only answer in the slot's next sync: that sync is
    not charged on top -- one try between two answers.
    """
    lock = FakeLock({1: "0000"}, ignore_writes=True)
    rig = await _started(rig_factory(lock), {1: "0000"})
    write, read = lock.write, lock.read
    lost_reads = 2

    async def _lost_write(slot, pin):
        lock.write = write  # type: ignore[method-assign]
        raise LockError(ErrorKind.NO_ANSWER)

    async def _lost_read(slots):
        nonlocal lost_reads
        lost_reads -= 1
        if not lost_reads:
            lock.read = read  # type: ignore[method-assign]
        raise LockError(ErrorKind.NO_ANSWER)

    lock.write, lock.read = _lost_write, _lost_read  # type: ignore[method-assign]
    rig.worker.want(1, "1234")
    await _run(rig, lambda: rig.status(1)[0] is Status.SUSPENDED)

    assert rig.status(1) == (Status.SUSPENDED, Repair.CANT_SET)
    # The owed try and the sync that paid it are one; then one per try.
    assert lock.writes.count((1, "1234")) == LIMIT


async def test_a_refusal_before_sending_says_nothing_about_the_lock(
    rig_factory,
) -> None:
    """The integration refusing is no answer from the lock: nothing owed is paid."""
    lock = FakeLock({1: "1111", 2: "2222"}, refuse_value={"9999"}, refuse_unsent=True)
    rig = await _started(rig_factory(lock), {1: "1111", 2: "2222"})
    lock.ignored = {1}
    rig.worker.want(1, "1234")
    rig.worker.want(2, "9999")
    await rig.settle()

    assert rig.status(2)[0] is Status.SUSPENDED
    assert rig.worker.slots[1].tries == 0


# Round 10 ------------------------------------------------------------------------


async def test_an_unsent_refusal_on_a_read_does_not_loop(rig_factory) -> None:
    """The integration refusing a read is the slot's: held, never synced for ever."""
    lock = FakeLock({1: "1111", 2: "2222"})
    rig = await _started(rig_factory(lock), {1: "1111", 2: "2222"})
    original = lock.read

    async def _refused(slots):
        if 3 in slots:
            raise LockError(ErrorKind.SLOT_REFUSED, sent=False)
        return await original(slots)

    lock.read = _refused  # type: ignore[method-assign]
    rig.worker.want(3, "3333")
    await rig.settle()

    assert rig.status(3)[0] is Status.SUSPENDED


async def test_an_unsent_refusal_on_a_probe_does_not_end_the_probing(
    rig_factory,
) -> None:
    """The integration refusing a probe's read is no answer: probing goes on."""
    lock = FakeLock({1: "1111", 2: "2222"})
    rig = await _started(rig_factory(lock), {1: "1111", 2: "2222"})
    lock.unreachable = True
    for _ in range(BACKOFF_AFTER):
        with pytest.raises(LockError):
            await rig.worker.read([1])
    assert not rig.worker.reachable
    original = lock.read

    async def _refused(slots):
        if lock.unreachable:
            raise OSError("down")
        if 2 in slots:
            raise LockError(ErrorKind.SLOT_REFUSED, sent=False)
        return await original(slots)

    lock.read = _refused  # type: ignore[method-assign]
    lock.unreachable = False
    rig.worker.want(1, "5555")
    await _run(rig, lambda: rig.status(1)[0] is Status.IN_SYNC, 50)

    # Guarantee: in sync, held, or offline. Observed: out of sync, no repair,
    # lock "not answering" (not offline) for ever.
    assert rig.status(1)[0] in (Status.IN_SYNC, Status.SUSPENDED) or rig.worker.offline


async def test_a_provider_raising_cancelled_does_not_kill_the_worker(
    rig_factory,
) -> None:
    """A provider's own cancelled future is no answer, not the worker stopping."""
    lock = FakeLock({1: "1111"})
    rig = await _started(rig_factory(lock), {1: "1111"})
    original = lock.write

    async def _cancelled(slot, pin):
        lock.write = original  # type: ignore[method-assign]
        fut = asyncio.get_running_loop().create_future()
        fut.cancel()
        await fut

    lock.write = _cancelled  # type: ignore[method-assign]
    rig.worker.want(1, "1234")
    await rig.settle()
    assert rig.status(1) == (Status.IN_SYNC, None)


@pytest.mark.parametrize("answer_lost", [False, True], ids=["answered", "answer lost"])
async def test_a_push_during_a_retargeted_write_does_not_hide_the_pin(
    rig_factory, answer_lost
) -> None:
    """A report taken in during a write that landed for an old target is older than it."""
    lock = FakeLock({})
    rig = await _started(rig_factory(lock), {1: None})
    assert rig.status(1) == (Status.IN_SYNC, None)

    def _meanwhile():
        if lock.writes == [(1, "1234")]:
            # The lock reports the slot as it was; then the user turns it off.
            rig.worker.push(1, Seen.EMPTY)
            rig.worker.want(1, None)

    lock.during_write.append(_meanwhile)
    if answer_lost:
        original = lock.write

        async def _lost(slot, pin):
            await original(slot, pin)
            if pin == "1234":
                raise LockError(ErrorKind.NO_ANSWER)

        lock.write = _lost  # type: ignore[method-assign]
    rig.worker.want(1, "1234")
    await rig.settle()

    # The old write landed: either it is cleared, or status must not say in sync.
    assert not (lock.codes == {1: "1234"} and rig.status(1)[0] is Status.IN_SYNC)


@pytest.mark.parametrize(
    ("pushed", "reads_first"),
    [
        # The old target's write landed and the lock says so: the slot is
        # written either way, so it shows out of sync (pending), not unknown.
        (Holds("5678"), False),
        # It agrees with the new target, but may be older than the old
        # write: read first.
        (Holds("9999"), True),
    ],
)
async def test_a_push_during_a_retargeted_write_is_kept_if_it_disagrees(
    rig_factory, pushed, reads_first
) -> None:
    """A report on an old target's write: only one agreeing with the new is dropped."""
    lock = FakeLock({1: "1234"})
    rig = await _started(rig_factory(lock), {1: "1234"})
    reads: list[tuple[int, ...]] = []
    original = lock.read

    async def _note(slots):
        reads.append(tuple(slots))
        return await original(slots)

    lock.read = _note  # type: ignore[method-assign]
    statuses: list[Status] = []
    rig.worker.on_change = lambda: statuses.append(rig.status(1)[0])

    def _meanwhile() -> None:
        lock.during_write.clear()
        rig.worker.push(1, pushed)
        rig.worker.want(1, "9999")

    lock.during_write.append(_meanwhile)
    rig.worker.want(1, "5678")
    await rig.settle()

    assert lock.writes[-2:] == [(1, "5678"), (1, "9999")]
    assert (Status.UNKNOWN in statuses) is reads_first
    assert reads == ([(1,), (1,)] if reads_first else [(1,)])
    assert rig.status(1) == (Status.IN_SYNC, None)


async def test_an_owed_try_paid_by_an_agreeing_read_does_not_hold(rig_factory) -> None:
    """A read that pays an owed try and shows the slot in sync charges nothing."""
    lock = FakeLock({1: "0000"})
    rig = await _started(rig_factory(lock), {1: "0000"})
    original = lock.write
    calls = 0

    async def _write(slot, pin):
        nonlocal calls
        calls += 1
        if calls <= 2:  # answered, not applied: two real tries
            if calls == 2:
                # A poll answers between this sync and the next.
                asyncio.ensure_future(rig.worker.read([1]))
            raise LockError(ErrorKind.TRANSIENT)
        lock.write = original  # type: ignore[method-assign]
        lock.codes[slot] = pin  # the write lands ...
        lock.unreachable = True  # ... and the answer is lost
        raise OSError("gone")

    lock.write = _write  # type: ignore[method-assign]
    rig.worker.want(1, "1234")
    await _run(rig, lambda: not rig.worker.reachable, 30)
    assert rig.status(1) == (Status.OUT_OF_SYNC, None)
    assert rig.worker.slots[1].tries == 2 and rig.worker._owed  # precondition
    lock.unreachable = False
    # The probe reads slot 1: the lock answers that it holds 1234.
    await _run(rig, lambda: rig.worker.reachable, 30)
    await rig.settle()

    assert lock.codes == {1: "1234"}
    assert rig.status(1) == (Status.IN_SYNC, None)
    assert rig.worker.slots[1].tries == 0


async def test_an_owed_try_is_not_charged_to_a_new_slot(rig_factory) -> None:
    """A slot forgotten and claimed again does not inherit the old one's owed try."""
    lock = FakeLock({1: "0000", 2: "2222"})
    rig = await _started(rig_factory(lock), {2: "2222"})
    lock.unreachable = True
    rig.worker.want(1, "1111")  # a new Slot: epoch 0
    await _run(rig, lambda: not rig.worker.reachable)
    rig.worker.forget(1)
    rig.worker.want(1, "5555")  # another new Slot: epoch 0 again
    lock.unreachable = False
    lock.ignore_writes = True  # the new target fails for real
    await _run(rig, lambda: rig.status(1)[0] is Status.SUSPENDED)

    assert rig.status(1)[0] is Status.SUSPENDED
    assert lock.writes.count((1, "5555")) == 3, lock.writes


async def test_held_ignored_slots_do_not_take_a_polled_lock_offline(
    rig_factory,
) -> None:
    """Probes read slots not held: held slots the lock ignores never take it offline."""
    good = {1: "1111"}
    bad = {n: str(n) * 4 for n in range(2, 13)}  # 11 ignored slots
    lock = FakeLock({**good}, strict=True)
    rig = await _started(rig_factory(lock), good)
    lock.ignored = set(bad)
    for n, pin in bad.items():
        rig.worker.want(n, pin)
    await _run(
        rig,
        lambda: all(rig.status(n)[0] is Status.SUSPENDED for n in bad),
        500,
    )
    assert all(rig.status(n)[0] is Status.SUSPENDED for n in bad)
    went_offline = was = False
    flips = 0
    for _ in range(200):
        try:
            await rig.worker.read()
        except LockError:
            pass
        await rig.settle()
        await asyncio.sleep(0)
        flips += rig.worker.offline and not was
        was = rig.worker.offline
        went_offline |= rig.worker.offline
    assert not went_offline, f"offline {flips} separate times in 200 polls"


async def test_stop_does_not_hang_on_a_provider_that_swallows_cancel(
    rig_factory,
) -> None:
    """A provider that eats the cancel is left behind: stop returns."""
    lock = FakeLock({1: "1111"})
    rig = await _started(rig_factory(lock), {1: "1111"})
    entered, let_go = asyncio.Event(), asyncio.Event()

    async def _stubborn(slot, pin):
        entered.set()
        while not let_go.is_set():
            try:
                await let_go.wait()
            except asyncio.CancelledError:
                pass  # a provider retry loop that eats cancellation

    lock.write = _stubborn  # type: ignore[method-assign]
    rig.worker.want(1, "1234")
    await entered.wait()
    stop = asyncio.ensure_future(rig.worker.stop(grace=0.1))
    done, _ = await asyncio.wait({stop}, timeout=2)  # real seconds
    let_go.set()
    await stop
    assert done, "stop() still waiting 2 s after a 0.1 s grace"


@pytest.mark.parametrize("transient", [0, 1], ids=["written", "answered error"])
async def test_any_answer_pays_what_is_owed(rig_factory, transient) -> None:
    """A write the lock answers -- taken or not -- proves it is there."""
    lock = FakeLock({1: "1111", 2: "2222"})
    rig = await _started(rig_factory(lock), {1: "1111", 2: "2222"})
    lock.ignored = {1}
    seen: list[int] = []

    def _note() -> None:
        # Slot 2's read-back: after its write's answer, before the read's.
        lock.after_read.clear()
        seen.append(rig.worker.slots[1].tries)

    lock.transient = transient
    lock.after_read.append(_note)
    # Slot 1's sync goes unanswered and is owed; slot 2's write is answered.
    rig.worker.want(1, "1234")
    rig.worker.want(2, "5678")
    await rig.settle()

    assert seen == [1]


async def test_a_probe_with_every_slot_held_still_asks_the_lock(rig_factory) -> None:
    """Reading nothing asks the lock nothing: a dead lock is not taken for back."""
    lock = FakeLock({1: "1111"}, refuse_value={"9999"})
    rig = await _started(rig_factory(lock), {1: "1111"})
    rig.worker.want(1, "9999")
    await rig.settle()
    assert rig.status(1)[1] is Repair.SLOT_DISABLED
    lock.unreachable = True
    for _ in range(BACKOFF_AFTER):
        with pytest.raises(LockError):
            await rig.worker.read([1])
    await _run(rig, lambda: rig.worker.reachable, 30)

    assert not rig.worker.reachable


# Round 11 ------------------------------------------------------------------------


async def test_a_clear_after_an_unverified_write_is_not_taken_as_done(
    rig_factory,
) -> None:
    """
    Writes land, read-backs are answered with errors: the slot is held. The
    user turns the slot off; the read from before the write says "empty".
    """
    lock = FakeLock({})
    rig = await _started(rig_factory(lock), {1: None})
    assert rig.status(1) == (Status.IN_SYNC, None)
    lock.read_errors = 1000  # reads answered with an error, writes land
    rig.worker.want(1, "1234")
    await _run(rig, lambda: rig.status(1)[0] is Status.SUSPENDED)
    assert lock.codes == {1: "1234"}
    rig.worker.want(1, None)
    await rig.settle()

    # Guarantee: no PIN left on the lock while status says in sync.
    assert not (lock.codes.get(1) == "1234" and rig.status(1)[0] is Status.IN_SYNC)


async def test_a_clear_after_a_write_lost_in_an_outage_is_not_taken_as_done(
    rig_factory,
) -> None:
    """The write lands but its answer is lost; the user clears during the outage."""
    lock = FakeLock({2: "2222"})
    rig = await _started(rig_factory(lock), {1: None, 2: "2222"})
    original = lock.write

    async def _lands_then_gone(slot, pin):
        lock.write = original  # type: ignore[method-assign]
        lock.codes[slot] = pin
        lock.unreachable = True
        raise OSError("gone")

    lock.write = _lands_then_gone  # type: ignore[method-assign]
    rig.worker.want(1, "1234")
    await _run(rig, lambda: not rig.worker.reachable)
    assert not rig.worker.reachable
    rig.worker.want(1, None)
    lock.unreachable = False
    await _run(rig, lambda: rig.worker.reachable)
    await rig.settle()

    assert not (lock.codes.get(1) == "1234" and rig.status(1)[0] is Status.IN_SYNC)


async def test_a_lock_started_with_no_slots_is_not_taken_as_reached(
    rig_factory,
) -> None:
    """The first read of no slots is no answer: a dead lock stays not yet reached."""
    lock = FakeLock(unreachable=True)
    rig = await _started(rig_factory(lock), {})
    rig.worker.want(1, "1234")
    await _run(rig, lambda: rig.worker.offline, 100)

    assert not rig.worker.offline  # #1257: never reached is not offline


async def test_empty_polls_during_an_outage_do_not_hold_a_slot(rig_factory) -> None:
    """A poll of no slots between syncs pays owed tries while the lock is dead."""
    lock = FakeLock({1: "0000"})
    rig = await _started(rig_factory(lock), {1: "0000"})
    original = lock.read

    async def _read(slots):
        if lock.unreachable and slots:
            # The integration polls an empty set while this read is out.
            polls.append(asyncio.ensure_future(rig.worker.read([])))
            await asyncio.sleep(0)
        return await original(slots)

    polls: list = []
    lock.read = _read  # type: ignore[method-assign]
    lock.unreachable = True
    rig.worker.want(1, "1234")
    await _run(rig, lambda: rig.status(1)[0] is Status.SUSPENDED, 50)

    # An outage never holds a normal slot.
    assert rig.status(1)[0] is not Status.SUSPENDED


async def test_a_charge_to_the_old_target_does_not_excuse_the_new_one(
    rig_factory,
) -> None:
    """
    Target A is charged (answered, not kept); the user retargets to B and the
    lock goes away before it answers again. B's failed sync is owed, and paid
    when the lock is back.
    """
    lock = FakeLock({1: "0000"}, ignore_writes=True)
    armed = False

    def _on_change() -> None:
        nonlocal armed
        if armed and rig.worker.slots[1].tries == 1:
            armed = False
            rig.worker.want(1, "BBBB")
            lock.unreachable = True

    rig = rig_factory(lock, on_change=_on_change)
    await _started(rig, {1: "0000"})
    armed = True
    rig.worker.want(1, "AAAA")
    await _run(rig, lambda: not rig.worker.reachable)
    assert not rig.worker.reachable and rig.worker.slots[1].want == "BBBB"
    lock.unreachable = False
    await _run(rig, lambda: rig.status(1)[0] is Status.SUSPENDED)

    assert rig.status(1) == (Status.SUSPENDED, Repair.CANT_SET)
    # One try owed for B's sync during the outage, then one per write.
    assert lock.writes.count((1, "BBBB")) == LIMIT - 1, lock.writes


async def test_an_ignored_slot_beside_a_held_one_is_held_not_the_lock_offline(
    rig_factory,
) -> None:
    """Slot 1 is held (the lock refused its PIN); the lock ignores slot 2."""
    lock = FakeLock({1: "1111"}, refuse_value={"9999"})
    rig = await _started(rig_factory(lock), {1: "1111"})
    rig.worker.want(1, "9999")
    await rig.settle()
    assert rig.status(1) == (Status.SUSPENDED, Repair.SLOT_DISABLED)
    lock.ignored = {2}
    rig.worker.want(2, "2222")
    await _run(
        rig,
        lambda: rig.worker.offline or rig.status(2)[0] is Status.SUSPENDED,
        200,
    )

    assert rig.status(2)[0] is Status.SUSPENDED
    assert not rig.worker.offline


async def test_a_slot_wanted_after_the_probe_found_none_starts_the_probing(
    rig_factory,
) -> None:
    """The only slot is forgotten while backing off; the next one wanted is probed."""
    lock = FakeLock({1: "0000"})
    rig = await _started(rig_factory(lock), {1: "0000"})
    lock.unreachable = True
    rig.worker.want(1, "1111")
    await _run(rig, lambda: not rig.worker.reachable)
    rig.worker.forget(1)
    # A probe already queued goes unanswered; the next finds nothing to read.
    await rig.settle()
    await _run(rig, rig.worker._retry.done)
    await rig.settle()
    assert not rig.worker.reachable
    lock.unreachable = False
    rig.worker.want(2, "2222")
    await _run(rig, lambda: rig.worker.reachable)
    await rig.settle()

    assert rig.worker.reachable
    assert rig.status(2) == (Status.IN_SYNC, None)


# Round 12


async def test_a_read_returning_garbage_is_a_provider_bug_charged_to_the_slot(
    rig_factory,
) -> None:
    """
    R12-1: a provider whose read returns something that is not a mapping
    (a forgotten ``return``) for one slot. The TypeError is raised after the
    exchange, outside its handling: the sync is neither charged nor queued
    again, and the slot sits out of sync for ever with the lock answering.
    """
    lock = FakeLock({1: "1111"})
    rig = await _started(rig_factory(lock), {1: "1111"})
    original = lock.read

    async def _read(slots):
        if 7 in slots:
            return None  # the provider forgot to return its readings
        return await original(slots)

    lock.read = _read  # type: ignore[method-assign]
    rig.worker.want(7, "7777")
    await _run(rig, lambda: rig.status(7)[0] is Status.SUSPENDED, 50)

    # In sync, held, or the lock offline: nothing else.
    assert rig.status(7)[0] is Status.SUSPENDED or rig.worker.offline, rig.status(7)


async def test_a_slot_forgotten_while_its_write_waits_the_gap_is_not_written(
    rig_factory,
) -> None:
    """
    R12-2: the integration forgets slot 1 while its write waits out the gap
    between exchanges. The PIN chosen before the wait is sent anyway: a code
    nothing tracks is put on the lock.
    """
    lock = FakeLock({2: "2222"})
    rig = await _started(rig_factory(lock), {1: None, 2: "2222"})
    real_sleep = rig.worker.sleep

    async def _sleep(seconds: float) -> None:
        if rig.worker.slots[1].want == "1234":  # the write waits its turn
            rig.worker.sleep = real_sleep
            rig.worker.forget(1)
        await real_sleep(seconds)

    rig.worker.sleep = _sleep
    rig.worker.want(1, "1234")
    await rig.settle()

    assert 1 not in lock.codes


async def test_a_cached_report_after_a_lost_write_does_not_hide_the_pin(
    rig_factory,
) -> None:
    """
    R12-3: the write of 1234 lands but its answer is lost in an outage; the
    user turns the slot off (want() forgets the old reading), then a cached
    view of the slot ("empty", from before the write) comes in as a push.
    The push is taken as the truth: in sync, never read again -- while the
    lock still holds the revoked PIN. No polling.
    """
    lock = FakeLock({2: "2222"})
    rig = await _started(rig_factory(lock), {1: None, 2: "2222"})
    original = lock.write

    async def _lands_then_gone(slot, pin):
        lock.write = original  # type: ignore[method-assign]
        lock.codes[slot] = pin
        lock.unreachable = True
        raise OSError("gone")

    lock.write = _lands_then_gone  # type: ignore[method-assign]
    rig.worker.want(1, "1234")
    await _run(rig, lambda: not rig.worker.reachable)
    assert not rig.worker.reachable
    rig.worker.want(1, None)
    rig.worker.push(1, Seen.EMPTY)  # the provider's cache, from before the write
    await rig.settle()

    # While the lock is away: the worker knows the write may have landed.
    assert not (lock.codes.get(1) == "1234" and rig.status(1)[0] is Status.IN_SYNC)


async def test_a_cached_report_after_a_lost_write_is_not_kept_once_back(
    rig_factory,
) -> None:
    """R12-3b: as above; the lock comes back through a probe of another slot."""
    lock = FakeLock({2: "2222"})
    rig = await _started(rig_factory(lock), {1: None, 2: "2222"})
    original = lock.write

    async def _lands_then_gone(slot, pin):
        lock.write = original  # type: ignore[method-assign]
        lock.codes[slot] = pin
        lock.unreachable = True
        raise OSError("gone")

    lock.write = _lands_then_gone  # type: ignore[method-assign]
    rig.worker.want(1, "1234")
    await _run(rig, lambda: not rig.worker.reachable)
    rig.worker.want(1, None)
    rig.worker.push(1, Seen.EMPTY)
    await rig.settle()
    lock.unreachable = False
    lock.ignored = {1}  # only probes of slot 2 bring the lock back
    await _run(rig, lambda: rig.worker.reachable)
    lock.ignored = set()
    await rig.settle()

    assert not (lock.codes.get(1) == "1234" and rig.status(1)[0] is Status.IN_SYNC)


async def test_a_slot_is_syncing_while_its_sync_is_queued_or_running(
    rig_factory,
) -> None:
    """The card's spinner: queued, then running, then done."""
    lock = FakeLock({})
    rig = await _started(rig_factory(lock), {1: None})
    seen: list[bool] = []
    lock.during_write.append(lambda: seen.append(rig.worker.syncing(1)))
    rig.worker.want(1, "1234")

    assert rig.worker.syncing(1)
    await rig.settle()
    assert seen == [True]
    assert not rig.worker.syncing(1)


async def test_a_release_reaches_the_lock_even_when_it_reads_empty(
    rig_factory,
) -> None:
    """A lock with users tears down the one the slot anchored, code or not."""
    lock = FakeLock({})
    rig = await _started(rig_factory(lock), {1: None})
    assert rig.status(1) == (Status.IN_SYNC, None)
    rig.worker.want(1, None, release=True)
    await rig.settle()

    assert lock.writes == [(1, None)]
    assert 1 not in rig.worker.slots


async def test_waking_a_lock_backing_off_probes_it_now(rig_factory) -> None:
    """A reconnect ends the wait: the lock is probed at once, and syncs if back."""
    lock = FakeLock({1: "0000"})
    rig = await _started(rig_factory(lock), {1: "0000"})
    paced = rig.clock.sleep

    async def _no_backoff_ends(seconds: float) -> None:
        if seconds >= BACKOFF_MIN:
            await asyncio.Event().wait()  # a backoff that never ends on its own
        await paced(seconds)

    rig.worker.sleep = _no_backoff_ends
    lock.unreachable = True
    rig.worker.want(1, "1234")
    await _run(rig, lambda: not rig.worker.reachable)
    assert not rig.worker.reachable
    lock.unreachable = False
    await _run(rig, lambda: rig.worker.reachable, 20)
    assert not rig.worker.reachable  # nothing ends the backoff on its own

    rig.worker.wake()
    await _run(rig, lambda: rig.status(1)[0] is Status.IN_SYNC)

    assert lock.codes[1] == "1234"


async def test_waking_a_lock_that_answers_does_nothing(rig_factory) -> None:
    """Nothing to end: no probe is started."""
    rig = await _started(rig_factory(FakeLock({1: "0000"})), {1: "0000"})
    rig.worker.wake()

    assert rig.worker._retry is None


async def test_a_clear_on_a_lock_that_hides_its_codes_is_trusted(rig_factory) -> None:
    """#1437: a lock still calling a cleared slot occupied is cleared once."""
    lock = FakeLock({1: "1234"}, masked=True)
    rig = await _started(rig_factory(lock), {1: "1234"})
    # Takes the clear, and goes on calling the slot occupied (a code it hides).
    lock.ignore_writes = True
    rig.worker.want(1, None)
    await rig.settle()

    assert lock.writes.count((1, None)) == 1
    assert rig.status(1) == (Status.IN_SYNC, None)


async def test_a_clear_that_changed_nothing_is_not_trusted_on_a_hiding_lock(
    rig_factory,
) -> None:
    """Answered, but nothing changed: no proof of what the slot holds."""
    lock = FakeLock({1: "1234"}, masked=True)
    rig = await _started(rig_factory(lock), {1: "1234"})
    original = lock.write

    async def _found_nothing(slot, pin):
        await original(slot, pin)
        lock.codes[slot] = "????"  # still occupied, someone else's
        return False

    lock.write = _found_nothing  # type: ignore[method-assign]
    rig.worker.want(1, None)
    await _run(rig, lambda: rig.status(1)[0] is Status.SUSPENDED)

    assert rig.status(1) == (Status.SUSPENDED, Repair.CODE_MAY_REMAIN)


async def test_a_release_that_found_nothing_to_clear_is_done(rig_factory) -> None:
    """The release reached the lock; the slot reads empty: nothing left to do."""
    lock = FakeLock({})
    rig = await _started(rig_factory(lock), {1: None})
    original = lock.write

    async def _found_nothing(slot, pin):
        await original(slot, pin)
        return False

    lock.write = _found_nothing  # type: ignore[method-assign]
    rig.worker.want(1, None, release=True)
    await rig.settle()

    assert lock.writes == [(1, None)]
    assert 1 not in rig.worker.slots


@pytest.mark.paced
async def test_a_slot_waits_longer_before_each_try_again(rig_factory) -> None:
    """#1257: a lock refusing writes for a while does not burn every try at once."""
    lock = FakeLock({1: "0000"}, ignore_writes=True)
    rig = await _started(rig_factory(lock), {1: "0000"})
    times: list[float] = []
    lock.during_write.append(lambda: times.append(rig.clock.now))
    rig.worker.want(1, "1234")
    await rig.settle()

    assert rig.status(1) == (Status.SUSPENDED, Repair.CANT_SET)
    assert len(times) == LIMIT
    assert times[1] - times[0] >= RETRY_AFTER[0]
    assert times[2] - times[1] >= RETRY_AFTER[1]


@pytest.mark.paced
async def test_a_new_target_does_not_wait_out_the_old_ones_pause(rig_factory) -> None:
    """A PIN changed after a failed try is written at once."""
    lock = FakeLock({1: "0000"}, ignore_writes=True)
    rig = await _started(rig_factory(lock), {1: "0000"})
    rig.clock.sleep = _forever_if_long(rig.clock.sleep)  # type: ignore[method-assign]
    rig.worker.sleep = rig.clock.sleep
    rig.worker.want(1, "1234")
    await rig.worker.settled()
    assert rig.worker._waits  # waiting out its pause
    lock.ignore_writes = False
    rig.worker.want(1, "5678")
    await rig.worker.settled()

    assert lock.codes[1] == "5678"
    assert not rig.worker._waits


def _forever_if_long(sleep):
    async def _sleep(seconds: float) -> None:
        if seconds >= RETRY_AFTER[0]:
            await asyncio.Event().wait()
        await sleep(seconds)

    return _sleep


async def test_a_turn_runs_in_the_lock_s_queue_and_counts_as_an_answer(
    rig_factory,
) -> None:
    """A question asked through another provider waits its turn like any job."""
    lock = FakeLock({1: "0000"})
    rig = await _started(rig_factory(lock), {1: "0000"})
    order: list[str] = []
    lock.during_write.append(lambda: order.append("write"))

    async def _ask() -> str:
        order.append("turn")
        return "answer"

    rig.worker.want(1, "1234")
    answer = await rig.worker.turn(_ask)

    assert answer == "answer"
    assert order == ["write", "turn"]  # after the sync queued before it


async def test_a_turn_while_backing_off_is_refused_unsent(rig_factory) -> None:
    """Nothing reaches a lock that is not answering, this included."""
    lock = FakeLock({1: "0000"})
    rig = await _started(rig_factory(lock), {1: "0000"})
    lock.unreachable = True
    rig.worker.want(1, "1234")
    await _run(rig, lambda: not rig.worker.reachable)

    async def _ask() -> None:
        raise AssertionError("reached the lock")

    with pytest.raises(LockError) as caught:
        await rig.worker.turn(_ask)
    assert not caught.value.sent


@pytest.mark.paced
async def test_stopping_cancels_a_slot_waiting_to_try_again(rig_factory) -> None:
    """Nothing is left running: a pause before the next try ends with the worker."""
    lock = FakeLock({1: "0000"}, ignore_writes=True)
    rig = await _started(rig_factory(lock), {1: "0000"})
    rig.worker.sleep = _forever_if_long(rig.clock.sleep)
    rig.worker.want(1, "1234")
    await rig.worker.settled()
    [waiting] = rig.worker._waits.values()

    await rig.worker.stop(grace=0.1)

    assert waiting.cancelled() or waiting.done()
    assert not rig.worker._waits


@pytest.mark.paced
async def test_a_poll_during_a_pause_does_not_bring_the_next_try_forward(
    rig_factory,
) -> None:
    """The slot waits out its pause, whatever reads the lock meanwhile."""
    lock = FakeLock({1: "0000"}, ignore_writes=True)
    rig = await _started(rig_factory(lock), {1: "0000"})
    rig.worker.sleep = _forever_if_long(rig.clock.sleep)
    rig.worker.want(1, "1234")
    await rig.worker.settled()
    writes = len(lock.writes)

    await rig.worker.read()
    await rig.worker.settled()

    assert len(lock.writes) == writes
    assert len(rig.worker._waits) == 1


@pytest.mark.paced
async def test_a_resumed_slot_starts_its_pauses_over(rig_factory) -> None:
    """A pause left over from before the resume does not cut the next one short."""
    lock = FakeLock({1: "0000"}, ignore_writes=True)
    rig = await _started(rig_factory(lock), {1: "0000"})
    rig.worker.sleep = _forever_if_long(rig.clock.sleep)
    rig.worker.want(1, "1234")
    await rig.worker.settled()
    [before] = rig.worker._waits.values()

    rig.worker.resume(1)
    await rig.worker.settled()

    [after] = rig.worker._waits.values()
    assert after is not before
    await asyncio.sleep(0)
    assert before.cancelled()


async def test_a_turn_waits_out_the_gap_like_any_exchange(rig_factory) -> None:
    """Paced like the rest: the lock gets its pause before the question."""
    lock = FakeLock({1: "0000"})
    rig = await _started(rig_factory(lock), {1: "0000"})
    last = rig.worker._last_exchange
    asked_at: list[float] = []

    async def _ask() -> None:
        asked_at.append(rig.clock.now)

    await rig.worker.turn(_ask)

    assert last is not None
    assert asked_at[0] - last >= rig.worker.gap


async def test_a_turn_after_stopping_is_refused_not_left_waiting(rig_factory) -> None:
    """A stopped worker answers at once: nobody waits for ever."""
    rig = await _started(rig_factory(FakeLock()), {})
    await rig.worker.stop(grace=0.1)

    async def _ask() -> None:
        raise AssertionError("reached the lock")

    with pytest.raises(LockError):
        async with asyncio.timeout(1):
            await rig.worker.turn(_ask)


async def test_a_probe_reads_the_slots_not_held_first(rig_factory) -> None:
    """
    A held slot is probed only once every slot not held went unanswered --
    counted afresh in each outage.
    """
    lock = FakeLock({1: "1111", 2: "2222", 3: "3333"}, refuse_value={"9999"})
    rig = await _started(rig_factory(lock), {1: "1111", 2: "2222", 3: "3333"})
    rig.worker.want(3, "9999")
    await rig.settle()
    assert rig.status(3)[1] is Repair.SLOT_DISABLED
    probes: list[int] = []
    original = lock.read

    async def _record(numbers):
        probes.extend(numbers)
        return await original(numbers)

    for _ in range(2):  # two outages
        lock.unreachable = True
        for _ in range(BACKOFF_AFTER):
            with pytest.raises(LockError):
                await rig.worker.read([1])
        probes.clear()
        lock.read = _record  # type: ignore[method-assign]
        await _run(rig, lambda: len(probes) >= 2, 30)
        lock.read = original  # type: ignore[method-assign]

        assert set(probes[:2]) == {1, 2}
        lock.unreachable = False
        await _run(rig, lambda: rig.worker.reachable, 30)
        await rig.settle()


async def test_a_held_slot_resumed_is_read_before_it_is_written(rig_factory) -> None:
    """The code was taken off at the keypad while held: nothing is sent again."""
    rig = await _started(
        rig_factory(FakeLock({1: "1234"}, refuse_clear={1})), {1: None}
    )
    await rig.pass_()
    assert rig.status(1) == (Status.SUSPENDED, Repair.CODE_MAY_REMAIN)
    del rig.lock.codes[1]  # at the keypad; polls leave the held slot out

    rig.worker.resume(1)
    await rig.settle()

    assert rig.status(1) == (Status.IN_SYNC, None)
    assert rig.lock.writes == [(1, None)]


async def test_a_release_after_unanswered_pin_writes_reaches_the_lock(
    rig_factory,  # noqa: F811
) -> None:
    """
    ``released`` from an earlier release must not excuse a later one.

    A release is answered but the code stays; the slot is claimed again and
    its PIN writes go unanswered (they may have landed: a native-user lock
    may have made the user); the slot is given up again and reads empty.
    The new release must still be sent.
    """
    lock = FakeLock({5: "1111"})
    rig = rig_factory(lock)
    rig.worker.want(5, "1111")
    rig.worker.start()
    await rig.settle()
    assert rig.status(5) == (Status.IN_SYNC, None)

    lock.ignore_writes = True
    rig.worker.want(5, None, release=True)
    await rig.settle()
    assert rig.status(5) == (Status.SUSPENDED, Repair.CODE_MAY_REMAIN)
    assert rig.worker.slots[5].released

    # Claimed again; the code is gone from the keypad, and the PIN writes go
    # unanswered.
    lock.ignore_writes = False
    lock.writes_unanswered = True
    del lock.codes[5]
    rig.worker.want(5, "2222")
    await rig.settle()
    assert (5, "2222") in lock.writes

    lock.writes_unanswered = False
    writes = len(lock.writes)
    rig.worker.want(5, None, release=True)
    await rig.settle()
    assert (5, None) in lock.writes[writes:]


async def test_a_write_is_read_back_by_asking_only_about_its_slot(
    rig_factory,  # noqa: F811
) -> None:
    """
    The read-back after a write asks about the slot written, not the whole lock.

    On a lock that drops half its answers, reading one slot back lands about
    half the time and a walk of every slot almost never (#1549).
    """
    lock = FakeLock({1: "1111", 2: "2222", 3: "3333"})
    rig = await _started(rig_factory(lock), {1: "1111", 2: "2222", 3: "3333"})
    reads: list[tuple[int, ...]] = []
    original = lock.read

    async def _note(slots):
        reads.append(tuple(slots))
        return await original(slots)

    lock.read = _note  # type: ignore[method-assign]
    rig.worker.want(2, "9999")
    await rig.settle()

    assert lock.writes == [(2, "9999")]
    assert reads == [(2,)]
    assert rig.status(2) == (Status.IN_SYNC, None)


# The lock as a whole -------------------------------------------------------------


async def test_the_backoff_doubles_from_a_minute_to_half_an_hour_and_starts_over(
    rig_factory,  # noqa: F811
) -> None:
    """
    A lock that stops answering waits a minute, then twice that each time, never
    more than half an hour; once it answers, the next outage starts at a minute.
    """
    lock = FakeLock({1: "1234"})
    rig = await _started(rig_factory(lock), {1: "1234"})
    waits: list[float] = []
    paced = rig.clock.sleep

    async def _recorded(seconds: float) -> None:
        if seconds >= BACKOFF_MIN:
            waits.append(seconds)
        await paced(seconds)

    rig.worker.sleep = _recorded
    lock.reads_unanswered = True
    for _ in range(BACKOFF_AFTER - 1):
        with pytest.raises(LockError):
            await rig.worker.read([1])
    assert rig.worker.reachable  # below the threshold: no backoff yet
    with pytest.raises(LockError):
        await rig.worker.read([1])
    assert not rig.worker.reachable

    await _run(rig, lambda: len(waits) >= 8, 400)
    assert waits[:8] == [60.0, 120.0, 240.0, 480.0, 960.0, 1800.0, 1800.0, 1800.0]
    assert BACKOFF_MAX == 1800.0

    lock.reads_unanswered = False
    await _run(rig, lambda: rig.worker.reachable, 400)
    assert rig.worker.reachable

    waits.clear()
    lock.reads_unanswered = True
    for _ in range(BACKOFF_AFTER):
        with pytest.raises(LockError):
            await rig.worker.read([1])
    await _run(rig, lambda: bool(waits), 400)
    assert waits[0] == BACKOFF_MIN


# Fights --------------------------------------------------------------------------


async def test_fights_spread_inside_the_window_hold_and_a_resume_forgets_them(
    rig_factory,  # noqa: F811
) -> None:
    """
    Three codes taken back within five minutes, minutes apart, hold the slot;
    once the user resumes it, one more is put right, not held.
    """
    lock = FakeLock({1: "1234"})
    rig = await _started(rig_factory(lock), {1: "1234"})
    step = WINDOW / 3
    for round_ in range(3):
        rig.clock.now += step - 1
        lock.codes[1] = f"000{round_}"
        rig.worker.push(1, Holds(f"000{round_}"))
        await rig.settle()

    assert rig.status(1) == (Status.SUSPENDED, Repair.CANT_SET)

    rig.worker.resume(1)
    await rig.settle()
    assert rig.status(1) == (Status.IN_SYNC, None)
    lock.codes[1] = "0009"
    rig.worker.push(1, Holds("0009"))
    await rig.settle()

    assert rig.status(1) == (Status.IN_SYNC, None)
    assert lock.codes[1] == "1234"


async def test_a_new_pin_on_a_slot_the_lock_hides_is_written_once(
    rig_factory,  # noqa: F811
) -> None:
    """
    #962: a lock that hides its codes still gets a changed PIN, once.

    The slot reads occupied whatever it holds. A code put there by someone
    else is written over once; a new PIN is written once; neither is
    written again.
    """
    lock = FakeLock({1: "1234"}, masked=True)
    rig = await _started(rig_factory(lock), {1: "1234"})
    assert lock.writes == [(1, "1234")]

    rig.worker.want(1, "4321")
    await rig.settle()
    for _ in range(3):
        await rig.pass_()

    assert lock.writes == [(1, "1234"), (1, "4321")]
    assert lock.codes == {1: "4321"}
    assert rig.status(1) == (Status.IN_SYNC, None)


async def test_a_code_deleted_at_the_lock_and_pushed_is_put_back(
    rig_factory,  # noqa: F811
) -> None:
    """#1538: an in-sync slot the lock says is now empty goes out of sync and is rewritten."""
    lock = FakeLock({1: "1234"})
    rig = await _started(rig_factory(lock), {1: "1234"})
    assert lock.writes == []

    del lock.codes[1]
    rig.worker.push(1, Seen.EMPTY)
    await rig.settle()

    assert lock.writes == [(1, "1234")]
    assert lock.codes == {1: "1234"}
    assert rig.status(1) == (Status.IN_SYNC, None)


async def test_polls_of_a_lock_with_a_held_ignored_slot(rig_factory) -> None:  # noqa: F811
    """
    A lock that fails a whole read over one slot it ignores (ZHA).

    Once slot 7 is held, polls leave it out: the drift on slot 1 is seen, and
    the lock is not backed off by a slot nothing is trying any more.
    """
    lock = FakeLock({1: "1111", 2: "2222"}, strict=True)
    rig = await _started(rig_factory(lock), {1: "1111", 2: "2222"})
    lock.ignored = {7}
    rig.worker.want(7, "7777")
    await _run(rig, lambda: rig.status(7)[0] is Status.SUSPENDED)
    assert rig.status(7)[0] is Status.SUSPENDED
    lock.codes[1] = "0000"  # changed at the keypad
    for _ in range(3):
        await _poll(rig)
    await rig.settle()
    # The drift on slot 1 is seen and put right; the lock is not taken as gone.
    assert lock.codes[1] == "1111"
    assert (1, "1111") in lock.writes
    assert rig.worker.reachable


async def test_a_probe_from_outside_leaves_no_stale_backoff_timer(rig_factory) -> None:  # noqa: F811
    """
    A lock brought back by a probe from outside (the hard refresh service)
    does not keep the old backoff's timer.

    The timer still sleeping out the old backoff blocks the next outage's
    probe from being scheduled: the lock stays down, every sync dropped,
    until that old timer fires -- up to 30 minutes for a 60s backoff.
    """
    lock = FakeLock({1: "1234"})
    rig = rig_factory(lock)
    stuck = asyncio.Event()
    first_long = True

    async def sleep(seconds: float) -> None:
        nonlocal first_long
        rig.clock.now += seconds
        if seconds >= 60 and first_long:
            # The first backoff's timer: still sleeping (a long backoff).
            first_long = False
            await stuck.wait()
        await asyncio.sleep(0)

    rig.worker.sleep = sleep
    await _started(rig, {1: "1234"})

    lock.reads_unanswered = True
    for _ in range(BACKOFF_AFTER):
        with pytest.raises(LockError):
            await rig.worker.read([1])
    assert not rig.worker.reachable

    # The hard refresh service: a probe from outside, and the lock answers.
    lock.reads_unanswered = False
    await rig.worker.read(probe=True)
    assert rig.worker.reachable

    # A second outage, and the lock answers again once probed.
    lock.reads_unanswered = True
    for _ in range(BACKOFF_AFTER):
        with pytest.raises(LockError):
            await rig.worker.read([1])
    assert not rig.worker.reachable
    lock.reads_unanswered = False
    for _ in range(50):
        if rig.worker.reachable:
            break
        await asyncio.sleep(0)
    assert rig.worker.reachable
    stuck.set()
