"""
What the lock workers remember across a restart, in one Home Assistant Store.

Per lock, keyed by its entity registry id so a rename keeps it (#1442), with
its entity id beside it: whether the lock has ever answered, and per slot
what was last written -- a keyed fingerprint or ``cleared``, never a PIN --
and a release clear still owed, with the entry that gave the slot up. What a
slot holds is read again; counts, fights and holds start over (a hold's
repair outlives a restart on its own).

Written a second after a change, and at Home Assistant's final write. Never
dropped on an unload (#1243): only when a release is done, a slot is let go
of for good, a lock leaves Lock Code Manager or an entry is deleted.

The design: docs/superpowers/specs/2026-10-14-milestone-5.md.
"""

from __future__ import annotations

from collections.abc import Callable, Collection
from typing import Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from ..const import DOMAIN
from ..core.worker import LockWorker

STORE_KEY = f"{DOMAIN}.core"
STORE_VERSION = 1
SAVE_DELAY = 1.0

Record = dict[str, Any]


class LockStore:
    """Each lock's record: whether it ever answered, and its slots'."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Start empty; ``async_load`` reads what was saved."""
        self._hass = hass
        self._store: Store[dict[str, Any]] = Store(hass, STORE_VERSION, STORE_KEY)
        self._locks: dict[str, Record] = {}

    async def async_load(self) -> None:
        """Read what was saved, each record under its lock's current entity id."""
        saved = await self._store.async_load() or {}
        self._locks = saved.get("locks", {})
        registry = er.async_get(self._hass)
        for key, record in self._locks.items():
            if (entity := registry.async_get(key)) is not None:
                record["entity_id"] = entity.entity_id

    # What a lock's worker starts from ------------------------------------------

    def wrote(self, lock: str, slot: int) -> str | None:
        """Return what was last written to ``slot`` on ``lock``, if anything."""
        record = self._find(lock)
        return (
            None if record is None else record["slots"].get(str(slot), {}).get("wrote")
        )

    def answered(self, lock: str) -> bool:
        """Return whether ``lock`` has ever answered."""
        record = self._find(lock)
        return record is not None and record["answered"]

    def releases(self) -> list[tuple[str, int, str]]:
        """Return each release clear still owed: lock, slot, the entry that gave it up."""
        return [
            (record["entity_id"], int(slot), saved["release"])
            for record in self._locks.values()
            for slot, saved in record["slots"].items()
            if "release" in saved
        ]

    # Keeping it current --------------------------------------------------------

    @callback
    def remember(
        self, lock: str, worker: LockWorker, given_up_by: Callable[[int], str | None]
    ) -> None:
        """
        Take what ``worker`` knows of ``lock`` now; slots it no longer tracks keep theirs.

        A release is kept while it is still going, not once held: its repair
        brings it back. ``given_up_by`` names the entry that gave a slot up.
        """
        if (key := self._key(lock)) is None:
            return  # gone from the entity registry: the lock is gone
        record = self._locks.get(key, {"answered": False, "slots": {}})
        slots = dict(record["slots"])
        for number, slot in worker.slots.items():
            saved: dict[str, str] = {}
            if slot.wrote is not None:
                saved["wrote"] = slot.wrote
            if slot.release and slot.hold is None and (by := given_up_by(number)):
                saved["release"] = by
            if saved:
                slots[str(number)] = saved
            else:
                slots.pop(str(number), None)
        self._put(
            key,
            {
                "entity_id": lock,
                "answered": worker.answered_ever,
                "slots": slots,
            },
        )

    @callback
    def forget_slot(self, lock: str, slot: int) -> None:
        """Forget ``slot`` on ``lock``: it is done, or let go of for good."""
        if (key := self._key_of(lock)) is not None:
            record = self._locks[key]
            slots = {n: s for n, s in record["slots"].items() if n != str(slot)}
            self._put(key, {**record, "slots": slots})

    @callback
    def forget_lock(self, lock: str) -> None:
        """Forget ``lock``: it left Lock Code Manager."""
        if (key := self._key_of(lock)) is not None:
            del self._locks[key]
            self._save()

    @callback
    def forget_entry(self, locks: Collection[str], slots: Collection[int]) -> None:
        """
        Forget a deleted entry's ``slots`` on its ``locks``.

        A clear it left going is on a slot it no longer has: it is kept.
        """
        for lock in locks:
            if (key := self._key_of(lock)) is not None:
                record = self._locks[key]
                kept = {n: s for n, s in record["slots"].items() if int(n) not in slots}
                self._put(key, {**record, "slots": kept})

    @callback
    def prune(self, keep: Collection[str]) -> list[Record]:
        """
        Forget every lock not in ``keep``, or gone; return the records forgotten.

        A record kept under a registry id the registry no longer has was for
        a lock that is gone, even if another now has its entity id.
        """
        kept = {
            k: r
            for k, r in self._locks.items()
            if r["entity_id"] in keep and k == self._key(r["entity_id"])
        }
        dropped = [r for k, r in self._locks.items() if k not in kept]
        if dropped:
            self._locks = kept
            self._save()
        return dropped

    # Inside --------------------------------------------------------------------

    def _key(self, lock: str) -> str | None:
        """
        Return the key ``lock`` is kept under: its registry id.

        Every lock Lock Code Manager drives has one; one without is gone.
        """
        entity = er.async_get(self._hass).async_get(lock)
        return None if entity is None else entity.id

    def _key_of(self, lock: str) -> str | None:
        """Return the key ``lock``'s record is kept under, if it has one."""
        return key if (key := self._key(lock)) in self._locks else None

    def _find(self, lock: str) -> Record | None:
        return None if (key := self._key_of(lock)) is None else self._locks[key]

    def _put(self, key: str, record: Record) -> None:
        if self._locks.get(key) != record:
            self._locks[key] = record
            self._save()

    def _save(self) -> None:
        self._store.async_delay_save(lambda: {"locks": self._locks}, SAVE_DELAY)
