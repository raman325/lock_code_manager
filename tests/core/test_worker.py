"""
The lock worker, scenario by scenario, against a fake lock and a fake clock.

Each scenario is a case from the bug catalog or the credential-model
decisions, driven only through the worker's own API.
"""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from dataclasses import dataclass, field

import pytest

from custom_components.lock_code_manager.core.slot import (
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
    refuse_clear: set[int] = field(default_factory=set)
    # Writes are acknowledged and not applied.
    ignore_writes: bool = False
    writes_unanswered: bool = False
    reads_unanswered: bool = False
    writes_never_sent: bool = False
    transient: int = 0
    hang: asyncio.Event | None = None
    # Raise something that is not a LockError on writes.
    broken: bool = False
    writes: list[tuple[int, str | None]] = field(default_factory=list)
    during_write: list = field(default_factory=list)

    async def read(self, slots: Collection[int]) -> dict[int, Reading]:
        """Answer a read the way this lock does."""
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
        if self.writes_never_sent:
            raise LockError(ErrorKind.NO_ANSWER, sent=False)
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
            raise LockError(ErrorKind.VALUE_REFUSED)
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
        """Run until the worker has nothing left to do."""
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
    assert rig.worker.slots[1].failures == []


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


async def test_a_write_that_never_reached_the_lock_counts_nothing(rig_factory) -> None:
    """Nothing was sent: no failure against the slot, and nothing to read back."""
    rig = await _started(rig_factory(FakeLock(writes_never_sent=True)), {1: "1234"})
    await rig.pass_()

    assert rig.worker.slots[1].failures == []
    assert rig.status(1) == (Status.OUT_OF_SYNC, None)


async def test_a_write_that_hangs_is_cut_off_and_retried(rig_factory) -> None:
    """A wedged write frees the lock within its budget (#1523), and is retried."""
    hang = asyncio.Event()
    rig = rig_factory(FakeLock(hang=hang), budget=0.05, gap=0.0)
    await _started(rig, {1: "1234"})
    assert rig.status(1) == (Status.OUT_OF_SYNC, None)

    rig.lock.hang = None
    await rig.pass_()
    assert rig.status(1) == (Status.IN_SYNC, None)


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

    assert rig.worker.slots[2].failures == []
    assert lock.writes == [(1, "1111"), (2, "2222")]
    assert rig.status(2) == (Status.IN_SYNC, None)


async def test_a_broken_provider_fails_its_slot_not_the_pass(rig_factory) -> None:
    """Any error a provider raises counts against that slot; others still sync."""
    lock = FakeLock(broken=True)
    rig = await _started(rig_factory(lock), {1: "1234", 2: "5678"})

    assert len(rig.worker.slots[1].failures) == 1
    assert len(rig.worker.slots[2].failures) == 1
    assert lock.writes == [(1, "1234"), (2, "5678")]


async def test_an_unanswered_write_to_an_unanswering_lock_charges_nothing(
    rig_factory,
) -> None:
    """A lock that has gone is the lock's problem, not the slot's."""
    lock = FakeLock()
    rig = await _started(rig_factory(lock), {1: "1234"})
    lock.writes_unanswered = True
    lock.reads_unanswered = True
    rig.worker.want(1, "4321")
    await rig.settle()

    assert lock.writes[-1] == (1, "4321")
    assert rig.worker.slots[1].failures == []


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
