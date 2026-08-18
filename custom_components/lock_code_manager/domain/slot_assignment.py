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

Users are identified the way :mod:`.names` identifies them: whitespace
normalized, compared case-insensitively. Storing a name in any other form
would let it match on one path and miss on another, and missing means looking
like a different user and being renumbered onto a different credential index.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from homeassistant.const import CONF_NAME

from .names import normalize_name, normalize_slot_names

CONF_SLOT_ASSIGNMENT = "slot_assignment"

_EMPTY_ASSIGNMENT: Mapping[str, int] = MappingProxyType({})


def _identity(name: str) -> str:
    """
    Return the form a user is recognized by.

    Matches ``names.deduplicate`` and ``names.validate_slot_names``, which
    both casefold. Comparing case-sensitively here would let ``Bob`` and
    ``BOB`` hold two credential indices for someone the rest of the system
    treats as one person, and would make a case-only rename read as a
    deletion plus an addition.
    """
    return normalize_name(name).casefold()


@dataclass(frozen=True, slots=True)
class SlotAssignment:
    """The slot number held by each configured user, keyed by name."""

    slots: Mapping[str, int]

    def __post_init__(self) -> None:
        """
        Put the mapping into its canonical form, then freeze it.

        Names are reduced to their identity form and slot numbers coerced to
        ``int``. Doing it here makes both invariants of the TYPE rather than
        of whichever factory happened to build it -- the plain constructor
        previously accepted a string slot number, and a caller reading
        ``entry.data`` directly instead of through :meth:`from_mapping` would
        reintroduce the double-booking that string keys caused.

        Two keys reducing to one identity keeps the LOWER slot. That case
        means the stored bookkeeping was already inconsistent; raising would
        make the entry unloadable, which is worse than picking
        deterministically, and the lower number is the one likelier to
        predate the corruption.
        """
        canonical: dict[str, int] = {}
        for name, slot in self.slots.items():
            key = _identity(name)
            number = int(slot)
            canonical[key] = min(canonical.get(key, number), number)
        object.__setattr__(self, "slots", MappingProxyType(canonical))

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
        return cls(slots=mapping.get(CONF_SLOT_ASSIGNMENT) or {})

    def to_dict(self) -> dict[str, Any]:
        """Return the bookkeeping to persist beside the configuration."""
        return {CONF_SLOT_ASSIGNMENT: dict(self.slots)}

    def slot(self, name: str) -> int | None:
        """Return the slot ``name`` occupies, or None if they hold none."""
        return self.slots.get(_identity(name))

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
        submission, since two users cannot share a name in the result. The
        renamed entry therefore displaces the one sitting on the target.

        A target is only displaced when a source actually surrendered a slot.
        Dropping it unconditionally deleted an uninvolved user whenever the
        map named a source holding nothing -- and made replaying an
        already-applied map erase its own result, so the operation was not
        idempotent.
        """
        moves = {
            _identity(old): _identity(new)
            for old, new in renames.items()
            if _identity(old) in self.slots
        }
        renamed = {
            moves[name]: slot for name, slot in self.slots.items() if name in moves
        }
        kept = {
            name: slot
            for name, slot in self.slots.items()
            if name not in moves and name not in renamed
        }
        moved = {**kept, **renamed}
        if moved == dict(self.slots):
            return self
        return SlotAssignment(slots=moved)

    def assign(
        self,
        names: Iterable[str],
        *,
        start: int = 1,
        unavailable: Collection[int] = (),
    ) -> SlotAssignment:
        """
        Return the assignment covering exactly ``names``.

        Users already holding a slot keep it, so a rename or an unrelated
        edit never renumbers anyone. New users take the lowest free slot at or
        above ``start``, skipping anything in ``unavailable``.

        Those two are not decoration. The entry has a configured start slot
        (``CONF_START_SLOT``), usually chosen because the numbers below it
        hold codes programmed by hand that Lock Code Manager does not manage,
        and ``config_flow._check_common_slots`` refuses ranges overlapping
        another entry on a shared lock. Since the slot IS the credential index
        on most providers, allocating from 1 unconditionally would write a new
        user's code over one of those.

        Returns ``self`` when nothing differs, so callers can use identity to
        decide whether a write is needed.
        """
        wanted = list(dict.fromkeys(_identity(name) for name in names))
        assigned = {name: self.slots[name] for name in wanted if name in self.slots}

        taken = set(assigned.values()) | set(unavailable)
        candidate = start
        for name in wanted:
            if name in assigned:
                continue
            while candidate in taken:
                candidate += 1
            assigned[name] = candidate
            taken.add(candidate)

        if assigned == dict(self.slots):
            return self
        return SlotAssignment(slots=assigned)


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
    return users, SlotAssignment(slots=assignment), renamed
