"""
Which slot number each user occupies.

The number is internal bookkeeping, not configuration, but on most providers
it IS the lock's credential index (``credential_index_follows_slot``). That
bounds it by the lock's advertised capacity, which is why numbers are reused
on deletion rather than issued monotonically -- monotonic ones would
eventually exceed every lock.

Reuse hands a newcomer the departed user's number, entity identifiers and
history, matching what the physical credential slot does.

Entity and device identifiers key on this number, so a rename moves nothing
in either registry.

Users are identified as :mod:`.names` identifies them: whitespace normalized,
compared case-insensitively. Any other stored form can match on one path and
miss on another, and a miss reads as a different user and renumbers them onto
a different credential index.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from homeassistant.const import CONF_NAME, CONF_PIN

from .names import identity, normalize_name, normalize_slot_names

CONF_SLOT_ASSIGNMENT = "slot_assignment"


@dataclass(frozen=True, slots=True)
class SlotAssignment:
    """The slot number held by each configured user, keyed by name."""

    slots: Mapping[str, int]

    def __post_init__(self) -> None:
        """
        Put the mapping into its canonical form, then freeze it.

        Canonicalizing here makes identity-form names and ``int`` numbers
        invariants of the type rather than of whichever factory built it, so
        constructing directly from stored data cannot introduce a string key
        that compares unequal to an int one.

        Two keys reducing to one identity keep the LOWER number: the stored
        bookkeeping was already inconsistent, and picking deterministically
        beats making the entry unloadable.
        """
        canonical: dict[str, int] = {}
        for name, slot in self.slots.items():
            try:
                number = int(slot)
            except TypeError, ValueError:
                # Dropping costs this user their number, which reconcile
                # reissues; raising would take the whole entry down.
                continue
            key = identity(str(name))
            canonical[key] = min(canonical.get(key, number), number)

        # No two users on one number: they would otherwise overwrite each
        # other on the lock every sync, and nothing else repairs it.
        #
        # The loser is dropped rather than renumbered because this type cannot
        # see the entry's start slot, so reissuing from 1 could land on a code
        # programmed by hand. reconcile knows the start and respects it.
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
        return cls(slots={})

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, Any]) -> SlotAssignment:
        """
        Read the assignment out of an entry's stored bookkeeping.

        Takes the already-merged mapping rather than an entry so there is no
        second options-before-data precedence rule to drift from the one in
        ``EntryConfig.from_entry``. The assignment has to come from the same
        side the users did, or a stale copy renumbers everyone on next start.
        """
        stored = mapping.get(CONF_SLOT_ASSIGNMENT)
        # Non-mapping storage carries nothing worth keeping and reconcile
        # rebuilds from the configured users; raising would abort setup.
        return cls(slots=stored if isinstance(stored, Mapping) else {})

    def to_dict(self) -> dict[str, Any]:
        """Return the bookkeeping to persist beside the configuration."""
        return {CONF_SLOT_ASSIGNMENT: dict(self.slots)}

    def slot(self, name: str) -> int | None:
        """Return the slot ``name`` occupies, or None if they hold none."""
        return self.slots.get(identity(name))

    def reconcile(
        self,
        names: Iterable[str],
        *,
        start: int,
        unavailable: Collection[int] = (),
    ) -> SlotAssignment:
        """
        Return the assignment covering exactly ``names``.

        Users keep their number through anyone else's arrival or departure.
        New users take the lowest free number at or above ``start``, skipping
        ``unavailable``. ``start`` is required so a caller decides where
        issuing begins rather than inheriting a default.

        Tenure outranks both constraints: a user already holding a number
        keeps it even when ``start`` rises above it or ``unavailable`` grows
        to include it, because moving somebody rewrites their credential on
        every lock and orphans their entities.

        For ``unavailable`` that leaves a real conflict standing -- two
        entries writing one credential index -- which this type cannot repair
        because it cannot see the other entry. ``config_flow`` prevents the
        overlap arising; surfacing it instead belongs at that seam.

        A rename is NOT handled here. It arrives as a name that is present
        and one that is gone, which is indistinguishable from a deletion plus
        an addition, so this would renumber somebody. Renaming re-keys the
        assignment directly, in ``EntryConfig.with_user_renamed``, where the
        old name and the new one are both known.

        Returns ``self`` when nothing differs, so callers can use identity to
        decide whether a write is needed.
        """
        wanted = list(dict.fromkeys(identity(name) for name in names))
        surviving = set(wanted)

        carried = {name: slot for name, slot in self.slots.items() if name in surviving}
        taken = set(carried.values()) | set(unavailable)
        candidate = start
        # Sorted because the signature accepts any iterable: an unordered one
        # would give the same configuration different credential indices from
        # run to run.
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
) -> tuple[dict[str, dict[str, Any]], SlotAssignment, list[str], list[str]]:
    """
    Convert slot-keyed configuration into name-keyed users plus an assignment.

    Pure, so the properties that matter -- nothing lost that was ever a
    user, nobody renumbered -- can be stated over it directly rather than
    through a config entry. Also returns the slots whose name the repair
    changed, and the empty ones dropped, for the migration to report.

    Repairs names itself rather than requiring callers to. The name becomes
    the mapping key here, so a duplicate would collapse two slots into one
    user and a missing one would raise mid-migration -- both reachable from
    stored data, and a migration with no rollback cannot rely on a
    precondition.

    Slot keys are coerced to ``int`` because the on-disk JSON form is ``str``,
    and a string surviving into the assignment misses ``candidate in taken``
    and issues an already-occupied number.
    """
    # A slot with neither a name nor a PIN is not a person: it is an empty
    # position left over from choosing how many slots to set up. Naming it
    # would invent a user who never existed and hold a slot number against
    # them, so it is dropped instead. Anything with a PIN survives, named
    # after its number if it has to be -- there is a credential on the lock
    # and somebody must own it.
    populated = {
        slot_num: slot
        for slot_num, slot in slots.items()
        if (slot or {}).get(CONF_PIN) or normalize_name((slot or {}).get(CONF_NAME))
    }
    dropped = [
        str(slot_num) for slot_num in sorted(set(slots) - set(populated), key=int)
    ]

    repaired, renamed = normalize_slot_names(populated)
    users: dict[str, dict[str, Any]] = {}
    assignment: dict[str, int] = {}
    for raw_slot_num, slot in sorted(repaired.items(), key=lambda kv: int(kv[0])):
        name = slot[CONF_NAME]
        users[name] = {k: v for k, v in slot.items() if k != CONF_NAME}
        assignment[name] = int(raw_slot_num)
    return users, SlotAssignment(slots=assignment), renamed, dropped
