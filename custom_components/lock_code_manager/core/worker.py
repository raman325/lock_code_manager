"""
One worker per lock: the only thing that ever talks to it.

A single task takes jobs from a queue -- syncing one slot, a read, a push
from the lock, a backoff probe -- one at a time, with a gap between
exchanges and a time limit on each. That one fact is the whole of the
serialization, ordering, pacing and ownership a lock needs: nothing else
can talk over a job, a push is handled in the order it came, and stopping
the task stops everything the lock was doing.

The design: docs/superpowers/specs/2026-10-13-core-first-principles.md.
"""

import asyncio
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
import logging
import time
from typing import Any, Protocol

from .slot import Reading, Repair, Seen, Slot, Status, Want

_LOGGER = logging.getLogger(__name__)

BACKOFF_AFTER = 3
BACKOFF_MIN = 60.0
BACKOFF_MAX = 1800.0
OFFLINE_AFTER = 12
# How long a slot waits before trying again after its first, then its second,
# failed try: a lock refusing writes for a while (a Matter lock still coming
# up, #1257) does not spend every try in seconds. A real failure is still
# held after its tries, a few minutes on.
RETRY_AFTER = (30.0, 120.0)


class ErrorKind(StrEnum):
    """Why an exchange with the lock did not succeed: what would change it."""

    # The lock will not take this PIN (a duplicate, one it rejects).
    VALUE_REFUSED = "value_refused"
    # The lock will not let this slot be written, or will not clear it.
    SLOT_REFUSED = "slot_refused"
    # The lock answered with a failure that may not repeat.
    TRANSIENT = "transient"
    # No answer from the lock: a disconnect, a timeout, or a service in front
    # of it answering in its place.
    NO_ANSWER = "no_answer"
    # The lock already holds what was asked.
    ALREADY_SO = "already_so"


class LockError(Exception):
    """A failed exchange: what kind, and whether it reached the lock."""

    def __init__(
        self, kind: ErrorKind, *, sent: bool = True, message: str = "", cause: str = ""
    ) -> None:
        """
        Describe the failure; ``sent`` is False when nothing left for the lock.

        ``cause`` is set only when the provider named, for the user, why it
        failed.
        """
        super().__init__(message or kind.value)
        self.kind = kind
        self.sent = sent
        self.message = message
        self.cause = cause


class LockPort(Protocol):
    """What the worker needs from a provider."""

    async def read(self, slots: Collection[int]) -> Mapping[int, Reading]:
        """
        Return what the lock says about each of ``slots`` now; raise ``LockError``.

        Never a cache: a write is checked by reading its slot back, and a
        cached old value would count against a write that worked (#873,
        #1090). A provider that cannot read the lock itself leaves the slot
        out, and the write is trusted. Cached views come in as pushes.
        """
        ...

    async def write(self, slot: int, pin: str | None) -> bool | None:
        """
        Put ``pin`` in ``slot``, or clear it for None; raise ``LockError``.

        False when the lock answered that there was nothing to change -- a
        clear that found nothing: that says nothing about what the slot holds.
        """
        ...


@dataclass
class _Job:
    kind: str
    slot: int | None = None
    slots: tuple[int, ...] = ()
    reading: Reading | None = None
    # When a push was taken in: dropped if its slot was written since.
    seq: int = 0
    done: asyncio.Future[Any] | None = None
    # A turn's exchange, made by someone else's provider.
    call: Callable[[], Awaitable[Any]] | None = None


def _resolve(
    done: asyncio.Future[Any] | None,
    result: Any = None,
    error: BaseException | None = None,
) -> None:
    """Answer a caller's future, unless the caller has given up on it."""
    if done is None or done.done():
        return
    if error is not None:
        done.set_exception(error)
    else:
        done.set_result(result)


