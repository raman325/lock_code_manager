"""
One worker per lock: the only thing that ever talks to it.

A single task takes jobs from a queue -- syncing slots, reads, pushes from
the lock -- one at a time, with a gap between exchanges. That one fact is
the whole of the serialization, ordering, pacing and ownership a lock
needs: nothing else can talk over a job, a push is handled in the order it
came, and stopping the task stops everything the lock was doing.

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
    slots: tuple[int, ...] = ()
    reading: Reading | None = None
    done: asyncio.Future[Any] | None = None
    probe: bool = False


@dataclass
class LockWorker:
    """The one owner of a lock's slots and of every exchange with it."""

    port: LockPort
    salt: bytes
    gap: float = 2.0
    budget: float = 60.0
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    on_change: Callable[[], None] = lambda: None
    on_disable: Callable[[int], None] = lambda slot: None
    slots: dict[int, Slot] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Start idle: nothing queued, the lock never yet reached."""
        self._queue: asyncio.Queue[_Job | None] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._retry: asyncio.Task[None] | None = None
        self._sync_queued = False
        self._writing: int | None = None
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
        self.request_sync()

    def forget(self, slot: int) -> None:
        """Stop tracking ``slot`` without touching what the lock holds."""
        self.slots.pop(slot, None)

    def resume(self, slot: int) -> None:
        """Lift ``slot``'s hold: the user acknowledged its repair."""
        if (current := self.slots.get(slot)) is not None:
            current.resume()
            self.request_sync()

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

    def request_sync(self) -> None:
        """Queue a sync pass, unless one is already waiting."""
        if not self._sync_queued:
            self._sync_queued = True
            self._queue.put_nowait(_Job("sync"))

    def push(self, slot: int, reading: Reading) -> None:
        """
        Take what the lock reported on its own about ``slot``.

        A report for the slot being written right now is dropped: the write's
        read-back is newer.
        """
        if slot != self._writing:
            self._queue.put_nowait(_Job("push", (slot,), reading))

    async def settled(self) -> None:
        """Wait until every job queued so far, and any it queued, is done."""
        await self._queue.join()

    async def read(self, slots: Collection[int] | None = None) -> dict[int, Reading]:
        """Read ``slots`` (every tracked one by default), in the lock's turn."""
        done: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        wanted = tuple(self.slots if slots is None else slots)
        self._queue.put_nowait(_Job("read", wanted, done=done))
        return await done

    # Lifecycle -----------------------------------------------------------------

    def start(self) -> None:
        """Start the worker: a first read of every tracked slot, then sync."""
        self._task = asyncio.create_task(self._run(), name="lock worker")
        self._queue.put_nowait(_Job("read", tuple(self.slots)))
        self.request_sync()

    async def stop(self, grace: float = 30.0) -> None:
        """Let the current job finish within ``grace``, then stop for good."""
        if self._retry is not None:
            self._retry.cancel()
        if self._task is None or self._task.done():
            return
        self._queue.put_nowait(None)
        try:
            async with asyncio.timeout(grace):
                await asyncio.shield(self._task)
        except TimeoutError:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    # The loop ------------------------------------------------------------------

    async def _run(self) -> None:
        while (job := await self._queue.get()) is not None:
            try:
                await self._do(job)
            except Exception as err:  # one bad job must not stop the lock
                _LOGGER.exception("Lock job %s failed", job.kind)
                if job.done is not None and not job.done.done():
                    job.done.set_exception(err)
            finally:
                self._queue.task_done()
            self.on_change()
        self._queue.task_done()

    async def _do(self, job: _Job) -> None:
        if job.kind == "push":
            if (slot := self.slots.get(job.slots[0])) is not None:
                assert job.reading is not None
                slot.observe(job.reading, self.salt, self.clock())
                self.request_sync()
            return
        if self._down_until is not None and not job.probe:
            if job.done is not None:
                job.done.set_exception(LockError(ErrorKind.NO_ANSWER, sent=False))
            if job.kind == "sync":
                self._sync_queued = False
            return
        if job.kind == "read":
            readings = await self._read(job.slots)
            if job.done is not None:
                job.done.set_result(readings)
            return
        self._sync_queued = False
        await self._sync()

    async def _sync(self) -> None:
        for number, slot in list(self.slots.items()):
            if self._down_until is not None:
                return
            if slot.step(self.salt) is Step.READ:
                try:
                    await self._read((number,))
                except LockError:
                    continue
            if slot.step(self.salt) is Step.WRITE:
                await self._write(number, slot)
            if slot.release and slot.status(self.salt) is Status.IN_SYNC:
                del self.slots[number]

    async def _write(self, number: int, slot: Slot) -> None:
        """
        Write what is wanted, then read it back.

        A write that raised (having reached the lock) is a failure; a
        read-back that disagrees with the write is one too, counted where it
        is observed. A refused value or slot holds at once; a write that never
        reached the lock counts nothing against the slot.
        """
        slot.wrote = None
        raised = False
        self._writing = number
        try:
            await self._exchange(lambda: self.port.write(number, slot.want))
        except LockError as err:
            if err.kind is ErrorKind.ALREADY_SO:
                slot.wrote_want(self.salt)
            elif not err.sent:
                return
            elif err.kind is ErrorKind.VALUE_REFUSED:
                slot.refused(value=True)
                if slot.want is not None:
                    self.on_disable(number)
                return
            elif err.kind is ErrorKind.SLOT_REFUSED:
                slot.refused(value=False)
                return
            else:
                raised = True
        else:
            slot.wrote_want(self.salt)
        finally:
            self._writing = None
        if raised:
            slot.failed(self.clock())
        try:
            await self._read((number,))
        except LockError:
            # A read-back that went unanswered charges nothing.
            return

    async def _read(self, numbers: Collection[int]) -> dict[int, Reading]:
        readings = dict(await self._exchange(lambda: self.port.read(numbers)))
        for number in numbers:
            reading = readings.setdefault(number, Seen.SILENT)
            if (slot := self.slots.get(number)) is not None:
                slot.observe(reading, self.salt, self.clock())
        return readings

    async def _exchange(self, call: Callable[[], Awaitable[Any]]) -> Any:
        """Run one exchange: after the gap, within the budget; note who answered."""
        if self._last_exchange is not None:
            wait = self._last_exchange + self.gap - self.clock()
            if wait > 0:
                await self.sleep(wait)
        try:
            async with asyncio.timeout(self.budget):
                result = await call()
        except TimeoutError:
            self._no_answer()
            raise LockError(ErrorKind.NO_ANSWER, message="no answer in time") from None
        except LockError as err:
            if err.kind is ErrorKind.NO_ANSWER or not err.sent:
                self._no_answer()
            else:
                self._answer()
            raise
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
        if self._no_answers < BACKOFF_AFTER:
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
        self._queue.put_nowait(_Job("read", tuple(self.slots), probe=True))
        self._queue.put_nowait(_Job("sync", probe=True))
