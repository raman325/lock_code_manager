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

from .names import normalize_name, normalize_slot_names

CONF_SLOT_ASSIGNMENT = "slot_assignment"

_EMPTY_ASSIGNMENT: Mapping[str, int] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class SlotAssignment:
    """The slot number held by each configured user, keyed by name."""

    slots: Mapping[str, int]

    def __post_init__(self) -> None:
        """
        Normalize the names and freeze the mapping.

        Normalizing here rather than at each method makes it an invariant of
        the type: a name can only ever be stored in one form, so it cannot
        match on one path and miss on another. Missing means looking like a
        different user and being renumbered, which moves that user's
        credential on every lock. Stored data predating this is normalized on
        read for the same reason.

        Freezing closes the one hole the factories leave: the plain
        constructor accepted a live dict, which a caller could then mutate
        under the identity check ``assign`` uses to decide whether to write.
        """
        object.__setattr__(
            self,
            "slots",
            MappingProxyType(
                {normalize_name(name): slot for name, slot in self.slots.items()}
            ),
        )

    def __hash__(self) -> int:
        """
        Hash by content.

        ``frozen=True`` advertises hashability, but the generated ``__hash__``
        hashes the mapping field and raises. Sorting to a tuple keeps the
        promise the decorator makes instead of leaving a runtime trap.
        """
        return hash(tuple(sorted(self.slots.items())))

    @classmethod
    def empty(cls) -> SlotAssignment:
        """Return the assignment for an entry with no users."""
        return cls(slots=_EMPTY_ASSIGNMENT)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> SlotAssignment:
        """
        Read the assignment out of an entry's stored bookkeeping.

        Takes ONE mapping deliberately. Configuration is read options-first
        and falls back to data (``EntryConfig.from_entry``), but the
        assignment must be read from the same side the users came from, or a
        stale copy renumbers everyone on the next start. Consumers pass the
        already-merged mapping rather than an entry, so there is no second
        precedence rule here to get out of step with the first.
        """
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

        A rename target may be a name somebody else still holds, and that is
        legal rather than a conflict: the holder must be departing in the same
        submission, since two users cannot share a name in the result. So the
        renamed entry WINS and the entry sitting on the target is dropped.
        Renaming into a target with a plain re-key instead lets whichever the
        mapping happens to iterate last overwrite the other -- which for the
        chain ``A -> B``, ``B -> C`` with ``C`` deleted moved the user
        formerly named B from index 2 to index 3, and gave a different answer
        for a different insertion order.

        The two halves are built over disjoint key sets, so the result does
        not depend on iteration order for chains any more than for swaps.
        """
        renames = {
            normalize_name(old): normalize_name(new) for old, new in renames.items()
        }
        targets = set(renames.values())
        renamed = {
            renames[name]: slot for name, slot in self.slots.items() if name in renames
        }
        kept = {
            name: slot
            for name, slot in self.slots.items()
            if name not in renames and name not in targets
        }
        moved = {**kept, **renamed}
        if moved == dict(self.slots):
            return self
        return SlotAssignment(slots=MappingProxyType(moved))

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
        # Normalized here, not assumed. A name reaching this boundary in its
        # raw form -- one caller storing the repaired name while another
        # passes what the user typed -- would look like a different user and
        # renumber them, moving their credential on every lock.
        wanted = list(dict.fromkeys(normalize_name(name) for name in names))
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
