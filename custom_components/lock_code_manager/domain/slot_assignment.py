"""
Which slot number each user occupies.

The slot number stops being configuration in version 3 and becomes internal
bookkeeping. It still exists, because on most providers it IS the lock's
credential index -- ``credential_index_follows_slot`` -- so it is bounded by
the lock's advertised capacity and must be reusable when a user is deleted.

That boundedness is why this is an assignment rather than a handle allocator:
a number that can never be reused would eventually exceed every lock's
capacity. Reuse is correct here. Deleting a user and creating another does
hand the newcomer the departed user's slot, entity identifiers, and history
-- exactly as it does today, because the physical credential slot is genuinely
being reused.

Entity and device identifiers keep keying on this number, so a rename moves
nothing in either registry.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from homeassistant.const import CONF_NAME

from .names import normalize_slot_names

CONF_SLOT_ASSIGNMENT = "slot_assignment"

_EMPTY_ASSIGNMENT: Mapping[str, int] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class SlotAssignment:
    """The slot number held by each configured user, keyed by name."""

    slots: Mapping[str, int]

    @classmethod
    def empty(cls) -> SlotAssignment:
        """Return the assignment for an entry with no users."""
        return cls(slots=_EMPTY_ASSIGNMENT)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> SlotAssignment:
        """Read the assignment out of an entry's stored bookkeeping."""
        raw = mapping.get(CONF_SLOT_ASSIGNMENT) or {}
        return cls(slots=MappingProxyType({str(k): int(v) for k, v in raw.items()}))

    def to_dict(self) -> dict[str, Any]:
        """Return the bookkeeping to persist beside the configuration."""
        return {CONF_SLOT_ASSIGNMENT: dict(self.slots)}

    def slot(self, name: str) -> int | None:
        """Return the slot ``name`` occupies, or None if they hold none."""
        return self.slots.get(name)

    def with_renames(self, renames: Mapping[str, str]) -> SlotAssignment:
        """
        Re-key the assignment so renamed users keep their slot.

        Must be applied BEFORE :meth:`assign`, which cannot tell a rename from
        a deletion plus an addition: it would free the old name's slot and
        hand it out in iteration order. Two renames in one submission then
        land each user on the other's index, rewriting both credentials on
        every lock -- the failure this whole design exists to avoid, arriving
        through the one operation that is supposed to be free.

        Built as a fresh mapping rather than moved key by key, so a swap
        (``A -> B`` and ``B -> A``) resolves without needing an order.
        """
        if not renames:
            return self
        return SlotAssignment(
            slots=MappingProxyType(
                {renames.get(name, name): slot for name, slot in self.slots.items()}
            )
        )

    def assign(self, names: Iterable[str]) -> SlotAssignment:
        """
        Return the assignment covering exactly ``names``.

        Users already holding a slot keep it, so a rename or an unrelated
        edit never renumbers anyone. New users take the lowest free slot,
        which is what keeps the numbers inside a lock's capacity -- counting
        upward from the highest ever issued would drift past it.

        Returns ``self`` when nothing differs, so callers can use identity to
        decide whether a write is needed.
        """
        wanted = list(dict.fromkeys(names))
        assigned = {name: self.slots[name] for name in wanted if name in self.slots}

        taken = set(assigned.values())
        candidate = 1
        for name in wanted:
            if name in assigned:
                continue
            while candidate in taken:
                candidate += 1
            assigned[name] = candidate
            taken.add(candidate)

        if assigned == dict(self.slots):
            return self
        return SlotAssignment(slots=MappingProxyType(assigned))


def users_from_slots(
    slots: Mapping[Any, Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], SlotAssignment, list[str]]:
    """
    Convert slot-keyed configuration into name-keyed users plus an assignment.

    The version 3 migration in one function, kept pure so the properties that
    matter -- nothing lost, nobody renumbered -- can be stated over it
    directly rather than through a config entry. Also returns the slots whose
    name the repair changed, so the migration can report them.

    Repairs the names ITSELF rather than documenting that callers must. The
    name becomes the mapping key here, so two slots sharing one would collapse
    into a single user -- losing a user and their code, and renumbering the
    survivor -- and a slot with no name at all would raise mid-migration. Both
    are reachable from a version 2 entry, where the name is optional. A
    precondition is not good enough on a migration with no rollback.

    Slot keys are coerced to ``int``. The on-disk JSON form is ``str``, so a
    migration reading stored data hands strings straight in; a string here
    survives into the assignment, where ``candidate in taken`` compares an int
    against it, misses, and issues a slot that is already occupied.
    """
    repaired, renamed = normalize_slot_names(slots)
    users: dict[str, dict[str, Any]] = {}
    assignment: dict[str, int] = {}
    for raw_slot_num, slot in sorted(repaired.items(), key=lambda kv: int(kv[0])):
        name = slot[CONF_NAME]
        users[name] = {k: v for k, v in slot.items() if k != CONF_NAME}
        assignment[name] = int(raw_slot_num)
    return users, SlotAssignment(slots=MappingProxyType(assignment)), renamed
