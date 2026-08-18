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
    slots: Mapping[int, Mapping[str, Any]],
) -> tuple[dict[str, dict[str, Any]], SlotAssignment]:
    """
    Convert slot-keyed configuration into name-keyed users plus an assignment.

    The version 3 migration in one function, kept pure so the properties that
    matter -- nothing lost, nobody renumbered -- can be stated over it
    directly rather than through a config entry.

    The name becomes the key and stops being a field, so a duplicate name is
    no longer something to validate and reject: it cannot be represented.
    Callers must have run the name repair first, since two slots sharing a
    name would silently collapse into one user here.
    """
    users: dict[str, dict[str, Any]] = {}
    assignment: dict[str, int] = {}
    for slot_num, slot in sorted(slots.items()):
        name = slot[CONF_NAME]
        users[name] = {k: v for k, v in slot.items() if k != CONF_NAME}
        assignment[name] = slot_num
    return users, SlotAssignment(slots=MappingProxyType(assignment))
