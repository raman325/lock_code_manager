"""
One worker per lock, shared by every entry that manages it.

Entries hold a lock and claim its slots. A slot has one owner at most; only
its owner sets what it holds. The lock's worker runs while an entry holds
the lock or a slot given up is still being cleared, and is stopped when
neither is true. Pure asyncio: the integration passes in how a worker is
built, how its lock is prepared before the worker first reads it (the
provider's setup), and how it is cleaned up after the worker stops.
"""

import asyncio
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass, field
import itertools
import logging
from typing import Any

from .slot import Key, Reading, Want
from .worker import LockPort, LockWorker

_LOGGER = logging.getLogger(__name__)

type Hook[K: Key, P: LockPort[Any]] = Callable[[str, LockWorker[K, P]], Awaitable[None]]


async def _nothing(lock: str, worker: LockWorker[Any, Any]) -> None:
    """Prepare or clean up nothing."""


def _itself(key: Any) -> Any:
    """Return ``key``: a worker keyed by slot numbers alone."""
    return key


class SlotTaken(Exception):
    """Another entry owns the slot."""

    def __init__(self, slot: int, owner: str) -> None:
        """Name the slot and its owner."""
        super().__init__(f"slot {slot} is owned by {owner}")
        self.slot = slot
        self.owner = owner


@dataclass
class _Lock[K: Key, P: LockPort[Any]]:
    worker: LockWorker[K, P]
    holders: set[str] = field(default_factory=set)
    # Slot -> the entry that owns it, with every key in it. A slot given up
    # has no owner.
    owners: dict[int, str] = field(default_factory=dict)
    # Preparing the lock, once an entry holds it: never for a one-off read.
    prepared: asyncio.Task[None] | None = None


