"""
One worker per lock, shared by every entry that manages it.

Entries hold a lock and claim its slots. A slot has one owner at most; only
its owner sets what it holds. The lock's worker runs while an entry holds
the lock or a slot given up is still being cleared, and is stopped when
neither is true. Pure asyncio: the integration passes in how a worker is
built.

The design: docs/superpowers/specs/2026-10-14-core-registry-adapter.md.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
import itertools

from .slot import Reading, Want
from .worker import LockWorker


class SlotTaken(Exception):
    """Another entry owns the slot."""

    def __init__(self, slot: int, owner: str) -> None:
        """Name the slot and its owner."""
        super().__init__(f"slot {slot} is owned by {owner}")
        self.slot = slot
        self.owner = owner


@dataclass
class _Lock:
    worker: LockWorker
    holders: set[str] = field(default_factory=set)
    # Slot -> the entry that owns it. A slot given up has no owner.
    owners: dict[int, str] = field(default_factory=dict)


class LockRegistry:
    """Lock id -> its one worker; which entries hold it and own which slots."""

    def __init__(
        self, make_worker: Callable[[str], LockWorker], grace: float = 30.0
    ) -> None:
        """Build workers with ``make_worker``; give each ``grace`` to stop."""
        self._make = make_worker
        self._grace = grace
        self._locks: dict[str, _Lock] = {}
        self._stops: dict[str, asyncio.Task[None]] = {}
        self._once = itertools.count()

    async def hold(
        self, lock: str, owner: str, wants: Mapping[int, Want] | None = None
    ) -> LockWorker:
        """
        Hold ``lock`` for ``owner``, claiming ``wants``' slots; return its worker.

        A new worker starts with every slot wanted, so its first read takes
        them all at once. A worker still stopping is waited for: one lock
        never has two.
        """
        wants = wants or {}
        while (stopping := self._stops.get(lock)) is not None:
            await asyncio.shield(stopping)
        entry = self._locks.get(lock)
        if entry is not None:
            _check_free(entry, owner, wants)
        else:
            entry = self._locks[lock] = _Lock(self._make(lock))
            inner = entry.worker.on_change

            def _on_change() -> None:
                inner()
                self._maybe_stop(lock)

            entry.worker.on_change = _on_change
        entry.holders.add(owner)
        for slot, pin in wants.items():
            entry.owners[slot] = owner
            entry.worker.want(slot, pin)
        if not entry.worker.started:
            entry.worker.start()
        return entry.worker

    def want(self, lock: str, owner: str, slot: int, pin: Want) -> None:
        """Claim ``slot`` for ``owner`` and set what it should hold."""
        entry = self._held(lock, owner)
        _check_free(entry, owner, (slot,))
        entry.owners[slot] = owner
        entry.worker.want(slot, pin)

    def unclaim(self, lock: str, owner: str, slot: int) -> None:
        """Stop managing ``slot``, leaving what the lock holds."""
        entry = self._held(lock, owner)
        if entry.owners.get(slot) == owner:
            del entry.owners[slot]
            entry.worker.forget(slot)

    def give_up(self, lock: str, owner: str, slot: int) -> None:
        """
        Clear ``slot`` from the lock and stop managing it.

        The clear goes on without an owner, even if ``owner`` unloads, until
        it is done or held; an entry claiming the slot meanwhile replaces it.
        """
        entry = self._held(lock, owner)
        if entry.owners.get(slot) == owner:
            del entry.owners[slot]
            entry.worker.want(slot, None, release=True)

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
            entry.worker.forget(slot)
        self._maybe_stop(lock)
        if (stopping := self._stops.get(lock)) is not None:
            await asyncio.shield(stopping)

    async def read_once(self, lock: str, slots: Collection[int]) -> dict[int, Reading]:
        """Read ``slots`` in the lock's turn, starting a worker for it if none runs."""
        owner = f"read-once-{next(self._once)}"
        worker = await self.hold(lock, owner)
        try:
            return await worker.read(slots)
        finally:
            await self.release(lock, owner)

    async def stop(self) -> None:
        """Stop every worker, clears still going included (Home Assistant stops)."""
        for lock, entry in list(self._locks.items()):
            del self._locks[lock]
            self._stops[lock] = asyncio.create_task(self._stop(lock, entry.worker))
        await asyncio.gather(*self._stops.values())

    def _held(self, lock: str, owner: str) -> _Lock:
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
        del self._locks[lock]
        self._stops[lock] = asyncio.create_task(self._stop(lock, entry.worker))

    async def _stop(self, lock: str, worker: LockWorker) -> None:
        try:
            await worker.stop(self._grace)
        finally:
            del self._stops[lock]


def _check_free(entry: _Lock, owner: str, slots: Collection[int]) -> None:
    """Raise ``SlotTaken`` if another entry owns any of ``slots``."""
    for slot in slots:
        if (other := entry.owners.get(slot)) not in (None, owner):
            raise SlotTaken(slot, other)