@dataclass
class LockWorker[P: LockPort]:
    """The one owner of a lock's slots and of every exchange with it."""

    port: P
    salt: bytes
    gap: float = 2.0
    # Seconds one exchange may take before it counts as no answer.
    budget: float = 60.0
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    on_change: Callable[[], None] = lambda: None
    # A write is about to be sent: what was written before is no longer
    # known to be there, and whatever remembers it has to forget it now --
    # the write may land and Home Assistant stop before it returns.
    on_send: Callable[[], None] = lambda: None
    # The lock refused this slot's PIN: the integration turns the slot off and
    # owns the repair that says why.
    on_disable: Callable[[int], None] = lambda slot: None
    # What was last written to a slot before this worker, by an earlier run:
    # a slot starts from it, so a lock that will not show its codes is not
    # written again (#405).
    recall: Callable[[int], str | None] = lambda slot: None
    # The lock answered an earlier run: one not answering now is offline.
    answered_before: bool = False
    slots: dict[int, Slot] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Start idle: nothing queued, the lock never yet reached."""
        self._queue: asyncio.Queue[_Job | None] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._retry: asyncio.Task[None] | None = None
        self._started = False
        self._stopping = False
        self._stopped = asyncio.Event()
        self._queued: set[int] = set()
        # The slot whose sync is running now.
        self._syncing: int | None = None
        # Slots waiting out their pause before trying again.
        self._waits: dict[int, asyncio.Task[None]] = {}
        self._seq = 0
        self._sent = 0
        self._written: dict[int, int] = {}
        self._last_exchange: float | None = None
        # Exchanges the lock answered: proof it is there.
        self._answers = 0
        # The provider raised something that is not a LockError.
        self._bug = False
        # Slots charged a try since the lock last answered, by Slot and epoch:
        # an outage costs each target one try at most.
        self._charged: dict[int, tuple[Slot, int]] = {}
        # Slots written since the lock last gave a reading of them, by the
        # Slot and epoch written for: the write may have landed, so what it
        # said before is no guide to a new target.
        self._unsure: dict[int, tuple[Slot, int]] = {}
        # Slots whose sync failed with nothing answering, by epoch: charged
        # when the lock next answers, as nothing else tells an outage from a
        # slot the lock ignores.
        self._owed: dict[int, tuple[Slot, int]] = {}
        self._probes = 0
        # Slots probed since the lock last answered.
        self._probed: set[int] = set()
        self._no_answers = 0
        self._down_until: float | None = None

    # The integration's side -------------------------------------------------

    def want(self, slot: int, pin: Want, *, release: bool = False) -> None:
        """Say what ``slot`` should hold; a release clear when ``release``."""
        self._stop_waiting(slot)
        if (current := self.slots.get(slot)) is None:
            self.slots[slot] = Slot(pin, wrote=self.recall(slot), release=release)
        else:
            current.aim(pin, release=release)
            if (
                slot in self._unsure
                and current.seen is not Seen.SILENT
                and current.agrees(self.salt)
            ):
                # A write since the last reading may have landed, so a reading
                # that agrees with the new target cannot vouch for it: read
                # first. One that disagrees only leads to a write: the slot
                # stays out of sync, pending, not unknown -- a read-back that
                # failed is no reason to forget what the lock said.
                current.seen = None
        self.request_sync((slot,))
        # Backing off with nothing to probe: probe this one.
        self._schedule_probe()

    def forget(self, slot: int) -> None:
        """Stop tracking ``slot`` without touching what the lock holds."""
        self._stop_waiting(slot)
        self.slots.pop(slot, None)

    def resume(self, slot: int) -> None:
        """Lift ``slot``'s hold: the user acknowledged its repair."""
        if (current := self.slots.get(slot)) is not None:
            self._stop_waiting(slot)
            current.resume()
            # Polls left it out while it was held: read it before deciding.
            current.seen = None
            self.request_sync((slot,))

    def status(self, slot: int) -> tuple[Status, Repair | None]:
        """Return ``slot``'s sync status and the repair it raises, if any."""
        current = self.slots[slot]
        return current.status(self.salt), current.hold

    def wake(self) -> None:
        """
        Probe now if backing off: something says the lock may be back.

        The provider's integration reconnecting, say. Only the probe's
        answer, as always, decides whether the lock is there.
        """
        if self._down_until is None or self._stopping:
            return
        self._down_until = self.clock()
        if self._retry is not None and not self._retry.done():
            self._retry.cancel()
        self._retry = None
        self._schedule_probe()

    def syncing(self, slot: int) -> bool:
        """Return whether a sync of ``slot`` is queued or running."""
        return slot in self._queued or slot == self._syncing

    @property
    def started(self) -> bool:
        """Return whether ``start`` was called."""
        return self._started

    @property
    def reachable(self) -> bool:
        """Return whether the lock is answering (not backing off)."""
        return self._down_until is None

    @property
    def answered(self) -> bool:
        """Return whether the lock has answered this worker at all."""
        return self._answers > 0

    @property
    def answered_ever(self) -> bool:
        """Return whether the lock has answered this worker or an earlier run."""
        return self.answered or self.answered_before

    @property
    def offline(self) -> bool:
        """
        Return whether a lock that answered before has stopped answering.

        A lock that answers for some slots and not others never gets here:
        a slot whose own exchanges go unanswered while another slot answers
        is held instead (#1644). A lock never reached is still coming up, not
        offline (#1257); one that answered an earlier run and is dead at
        this one's start is.
        """
        return self.answered_ever and self._no_answers >= OFFLINE_AFTER

    def request_sync(self, slots: Collection[int] | None = None) -> None:
        """
        Queue a sync of ``slots`` (every tracked one by default), once each.

        Before ``start`` nothing is queued: the first read comes first.
        """
        if not self._started:
            return
        for slot in tuple(self.slots if slots is None else slots):
            if slot not in self._queued:
                self._queued.add(slot)
                self._put(_Job("slot", slot=slot))

    def push(self, slot: int, reading: Reading) -> None:
        """
        Take what the lock reported on its own about ``slot``.

        A report taken in before the slot's last write finished is dropped
        when its turn comes: the write's read-back is newer.
        """
        self._put(_Job("push", slot=slot, reading=reading, seq=self._next()))

    async def read(
        self, slots: Collection[int] | None = None, *, probe: bool = False
    ) -> dict[int, Reading]:
        """
        Read ``slots`` in the lock's turn: by default every tracked slot not held.

        A held slot's reading counts for nothing until it is resumed, which
        reads it again -- and a lock that fails a read over one slot it
        ignores would otherwise fail every poll of the rest. ``probe`` when
        someone says the lock may be back (the hard refresh service): the
        read goes even while backing off, and an answer brings the lock back.
        """
        if self._stopping:
            raise LockError(ErrorKind.NO_ANSWER, sent=False, message="stopped")
        done: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        wanted = tuple(
            (n for n, slot in self.slots.items() if slot.hold is None)
            if slots is None
            else slots
        )
        self._put(_Job("probe" if probe else "read", slots=wanted, done=done))
        return await done

    async def turn[T](self, call: Callable[[], Awaitable[T]]) -> T:
        """
        Run ``call`` against the lock in its turn, as one exchange; return its result.

        For a question about the lock asked through a provider of its own (the
        migration's unmanaged-code sweep): nothing else talks to the lock
        meanwhile, and its answer counts like any other.
        """
        if self._stopping:
            raise LockError(ErrorKind.NO_ANSWER, sent=False, message="stopped")
        done: asyncio.Future[T] = asyncio.get_running_loop().create_future()
        self._put(_Job("turn", call=call, done=done))
        return await done

    async def settled(self) -> None:
        """Wait until every job queued so far, and any it queued, is done."""
        await self._queue.join()

    # Lifecycle -----------------------------------------------------------------

    def start(self) -> None:
        """Start the worker: a first read of every tracked slot, then sync."""
        self._started = True
        self._task = asyncio.create_task(self._run(), name="lock worker")
        self._put(_Job("read", slots=tuple(self.slots)))
        # Behind the read, so they find it done -- and still run if it failed.
        self.request_sync()

    async def stop(self, grace: float = 30.0) -> None:
        """
        Stop for good.

        No new jobs are taken, waiting ones are answered at once, and the job
        running now gets ``grace`` to finish before it is cancelled.
        """
        if self._stopping:
            await self._stopped.wait()
            return
        self._stopping = True
        try:
            self._drain()
            if self._task is not None and not self._task.done():
                self._queue.put_nowait(None)
                try:
                    async with asyncio.timeout(grace):
                        await asyncio.shield(self._task)
                except TimeoutError:
                    self._task.cancel()
                    # A provider that eats the cancel is left behind, not awaited.
                    await asyncio.wait({self._task}, timeout=grace)
            if self._retry is not None:
                self._retry.cancel()
                await asyncio.gather(self._retry, return_exceptions=True)
            if waits := list(self._waits.values()):
                for waiting in waits:
                    waiting.cancel()
                await asyncio.gather(*waits, return_exceptions=True)
                self._waits.clear()
        finally:
            # Even if this caller was cancelled: nothing is left running or
            # queued (a cancelled task never reaches the end-of-queue marker),
            # and no later stop() waits for ever.
            for task in (self._task, self._retry):
                if task is not None:
                    task.cancel()
            self._drain()
            self._stopped.set()

    def _drain(self) -> None:
        """Answer and mark done every job still queued."""
        while not self._queue.empty():
            job = self._queue.get_nowait()
            if job is not None:
                _resolve(
                    job.done,
                    error=LockError(ErrorKind.NO_ANSWER, sent=False, message="stopped"),
                )
            self._queue.task_done()

    def _put(self, job: _Job) -> None:
        if not self._stopping:
            self._queue.put_nowait(job)

    # The loop ------------------------------------------------------------------

    async def _run(self) -> None:
        while True:
            if self._stopping and self._queue.empty():
                return  # it ate stop's cancel; nothing is left to do
            if (job := await self._queue.get()) is None:
                break
            try:
                await self._do(job)
            except LockError as err:
                _resolve(job.done, error=err)
            except asyncio.CancelledError:
                # Cut off by stop: its caller still gets an answer.
                _resolve(
                    job.done,
                    error=LockError(ErrorKind.NO_ANSWER, sent=False, message="stopped"),
                )
                raise
            except Exception as err:  # one bad job must not stop the lock
                _LOGGER.exception("Lock job %s failed", job.kind)
                _resolve(job.done, error=err)
            finally:
                self._queue.task_done()
            try:
                self.on_change()
            except Exception:  # the integration's trouble must not stop the lock
                _LOGGER.exception("Lock change callback failed")
        self._queue.task_done()

    async def _do(self, job: _Job) -> None:
        if job.kind == "push":
            self._take_push(job)
            return
        if job.kind == "slot":
            assert job.slot is not None
            self._queued.discard(job.slot)
        if self._down_until is not None and job.kind != "probe":
            # Backing off: callers hear at once that the lock is not
            # answering; slot syncs are queued again once it answers.
            _resolve(job.done, error=LockError(ErrorKind.NO_ANSWER, sent=False))
            return
        if job.kind == "slot":
            assert job.slot is not None
            self._syncing = job.slot
            try:
                await self._sync(job.slot)
            finally:
                self._syncing = None
            self._again(job.slot)
            return
        if job.kind == "turn":
            assert job.call is not None
            _resolve(job.done, await self._exchange(job.call))
            return
        # A probe left unanswered schedules the next one through _no_answer.
        _resolve(job.done, await self._read(job.slots))

    def _take_push(self, job: _Job) -> None:
        assert job.slot is not None and job.reading is not None
        slot = self.slots.get(job.slot)
        if slot is None or self._written.get(job.slot, 0) > job.seq:
            return
        if (unsure := self._unsure.get(job.slot)) is not None and (
            unsure[0] is not slot or unsure[1] != slot.epoch
        ):
            # A write for another target since the last reading may have
            # landed, and a report may be a cache older than it: one that
            # agrees with this target cannot vouch for it, so read first. One
            # that disagrees only leads to a write, as in ``want``: the slot
            # shows out of sync, pending, not unknown.
            slot.seen = job.reading
            if slot.agrees(self.salt):
                slot.seen = None
        else:
            slot.observe(job.reading, self.salt, self.clock(), fresh=False)
        self.request_sync((job.slot,))

    async def _sync(self, number: int) -> None:
        """
        Bring one slot in line: read it if never read, write it if it differs.

        A sync that ends out of sync costs the slot one try, charged once the
        lock has shown it is there: at once if anything in the sync was
        answered, else when the lock next answers. Between two answers a slot
        is charged once at most, so an outage costs it one try at most. A
        provider bug is the slot's: charged at once, every time. Nothing is
        awaited between looking and charging, and a sync begun before the
        counts started over is not charged.

        So every sync that does not end in sync is charged, or owed while
        nothing answers -- and only an answer resets the lock's no-answers:
        each slot ends in sync, held, or with the lock offline.
        """
        if (slot := self.slots.get(number)) is None:
            return
        epoch, answers, self._bug = slot.epoch, self._answers, False
        try:
            if slot.status(self.salt) is Status.UNKNOWN:
                await self._read((number,), follow=False)
            if slot.status(self.salt) is Status.OUT_OF_SYNC:
                await self._write(number, slot)
        except LockError as err:
            _LOGGER.debug("Sync of slot %s failed: %s", number, err)
            if err.kind is ErrorKind.SLOT_REFUSED and slot.epoch == epoch:
                # A read refused for the slot (past the lock's capacity).
                slot.refused(value=False, why=err.message)
        if slot.epoch == epoch and self._failed(slot):
            if self._bug:
                slot.tried()
            elif self._answers > answers:
                self._charge(number, slot)
            elif not self._was_charged(number, slot):
                self._owed[number] = (slot, epoch)
        if (
            slot.release
            and slot.status(self.salt) is Status.IN_SYNC
            and self.slots.get(number) is slot
        ):
            del self.slots[number]

    def _failed(self, slot: Slot) -> bool:
        return slot.status(self.salt) in (Status.UNKNOWN, Status.OUT_OF_SYNC)

    def _charge(self, number: int, slot: Slot) -> None:
        """Charge ``slot`` one try, unless charged since the lock last answered."""
        if not self._was_charged(number, slot):
            self._charged[number] = (slot, slot.epoch)
            slot.tried()

    def _was_charged(self, number: int, slot: Slot) -> bool:
        """Return whether this target was charged since the lock last answered."""
        charged = self._charged.get(number)
        return charged is not None and charged[0] is slot and charged[1] == slot.epoch

    async def _write(self, number: int, slot: Slot) -> None:
        """
        Write what is wanted, then read it back.

        What was sent is what is recorded; if the slot changed hands or wants
        another PIN by the time the write is back, nothing is recorded or read
        back; the write may have landed, so ``want`` forgot what the lock
        last said if it agrees with the new target, and the next sync reads
        before it decides. A refusal holds at once.
        Anything else, even a write that never left, is read back -- unless
        the lock has just gone into backoff -- and a cause the provider named
        is kept, so a slot held after its tries says it.
        """
        pin = slot.want
        slot.wrote = None

        def current() -> bool:
            # Still the slot in place, still wanting what was sent.
            return self.slots.get(number) is slot and slot.want == pin

        # Forgotten or retargeted while waiting its turn: send nothing.
        await self._pace()
        if not current():
            return
        self._unsure[number] = (slot, slot.epoch)
        try:
            self.on_send()
        except Exception:  # the integration's trouble must not stop the lock
            _LOGGER.exception("Lock send callback failed")
        try:
            # Reports taken in before the write is sent are older than it.
            changed = await self._exchange(
                lambda: self.port.write(number, pin), mark=number
            )
        except LockError as err:
            if not current():
                return
            # If it is held after its tries, a cause the provider named says why.
            slot.cause = err.cause
            if err.kind is ErrorKind.ALREADY_SO:
                slot.wrote_want(self.salt)
                slot.released = slot.release
            elif err.kind is ErrorKind.VALUE_REFUSED:
                slot.refused(value=True, why=err.message)
                if pin is not None:
                    self.on_disable(number)
                return
            elif err.kind is ErrorKind.SLOT_REFUSED:
                slot.refused(value=False, why=err.message)
                return
        else:
            if not current():
                return
            slot.cause = ""
            slot.released = slot.release
            if changed is not False:
                slot.wrote_want(self.salt)
        if self._down_until is None:
            await self._read((number,), follow=False)

    def _next(self) -> int:
        """Return the next point in the order reports and exchanges happen."""
        self._seq += 1
        return self._seq

    async def _read(
        self, numbers: Collection[int], *, follow: bool = True
    ) -> dict[int, Reading]:
        """
        Read ``numbers``; unless ``follow`` is off, queue any slot that needs work.

        A reading is newer than every report on its slot taken in before the
        read was sent; one taken in while it was out may be newer, and is
        kept. A slot the read left out says nothing, and drops no report.
        """
        if not numbers:
            # Reading nothing asks the lock nothing: no answer, no no-answer.
            return {}

        async def _readings() -> dict[int, Reading]:
            # Inside the exchange: a provider returning garbage is its bug.
            return dict(await self.port.read(numbers))

        readings = await self._exchange(_readings, pay=False)
        due = self._due()
        for number in numbers:
            if number in readings:
                self._written[number] = self._sent
                self._unsure.pop(number, None)
            reading = readings.setdefault(number, Seen.SILENT)
            if (slot := self.slots.get(number)) is not None:
                slot.observe(reading, self.salt, self.clock())
                if follow:
                    self._again(number)
        self._pay(due)
        return readings

    def _again(self, number: int) -> None:
        """
        Queue another sync of ``number`` if it still needs one.

        Every sync that does not end in sync is charged to the slot, or owed
        until the lock next answers -- and with no answer, the lock backs off
        on its way to the offline repair. So this ends in sync, held, or with
        the lock offline -- never in a loop. (A job queued while backing off
        fails at once, and an answer queues every slot again.)
        """
        slot = self.slots.get(number)
        if slot is None or not self._failed(slot):
            return
        delay = RETRY_AFTER[min(slot.tries, len(RETRY_AFTER)) - 1] if slot.tries else 0
        if delay <= 0:
            self.request_sync((number,))
            return
        # Charged a try: wait a while before the next, unless already waiting.
        if (waiting := self._waits.get(number)) is not None and not waiting.done():
            return
        self._waits[number] = asyncio.create_task(self._retry_later(number, delay))

    async def _retry_later(self, number: int, delay: float) -> None:
        await self.sleep(delay)
        self._waits.pop(number, None)
        self.request_sync((number,))

    def _stop_waiting(self, number: int) -> None:
        if (waiting := self._waits.pop(number, None)) is not None:
            waiting.cancel()

    async def _exchange(
        self,
        call: Callable[[], Awaitable[Any]],
        *,
        mark: int | None = None,
        pay: bool = True,
    ) -> Any:
        """
        Run one exchange after the gap, within the budget.

        ``_sent`` is when it was sent, after the gap: reports on ``mark``
        taken in before then are dropped.

        No answer -- the budget running out, a ``NO_ANSWER``, a transient
        failure that never left for the lock, or a network error -- counts
        against the lock. Any other ``LockError`` that was sent is the lock
        answering, refusals included. One that never left (the integration
        refusing) and anything else a provider raises (a bug in it, logged)
        say nothing about the lock: a bug is a transient failure charged to
        the slot it hits -- or, on a probe, no answer, so probing goes on.
        """
        await self._pace()
        self._sent = self._next()
        if mark is not None:
            self._written[mark] = self._sent
        try:
            async with asyncio.timeout(self.budget):
                result = await call()
        except LockError as err:
            if err.kind is ErrorKind.NO_ANSWER or (
                not err.sent and err.kind is ErrorKind.TRANSIENT
            ):
                self._no_answer()
            elif err.sent:
                self._answer()
                self._pay()
            elif self._down_until is not None:
                # The integration refusing, unsent: the slot is held for it,
                # and it says nothing of the lock being back.
                self._no_answer()
            raise
        except asyncio.CancelledError:
            if (task := asyncio.current_task()) is not None and task.cancelling():
                raise
            self._no_answer()
            raise LockError(ErrorKind.NO_ANSWER, message="cancelled") from None
        except OSError as err:  # timeouts and dropped connections included
            self._no_answer()
            raise LockError(
                ErrorKind.NO_ANSWER, message=str(err) or "no answer"
            ) from err
        except Exception as err:
            _LOGGER.exception("Lock provider failed")
            self._bug = True
            if self._down_until is not None:
                self._no_answer()
            raise LockError(ErrorKind.TRANSIENT, message=str(err)) from err
        finally:
            self._last_exchange = self.clock()
        self._answer()
        if pay:
            self._pay()
        return result

    async def _pace(self) -> None:
        """Wait out the gap since the last exchange."""
        if self._last_exchange is not None:
            wait = self._last_exchange + self.gap - self.clock()
            if wait > 0:
                await self.sleep(wait)

    def _answer(self) -> None:
        was_down = self._down_until is not None
        self._answers += 1
        self._charged.clear()
        self._probed.clear()
        self._no_answers = 0
        self._down_until = None
        if was_down:
            # Back -- maybe by a probe from outside (the hard refresh): the
            # old backoff's timer would hold up the next outage's probes.
            if self._retry is not None and not self._retry.done():
                self._retry.cancel()
            self._retry = None
            # Every slot dropped while backing off is synced again.
            self.request_sync()

    def _due(self) -> list[tuple[int, Slot]]:
        """Take the owed tries: the lock answered, so they were the slots'."""
        owed, self._owed = self._owed, {}
        return [
            (number, owing)
            for number, (owing, epoch) in owed.items()
            if owing.epoch == epoch and self._failed(owing)
        ]

    def _pay(self, due: list[tuple[int, Slot]] | None = None) -> None:
        """Charge owed tries still failing once the answer's reading is in."""
        for number, slot in self._due() if due is None else due:
            if self._failed(slot):
                self._charge(number, slot)

    def _no_answer(self) -> None:
        self._no_answers += 1
        if self._no_answers < BACKOFF_AFTER or self._stopping:
            return
        delay = min(
            BACKOFF_MIN * 2 ** min(self._no_answers - BACKOFF_AFTER, 16), BACKOFF_MAX
        )
        self._down_until = self.clock() + delay
        self._schedule_probe()

    def _schedule_probe(self) -> None:
        if self._stopping or self._down_until is None:
            return
        if self._retry is None or self._retry.done():
            delay = max(0.0, self._down_until - self.clock())
            self._retry = asyncio.create_task(self._probe_after(delay))

    async def _probe_after(self, delay: float) -> None:
        """
        After the backoff, read one slot; an answer brings the lock back.

        Each probe reads the next slot not held in turn -- every slot in
        turn once those have all gone unanswered since the lock last
        answered -- so a slot the lock ignores cannot keep it down for long;
        a provider bug on one is no answer.
        """
        await self.sleep(delay)
        free = sorted(n for n, slot in self.slots.items() if slot.hold is None)
        if numbers := free if set(free) - self._probed else sorted(self.slots):
            # With nothing to read, the next slot wanted starts the probing.
            self._probes += 1
            number = numbers[self._probes % len(numbers)]
            self._probed.add(number)
            self._put(_Job("probe", slots=(number,)))
