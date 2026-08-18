"""
Which slot number each user occupies.

The slot number stops being configuration in version 3 and becomes internal
bookkeeping. It still exists, because on most providers it IS the lock's
credential index -- ``credential_index_follows_slot`` -- so it is bounded by
the lock's advertised capacity and must be reusable when a user is deleted.

That boundedness is why numbers are reused rather than issued monotonically:
one that is never reused would eventually exceed every lock's capacity.
Deleting a user and creating another does hand the newcomer the departed
user's slot, entity identifiers, and history, which matches what the physical
credential slot is doing.

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
        ``int``. Doing it here makes both invariants of the TYPE rather than of
        whichever factory built it, so a caller constructing directly from
        stored data cannot introduce a string key that compares unequal to an
        int one.

        Two keys reducing to one identity keeps the LOWER slot. That case
        means the stored bookkeeping was already inconsistent; raising would
        make the entry unloadable, which is worse than picking
        deterministically, and the lower number is the one likelier to
        predate the corruption.
        """
        canonical: dict[str, int] = {}
        for name, slot in self.slots.items():
            try:
                number = int(slot)
            except TypeError, ValueError:
                # Unusable stored value. Dropping costs this user their slot,
                # which reconcile then reissues; raising would take the whole
                # entry down, and corrupt bookkeeping is exactly what the
                # coercion above exists to survive.
                continue
            key = _identity(str(name))
            canonical[key] = min(canonical.get(key, number), number)

        # No two users on one number, as an invariant of the TYPE. Reachable
        # only from bookkeeping that was already inconsistent, but both users
        # would otherwise write over each other on the lock every sync, and
        # nothing else in the system repairs it.
        #
        # The loser is DROPPED rather than renumbered here, because this has
        # no idea what the entry's start slot is -- reissuing from 1 could put
        # them below it, on a code programmed by hand. reconcile knows, and
        # gives them a number that respects it.
        deduped: dict[str, int] = {}
        held: set[int] = set()
        for key in sorted(canonical):
            number = canonical[key]
            if number in held:
                continue
            held.add(number)
            deduped[key] = number
        object.__setattr__(self, "slots", MappingProxyType(deduped))

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
        stored = mapping.get(CONF_SLOT_ASSIGNMENT)
        # Anything that is not a mapping carries nothing worth keeping, and
        # reconcile rebuilds the assignment from the configured users anyway.
        # Raising here would abort entry setup over hand-edited storage, which
        # is the same threat model the coercions below exist for.
        return cls(slots=stored if isinstance(stored, Mapping) else {})

    def to_dict(self) -> dict[str, Any]:
        """Return the bookkeeping to persist beside the configuration."""
        return {CONF_SLOT_ASSIGNMENT: dict(self.slots)}

    def slot(self, name: str) -> int | None:
        """Return the slot ``name`` occupies, or None if they hold none."""
        return self.slots.get(_identity(name))

    def reconcile(
        self,
        names: Iterable[str],
        *,
        start: int,
        renames: Mapping[str, str] | None = None,
        unavailable: Collection[int] = (),
    ) -> SlotAssignment:
        """
        Return the assignment covering exactly ``names``, applying ``renames``.

        ONE operation rather than a rename step followed by an allocation
        step. Splitting them put the order in the caller's hands, and the
        order is the whole difficulty: a rename is indistinguishable from a
        deletion plus an addition unless you already know who survives.
        Getting that sequence wrong produced a high-severity defect in three
        consecutive review rounds -- users deleted, users renumbered onto
        another user's credential index -- so the sequence is gone rather
        than documented.

        ``names`` is the full set of users in the NEW configuration, which is
        what resolves the ambiguity: a rename target that is not in ``names``
        is departing, and one that is names the renamed user.

        Users keep their slot through a rename and through anyone else's
        arrival or departure. New users take the lowest free number at or
        above ``start``, skipping ``unavailable``.

        ``start`` is required, not defaulted. The entry's configured start
        slot (``CONF_START_SLOT``) is usually chosen because the numbers below
        it hold codes programmed by hand that Lock Code Manager does not
        manage, and ``config_flow._check_common_slots`` refuses ranges that
        overlap another entry on a shared lock. Since the slot IS the
        credential index on most providers, defaulting to 1 would make a
        forgotten argument write a new user's code over one of those --
        silently, and on a real door. Required makes it a type error.

        A user already holding a slot keeps it even when ``start`` rises
        above it or ``unavailable`` comes to include it: both constrain
        ALLOCATION, not tenure. Moving somebody rewrites their credential on
        every lock and orphans their entities, which is worse than the entry
        holding a number it would not choose today.

        That trade is clean for ``start`` and NOT clean for ``unavailable``.
        A tenured user on a number another entry owns means two entries
        writing one credential index, which nothing here can repair -- this
        type cannot see the other entry. It is left standing because the
        alternative is renumbering somebody without being asked, and because
        ``config_flow._check_common_slots`` is what prevents the overlap
        arising in the first place. If a consumer ever needs the conflict
        surfaced rather than absorbed, that belongs at the seam that knows
        about other entries, not here.

        Returns ``self`` when nothing differs, so callers can use identity to
        decide whether a write is needed.
        """
        wanted = list(dict.fromkeys(_identity(name) for name in names))
        surviving = set(wanted)

        # A move is honoured only when the source holds a slot AND the target
        # is among the survivors; a rename whose target is absent contradicts
        # the name set and is ignored.
        #
        # Two sources renaming onto one target is likewise contradictory.
        # Resolved in sorted order so the same input always gives the same
        # answer regardless of mapping iteration order.
        # Reduce to identity form FIRST. Iterating the raw mapping let two
        # keys that mean the same user (``Alice`` and ``alice ``) each take a
        # turn: the later overwrote the earlier while the earlier's target
        # stayed claimed, so a third user's legitimate rename onto that target
        # was refused and they were renumbered. It also made the answer depend
        # on the mapping's insertion order.
        wanted_moves: dict[str, str] = {}
        for old in sorted(renames or {}, key=lambda key: (_identity(key), str(key))):
            wanted_moves.setdefault(_identity(old), _identity((renames or {})[old]))

        honoured: dict[str, str] = {}
        claimed: set[str] = set()
        for source in sorted(wanted_moves):
            target = wanted_moves[source]
            if source not in self.slots or target not in surviving:
                continue
            if target in claimed:
                continue
            honoured[source] = target
            claimed.add(target)

        renamed = {
            honoured[name]: slot
            for name, slot in self.slots.items()
            if name in honoured
        }
        kept = {
            name: slot
            for name, slot in self.slots.items()
            if name not in honoured and name in surviving and name not in renamed
        }

        carried = {**kept, **renamed}
        taken = set(carried.values()) | set(unavailable)
        candidate = start
        # Sorted so the answer does not depend on how the caller ordered
        # ``names``. The signature accepts any iterable, and a set or
        # ``dict.keys()`` from an unordered source would otherwise give the
        # same configuration different credential indices from run to run.
        for name in sorted(name for name in wanted if name not in carried):
            while candidate in taken:
                candidate += 1
            carried[name] = candidate
            taken.add(candidate)

        if carried == dict(self.slots):
            return self
        return SlotAssignment(slots=carried)


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
