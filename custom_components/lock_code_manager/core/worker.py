"""
One worker per lock: the only thing that ever talks to it.

A single task takes jobs from a queue -- syncing one slot, a read, a push
from the lock, a backoff probe -- one at a time, with a gap between
exchanges and a time limit on each job. That one fact is the whole of the
serialization, ordering, pacing and ownership a lock needs: nothing else
can talk over a job, a push is handled in the order it came, and stopping
the task stops everything the lock was doing.

The design: docs/superpowers/specs/2026-10-13-core-first-principles.md.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
import logging
import time
from typing import Any, Protocol

from .slot import Reading, Repair, Seen, Slot, Status, Step, Want

_LOGGER = logging.getLogger(__name__)

BACKOFF_AFTER = 3
BACKOFF_MIN = 60.0
BACKOFF_MAX = 1800.0
OFFLINE_AFTER = 12
# A slot's sync is at most a read, a write and its read-back.
SLOT_EXCHANGES = 3


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
        self, kind: ErrorKind, *, sent: bool = True, message: str = ""
    ) -> None:
        """Describe the failure; ``sent`` is False when nothing left for the lock."""
        super().__init__(message or kind.value)
        self.kind = kind
        self.sent = sent


class LockPort(Protocol):
    """What the worker needs from a provider."""

    async def read(self, slots: Collection[int]) -> Mapping[int, Reading]:
        """Return what the lock says about each of ``slots``; raise ``LockError``."""
        ...

    async def write(self, slot: int, pin: str | None) -> None:
        """Put ``pin`` in ``slot``, or clear it for None; raise ``LockError``."""
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
class LockWorker:
    """The one owner of a lock's slots and of every exchange with it."""

    port: LockPort
    salt: bytes
    gap: float = 2.0
    # Seconds one exchange may take; a job's limit is this per exchange.
    budget: float = 60.0
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    on_change: Callable[[], None] = lambda: None
    # The lock refused this slot's PIN: the integration turns the slot off and
    # owns the repair that says why.
    on_disable: Callable[[int], None] = lambda slot: None
    slots: dict[int, Slot] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Start idle: nothing queued, the lock never yet reached."""
        self._queue: asyncio.Queue[_Job | None] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._retry: asyncio.Task[None] | None = None
        self._stopping = False
        self._queued: set[int] = set()
        self._seq = 0
        self._written: dict[int, int] = {}
        self._last_exchange: float | None = None
        self._answered = False
        self._no_answers = 0
        self._down_until: float | None = None

    # The integration's side -------------------------------------------------

    def want(self, slot: int, pin: Want, *, release: bool = False) -> None:
        """Say what ``slot`` should hold; a release clear when ``release``."""
        if (current := self.slots.get(slot)) is None:
            self.slots[slot] = Slot(pin, release=release)
        else:
            current.aim(pin, release=release)
        self.request_sync((slot,))

    def forget(self, slot: int) -> None:
        """Stop tracking ``slot`` without touching what the lock holds."""
        self.slots.pop(slot, None)

    def resume(self, slot: int) -> None:
        """Lift ``slot``'s hold: the user acknowledged its repair."""
        if (current := self.slots.get(slot)) is not None:
            current.resume()
            self.request_sync((slot,))

    def status(self, slot: int) -> tuple[Status, Repair | None]:
        """Return ``slot``'s sync status and the repair it raises, if any."""
        current = self.slots[slot]
        return current.status(self.salt), current.hold

    @property
    def reachable(self) -> bool:
        """Return whether the lock is answering (not backing off)."""
        return self._down_until is None

    @property
    def offline(self) -> bool:
        """
        Return whether a lock that answered before has stopped answering.

        Every write is read back and every backoff probe is a read, so a lock
        whose writes fail while it still answers reads never gets here: its
        slots hold instead (#1644). A lock never reached is still coming up,
        not offline (#1257).
        """
        return self._answered and self._no_answers >= OFFLINE_AFTER

    def request_sync(self, slots: Collection[int] | None = None) -> None:
        """Queue a sync of ``slots`` (every tracked one by default), once each."""
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
        self._seq += 1
        self._put(_Job("push", slot=slot, reading=reading, seq=self._seq))

    async def read(self, slots: Collection[int] | None = None) -> dict[int, Reading]:
        """Read ``slots`` (every tracked one by default), in the lock's turn."""
        if self._stopping:
            raise LockError(ErrorKind.NO_ANSWER, sent=False, message="stopped")
        done: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        wanted = tuple(self.slots if slots is None else slots)
        self._put(_Job("read", slots=wanted, done=done))
        return await done

    async def settled(self) -> None:
        """Wait until every job queued so far, and any it queued, is done."""
        await self._queue.join()

    # Lifecycle -----------------------------------------------------------------

    def start(self) -> None:
        """Start the worker: a first read of every tracked slot, then sync."""
        self._task = asyncio.create_task(self._run(), name="lock worker")
        self._put(_Job("read", slots=tuple(self.slots)))
        self.request_sync()

    async def stop(self, grace: float = 30.0) -> None:
        """
        Stop for good.

        No new jobs are taken, waiting ones are answered at once, and the job
        running now gets ``grace`` to finish before it is cancelled.
        """
        if self._stopping:
            return
        self._stopping = True
        while not self._queue.empty():
            job = self._queue.get_nowait()
            if job is not None:
                _resolve(
                    job.done,
                    error=LockError(ErrorKind.NO_ANSWER, sent=False, message="stopped"),
                )
            self._queue.task_done()
        if self._task is not None and not self._task.done():
            self._queue.put_nowait(None)
            try:
                async with asyncio.timeout(grace):
                    await asyncio.shield(self._task)
            except TimeoutError:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)
        if self._retry is not None:
            self._retry.cancel()
            await asyncio.gather(self._retry, return_exceptions=True)

    def _put(self, job: _Job) -> None:
        if not self._stopping:
            self._queue.put_nowait(job)

    # The loop ------------------------------------------------------------------

    async def _run(self) -> None:
        while (job := await self._queue.get()) is not None:
            try:
                async with asyncio.timeout(self._limit(job)):
                    await self._do(job)
            except TimeoutError:
                # A job past its limit frees the lock (#1523) and counts as a
                # no-answer; whatever it was writing stays unknown.
                self._no_answer()
                _resolve(job.done, error=LockError(ErrorKind.NO_ANSWER))
            except LockError as err:
                _resolve(job.done, error=err)
            except Exception as err:  # one bad job must not stop the lock
                _LOGGER.exception("Lock job %s failed", job.kind)
                _resolve(job.done, error=err)
            finally:
                self._queue.task_done()
            self.on_change()
            if self._stopping:
                break
        if job is None:
            self._queue.task_done()

    def _limit(self, job: _Job) -> float:
        exchanges = SLOT_EXCHANGES if job.kind == "slot" else 1
        return exchanges * (self.budget + self.gap)

    async def _do(self, job: _Job) -> None:
        if job.kind == "push":
            self._take_push(job)
            return
        if job.kind == "slot":
            assert job.slot is not None
            self._queued.discard(job.slot)
        if self._down_until is not None and job.kind != "probe":
            # Backing off: callers hear at once that the lock is not
            # answering; slot syncs are queued again once a probe gets through.
            _resolve(job.done, error=LockError(ErrorKind.NO_ANSWER, sent=False))
            return
        if job.kind == "slot":
            assert job.slot is not None
            await self._sync(job.slot)
            return
        readings = await self._read(job.slots)
        _resolve(job.done, readings)
        if job.kind == "probe":
            self.request_sync()

    def _take_push(self, job: _Job) -> None:
        assert job.slot is not None and job.reading is not None
        slot = self.slots.get(job.slot)
        if slot is None or self._written.get(job.slot, 0) > job.seq:
            return
        slot.observe(job.reading, self.salt, self.clock())
        self.request_sync((job.slot,))

    async def _sync(self, number: int) -> None:
        if (slot := self.slots.get(number)) is None:
            return
        if slot.step(self.salt) is Step.READ:
            await self._read((number,))
        if slot.step(self.salt) is Step.WRITE:
            await self._write(number, slot)
        if slot.release and slot.status(self.salt) is Status.IN_SYNC:
            del self.slots[number]

    async def _write(self, number: int, slot: Slot) -> None:
        """
        Write what is wanted, then read it back.

        What was sent is what is recorded; if the target changed while the
        write was out, the new target starts clean. A write that raised having
        reached the lock is a failure. One that went unanswered is a failure
        only if the lock then answers the read-back -- a lock that answers but
        whose writes do not get through (#1644) -- otherwise the lock is
        simply not answering. A read-back that disagrees with the write is a
        failure too, counted where it is observed. A refused value or slot
        holds at once; a write that never left counts nothing against the
        slot.
        """
        pin = slot.want
        slot.wrote = None
        unanswered = False
        try:
            await self._exchange(lambda: self.port.write(number, pin))
        except LockError as err:
            if slot.want != pin:
                pass
            elif err.kind is ErrorKind.ALREADY_SO:
                slot.wrote_want(self.salt)
            elif not err.sent:
                return
            elif err.kind is ErrorKind.VALUE_REFUSED:
                slot.refused(value=True)
                if pin is not None:
                    self.on_disable(number)
                return
            elif err.kind is ErrorKind.SLOT_REFUSED:
                slot.refused(value=False)
                return
            elif err.kind is ErrorKind.NO_ANSWER:
                unanswered = True
            else:
                slot.failed(self.clock())
        else:
            if slot.want == pin:
                slot.wrote_want(self.salt)
        try:
            await self._read((number,))
        finally:
            self._seq += 1
            self._written[number] = self._seq
        if unanswered and slot.want == pin:
            slot.failed(self.clock())

    async def _read(self, numbers: Collection[int]) -> dict[int, Reading]:
        readings = dict(await self._exchange(lambda: self.port.read(numbers)))
        for number in numbers:
            reading = readings.setdefault(number, Seen.SILENT)
            if (slot := self.slots.get(number)) is not None:
                slot.observe(reading, self.salt, self.clock())
        return readings

    async def _exchange(self, call: Callable[[], Awaitable[Any]]) -> Any:
        """Run one exchange after the gap; note whether the lock answered."""
        if self._last_exchange is not None:
            wait = self._last_exchange + self.gap - self.clock()
            if wait > 0:
                await self.sleep(wait)
        try:
            result = await call()
        except LockError as err:
            if err.kind is ErrorKind.NO_ANSWER or not err.sent:
                self._no_answer()
            else:
                self._answer()
            raise
        except Exception as err:
            # Anything else a provider raises is a failure that may not
            # repeat, not a reason to stop syncing the lock's other slots.
            raise LockError(ErrorKind.TRANSIENT, message=str(err)) from err
        finally:
            self._last_exchange = self.clock()
        self._answer()
        return result

    def _answer(self) -> None:
        self._answered = True
        self._no_answers = 0
        self._down_until = None

    def _no_answer(self) -> None:
        self._no_answers += 1
        if self._no_answers < BACKOFF_AFTER or self._stopping:
            return
        delay = min(
            BACKOFF_MIN * 2 ** min(self._no_answers - BACKOFF_AFTER, 16), BACKOFF_MAX
        )
        self._down_until = self.clock() + delay
        if self._retry is None or self._retry.done():
            self._retry = asyncio.create_task(self._probe_after(delay))

    async def _probe_after(self, delay: float) -> None:
        """After the backoff, read every slot; an answer brings the lock back."""
        await self.sleep(delay)
        self._put(_Job("probe", slots=tuple(self.slots)))
