"""
What the lock workers remember across a restart, in one Home Assistant Store.

Per lock, keyed by the entity registry id its worker is bound to, so a rename
keeps it (#1442) and a device re-paired under the same entity id never finds
the old one's, with its entity id beside it: whether the lock has ever
answered, whether a read of it has ever described a slot (#1581; absent in a
record saved before, and then not known to), and per slot what was last
written for each credential type -- a keyed fingerprint or ``cleared``, never
the value -- and a release clear still owed, with the entry that gave the slot
up. What a slot holds is read again; counts, fights and holds start over (a
hold's repair outlives a restart on its own).

Written a second after a change, and at Home Assistant's final write. Never
dropped on an unload (#1243): only when a release is done, a slot is let go
of for good, a lock leaves Lock Code Manager or an entry is deleted. A
record whose registry id is gone goes at the next start.
"""

from collections import defaultdict
from collections.abc import Callable, Collection
from typing import Any, override

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store

from ..const import DOMAIN
from ..model.credentials import CredentialAddress, CredentialType, pin_address
from ..sync.slot import CLEARED, Slot
from ..sync.worker import LockWorker

STORE_KEY = f"{DOMAIN}.core"
STORE_VERSION = 2
SAVE_DELAY = 1.0

Record = dict[str, Any]


class _Store(Store[dict[str, Any]]):
    """The core's store, moving older records to the current shape as it loads."""

    @override
    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any]:
        """Version 1 kept one fingerprint per slot: a PIN's."""
        locks = old_data.get("locks", {})
        if old_major_version < 2:
            locks = {
                key: {
                    **record,
                    "slots": {
                        number: _per_type(saved)
                        for number, saved in record["slots"].items()
                    },
                }
                for key, record in locks.items()
            }
        return {**old_data, "locks": locks}


def _per_type(saved: dict[str, Any]) -> dict[str, Any]:
    """Return a version 1 slot record with what was written kept under PIN."""
    if "wrote" not in saved:
        return saved
    return {**saved, "wrote": {CredentialType.PIN.value: saved["wrote"]}}


class LockStore:
    """Each lock's record, under ``key``: the registry id its worker is bound to."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Start empty; ``async_load`` reads what was saved."""
        self._hass = hass
        self._store: Store[dict[str, Any]] = _Store(hass, STORE_VERSION, STORE_KEY)
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

    def wrote(self, key: str, address: CredentialAddress) -> str | None:
        """Return what was last written at ``address`` on the lock, if anything."""
        record = self._locks.get(key)
        if record is None:
            return None
        saved = record["slots"].get(str(address.user_ref), {})
        return saved.get("wrote", {}).get(address.credential_type.value)

    def wrote_pin(self, key: str, slot: int) -> bool:
        """
        Return whether a PIN was what was last written to ``slot`` on the lock.

        A slot only ever cleared never held Lock Code Manager's code.
        """
        return self.wrote(key, pin_address(slot)) not in (None, CLEARED)

    def answered(self, key: str) -> bool:
        """Return whether the lock has ever answered."""
        record = self._locks.get(key)
        return record is not None and record["answered"]

    def reports(self, key: str) -> bool:
        """Return whether a read of the lock has ever described a slot."""
        record = self._locks.get(key)
        return record is not None and record.get("reports", False)

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
        self,
        key: str,
        lock: str,
        worker: LockWorker[CredentialAddress, Any],
        given_up_by: Callable[[int], str | None],
    ) -> None:
        """
        Take what ``worker`` knows of ``lock`` now; what it no longer tracks keeps its own.

        Per slot, each credential type the worker tracks is written over; a
        type it does not track keeps what was last written for it. A release
        is kept while it is still going, not once held: its repair brings it
        back. ``given_up_by`` names the entry that gave a slot up.
        """
        record = self._locks.get(key, {"answered": False, "slots": {}})
        slots = dict(record["slots"])
        tracked: defaultdict[int, list[tuple[CredentialAddress, Slot]]] = defaultdict(
            list
        )
        for address, slot in worker.slots.items():
            tracked[address.user_ref].append((address, slot))
        for number, held in tracked.items():
            wrote = dict(slots.get(str(number), {}).get("wrote", {}))
            for address, slot in held:
                if slot.wrote is None:
                    wrote.pop(address.credential_type.value, None)
                else:
                    wrote[address.credential_type.value] = slot.wrote
            saved: dict[str, Any] = {"wrote": wrote} if wrote else {}
            if any(slot.release and slot.hold is None for _, slot in held) and (
                by := given_up_by(number)
            ):
                saved["release"] = by
            if saved:
                slots[str(number)] = saved
            else:
                slots.pop(str(number), None)
        self._put(
            key,
            {
                **record,
                "entity_id": lock,
                "answered": worker.answered_ever,
                "slots": slots,
            },
        )

    @callback
    def reported(self, key: str, lock: str) -> None:
        """Remember that a read of ``lock`` has described a slot."""
        record = self._locks.get(key, {"answered": False, "slots": {}})
        self._put(key, {**record, "entity_id": lock, "reports": True})

    @callback
    def unreported(self, key: str) -> None:
        """Forget that a read of the lock has described a slot: it is asked again."""
        if (record := self._locks.get(key)) is not None:
            self._put(key, {k: v for k, v in record.items() if k != "reports"})

    @callback
    def forget_slot(self, key: str, slot: int) -> None:
        """Forget ``slot`` on the lock: it is done, or let go of for good."""
        if (record := self._locks.get(key)) is not None:
            slots = {n: s for n, s in record["slots"].items() if n != str(slot)}
            self._put(key, {**record, "slots": slots})

    @callback
    def forget_lock(self, key: str) -> None:
        """Forget the lock: it left Lock Code Manager, or its device is gone."""
        if self._locks.pop(key, None) is not None:
            self._save()

    @callback
    def forget_entry(self, keys: Collection[str], slots: Collection[int]) -> None:
        """
        Forget a deleted entry's ``slots`` on the locks kept under ``keys``.

        A clear it left going is on a slot it no longer has: it is kept.
        """
        for key in keys:
            if (record := self._locks.get(key)) is not None:
                kept = {n: s for n, s in record["slots"].items() if int(n) not in slots}
                self._put(key, {**record, "slots": kept})

    @callback
    def prune(self, keep: Collection[str]) -> list[Record]:
        """
        Forget every lock not in ``keep``, or gone; return the records forgotten.

        A record kept under a registry id the registry no longer has was for
        a lock that is gone, even if another now has its entity id.
        """
        registry = er.async_get(self._hass)
        kept = {
            k: r
            for k, r in self._locks.items()
            if r["entity_id"] in keep and registry.async_get(k) is not None
        }
        dropped = [r for k, r in self._locks.items() if k not in kept]
        if dropped:
            self._locks = kept
            self._save()
        return dropped

    # Inside --------------------------------------------------------------------

    def _put(self, key: str, record: Record) -> None:
        if self._locks.get(key) != record:
            self._locks[key] = record
            self._save()

    def _save(self) -> None:
        self._store.async_delay_save(lambda: {"locks": self._locks}, SAVE_DELAY)