class LockRegistry[K: Key, P: LockPort[Any]]:
    """Lock id -> its one worker; which entries hold it and own which slots."""

    def __init__(
        self,
        make_worker: Callable[[str], LockWorker[K, P]],
        grace: float = 30.0,
        *,
        prepare: Hook[K, P] = _nothing,
        cleanup: Hook[K, P] = _nothing,
        slot_of: Callable[[K], int] = _itself,
    ) -> None:
        """
        Build workers with ``make_worker``; give each ``grace`` to stop.

        ``prepare`` runs once an entry holds the lock, before a new worker's
        first read; ``cleanup`` runs after every worker stops, once
        ``prepare`` is done if it ran -- a one-off read's worker included.
        ``slot_of`` names the slot a key is in: an entry owns a slot, and
        every key in it.
        """
        self._make = make_worker
        self._grace = grace
        self._prepare = prepare
        self._cleanup = cleanup
        self._slot_of = slot_of
        self._locks: dict[str, _Lock[K, P]] = {}
        self._stops: dict[str, asyncio.Task[None]] = {}
        self._once = itertools.count()
        self._closed = False

    async def hold(
        self, lock: str, owner: str, wants: Mapping[K, Want] | None = None
    ) -> LockWorker[K, P]:
        """
        Hold ``lock`` for ``owner``, claiming ``wants``' slots; return its worker.

        A new worker is prepared, then starts with every slot wanted so far,
        so its first read takes them all at once. A worker still stopping is
        waited for: one lock never has two. Once the registry has stopped,
        nothing is held.
        """
        return await self._hold(lock, owner, wants or {}, prepare=True)

    async def _hold(
        self, lock: str, owner: str, wants: Mapping[K, Want], *, prepare: bool
    ) -> LockWorker[K, P]:
        while (stopping := self._stops.get(lock)) is not None:
            # Not shield: a stop cancelled with stop()'s caller is still done.
            await asyncio.wait({stopping})
        if self._closed:
            raise RuntimeError("the lock registry has stopped")
        if new := (entry := self._locks.get(lock)) is None:
            worker = self._make(lock)
            entry = self._locks[lock] = _Lock(worker)
            inner = worker.on_change

            def _on_change() -> None:
                try:
                    inner()
                finally:
                    # The worker logs what ``inner`` raises; stopping goes on.
                    self._maybe_stop(lock)

            worker.on_change = _on_change
        else:
            _check_free(entry, owner, {self._slot_of(key) for key in wants})
        entry.holders.add(owner)
        for key, value in wants.items():
            entry.owners[self._slot_of(key)] = owner
            entry.worker.want(key, value)
        joined = None
        if prepare and entry.prepared is None:
            entry.prepared = asyncio.create_task(self._prepared(lock, entry.worker))
            if not new:
                # A one-off read's worker, already started: what it asked the
                # lock before the lock was ready went unanswered.
                joined = entry.prepared
        if joined is not None:
            await asyncio.wait({joined})
            if self._locks.get(lock) is entry:
                entry.worker.wake()
                entry.worker.request_sync()
        if new:
            try:
                if entry.prepared is not None:
                    # The first read comes once the lock is ready for it.
                    await asyncio.wait({entry.prepared})
            finally:
                # Started even if this caller was cancelled: others may hold it.
                if self._locks.get(lock) is entry:
                    entry.worker.start()
        return entry.worker

    def worker(self, lock: str) -> LockWorker[K, P] | None:
        """Return ``lock``'s worker, if it has one in service."""
        return None if (entry := self._locks.get(lock)) is None else entry.worker

    def owner(self, lock: str, slot: int) -> str | None:
        """Return the entry that owns ``slot`` on ``lock``, if one does."""
        entry = self._locks.get(lock)
        return None if entry is None else entry.owners.get(slot)

    def want(self, lock: str, owner: str, key: K, value: Want) -> None:
        """Claim ``key``'s slot for ``owner`` and set what ``key`` should hold."""
        entry = self._held(lock, owner)
        slot = self._slot_of(key)
        _check_free(entry, owner, (slot,))
        entry.owners[slot] = owner
        entry.worker.want(key, value)

    def unclaim(self, lock: str, owner: str, slot: int) -> None:
        """Stop managing ``slot``, every key in it, leaving what the lock holds."""
        entry = self._held(lock, owner)
        if entry.owners.get(slot) == owner:
            del entry.owners[slot]
            for key in self._keys(entry, slot):
                entry.worker.forget(key)

    def give_up(self, lock: str, owner: str, slot: int) -> None:
        """
        Clear ``slot``, every key in it, from the lock and stop managing it.

        The clear goes on without an owner, even if ``owner`` unloads, until
        it is done or held; an entry claiming the slot meanwhile replaces it.
        """
        entry = self._held(lock, owner)
        if entry.owners.get(slot) == owner:
            del entry.owners[slot]
            for key in self._keys(entry, slot):
                entry.worker.want(key, None, release=True)

    async def release(self, lock: str, owner: str) -> None:
        """
        Let go of ``lock``: ``owner``'s slots are left as they are on it.

        The last holder out stops the worker, once no clear is still going.
        """
        entry = self._locks.get(lock)
        if entry is None or owner not in entry.holders:
            return
        entry.holders.discard(owner)
        for slot in [s for s, o in entry.owners.items() if o == owner]:
            del entry.owners[slot]
            for key in self._keys(entry, slot):
                entry.worker.forget(key)
        self._maybe_stop(lock)
        if (stopping := self._stops.get(lock)) is not None:
            await asyncio.wait({stopping})

    async def read_once(self, lock: str, slots: Collection[K]) -> dict[K, Reading]:
        """Read ``slots`` in the lock's turn, starting a worker for it if none runs."""
        owner = f"once-{next(self._once)}"
        # Not prepared: a read needs nothing of the lock's setup.
        worker = await self._hold(lock, owner, {}, prepare=False)
        try:
            return await worker.read(slots)
        finally:
            await self.release(lock, owner)

    async def write_once(
        self,
        lock: str,
        key: K,
        value: Want,
        *,
        before: Callable[[LockWorker[K, P]], None] = lambda worker: None,
    ) -> LockWorker[K, P]:
        """
        Put ``value`` under a key whose slot no entry owns, or clear it for None.

        Ownerless and one-shot, like a slot given up: the worker runs on until
        it is in sync, then drops it, or holds it after its tries. Returns the
        worker, whose view of the key says how it went.
        """
        owner = f"once-{next(self._once)}"
        worker = await self._hold(lock, owner, {}, prepare=True)
        try:
            _check_free(self._held(lock, owner), owner, (self._slot_of(key),))
            before(worker)
            worker.want(key, value, release=True)
            if worker.slots[key].hold is not None:
                # Asked again after it was held: the user saying try again.
                worker.resume(key)
        finally:
            await self.release(lock, owner)
        return worker

    async def stop(self) -> None:
        """Stop every worker, clears still going included (Home Assistant stops)."""
        self._closed = True
        for lock in list(self._locks):
            self._retire(lock)
        await asyncio.gather(*self._stops.values())

    def _keys(self, entry: _Lock[K, P], slot: int) -> list[K]:
        """Return every key the worker tracks in ``slot``."""
        return [key for key in entry.worker.slots if self._slot_of(key) == slot]

    def _held(self, lock: str, owner: str) -> _Lock[K, P]:
        entry = self._locks.get(lock)
        if entry is None or owner not in entry.holders:
            raise KeyError(f"{owner} does not hold {lock}")
        return entry

    def _maybe_stop(self, lock: str) -> None:
        entry = self._locks.get(lock)
        if entry is None or entry.holders:
            return
        if any(
            slot.release and slot.hold is None for slot in entry.worker.slots.values()
        ):
            return  # a slot given up is still being cleared
        self._retire(lock)

    def _retire(self, lock: str) -> None:
        """Take ``lock``'s worker out of service and stop it."""
        entry = self._locks.pop(lock)
        self._stops[lock] = asyncio.create_task(self._stop(lock, entry))

    async def _prepared(self, lock: str, worker: LockWorker[K, P]) -> None:
        try:
            await self._prepare(lock, worker)
        except Exception:
            # Its worker still runs: the lock is there, just not ready.
            _LOGGER.exception("Preparing lock %s failed", lock)

    async def _stop(self, lock: str, entry: _Lock[K, P]) -> None:
        try:
            await entry.worker.stop(self._grace)
            if entry.prepared is not None:
                await asyncio.wait({entry.prepared})
            try:
                await self._cleanup(lock, entry.worker)
            except Exception:
                _LOGGER.exception("Cleaning up lock %s failed", lock)
        finally:
            del self._stops[lock]


def _check_free(entry: _Lock[Any, Any], owner: str, slots: Collection[int]) -> None:
    """Raise ``SlotTaken`` if another entry owns any of ``slots``."""
    for slot in slots:
        if (other := entry.owners.get(slot)) not in (None, owner):
            raise SlotTaken(slot, other)
