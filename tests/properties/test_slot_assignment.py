"""
Property-based tests for the slot-to-user configuration shape.

The version 3 release is one change: ``slots: {1: {name: Raman, pin: ...}}``
becomes ``users: {Raman: {pin: ...}}``, with the slot number demoted to
internal bookkeeping. Two things can go wrong, and both are invariants over
the whole configuration rather than any single slot:

* **something is lost** -- a field, or a whole user, disappears in the
  conversion, which is silent because the result is still valid;
* **somebody is renumbered** -- a user's slot changes, which moves their
  credential to a different index on every lock and orphans their entities.

Neither is a property of one call. Both are stated here over generated
configurations and edit histories.
"""

from __future__ import annotations

from hypothesis import assume, given, strategies as st

from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN

from custom_components.lock_code_manager.domain.names import normalize_slot_names
from custom_components.lock_code_manager.domain.slot_assignment import (
    SlotAssignment,
    _identity,
    users_from_slots,
)

NAMES = st.sampled_from(["Raman", "Alice", "Bob", "Cleaner", "Guest 1"])
NAME_SETS = st.lists(NAMES, max_size=5).map(lambda ns: list(dict.fromkeys(ns)))
EDIT_HISTORIES = st.lists(NAME_SETS, min_size=1, max_size=8)

SLOT_CONFIGS = st.builds(
    lambda name, pin, enabled: {
        CONF_NAME: name,
        CONF_PIN: pin,
        CONF_ENABLED: enabled,
    },
    NAMES,
    st.text(alphabet="0123456789", min_size=4, max_size=6),
    st.booleans(),
)


@st.composite
def slot_mappings(draw: st.DrawFn) -> dict[int, dict]:
    """Slot-keyed configurations with unique names and int keys."""
    configs = draw(st.lists(SLOT_CONFIGS, max_size=5))
    seen: set[str] = set()
    unique = [
        c for c in configs if not (c[CONF_NAME] in seen or seen.add(c[CONF_NAME]))
    ]
    starts = draw(st.integers(min_value=1, max_value=4))
    return {starts + i: config for i, config in enumerate(unique)}


@st.composite
def stored_slot_mappings(draw: st.DrawFn) -> dict:
    """Configurations shaped like STORAGE rather than like valid input.

    Keys are strings, because that is what the on-disk JSON form yields and
    what a migration reading stored data actually hands in. Names may repeat
    or be missing, because both are reachable from a version 2 entry where
    the name is optional.

    The clean strategy above cannot expose either hazard, which is exactly
    why the string-key slot collision survived the first round of these
    properties.
    """
    configs = draw(st.lists(SLOT_CONFIGS, max_size=4))
    start = draw(st.integers(min_value=1, max_value=4))
    slots = {}
    for i, config in enumerate(configs):
        nameless = draw(st.booleans())
        slots[str(start + i)] = (
            {k: v for k, v in config.items() if k != CONF_NAME} if nameless else config
        )
    return slots


@given(slots=slot_mappings())
def test_conversion_loses_no_user(slots: dict[int, dict]) -> None:
    """Every configured slot becomes exactly one user."""
    users, assignment, _ = users_from_slots(slots)

    assert len(users) == len(slots)
    assert set(users) == {slot[CONF_NAME] for slot in slots.values()}
    # Compared through slot(), not by key set: users are keyed by the name as
    # displayed while the assignment keys by the identity form the rest of
    # the system compares on (whitespace-normalized, casefolded).
    assert len(assignment.slots) == len(users)
    assert all(assignment.slot(name) is not None for name in users)


@given(slots=slot_mappings())
def test_conversion_loses_no_field(slots: dict[int, dict]) -> None:
    """Every field except the name survives, unchanged.

    The name stops being a field because it becomes the key. Anything else
    going missing is silent data loss -- the result still validates, the user
    just quietly has no code.
    """
    users, _, _ = users_from_slots(slots)

    for slot in slots.values():
        user = users[slot[CONF_NAME]]
        assert user == {k: v for k, v in slot.items() if k != CONF_NAME}


@given(slots=slot_mappings())
def test_conversion_renumbers_nobody(slots: dict[int, dict]) -> None:
    """Each user keeps the slot number they already occupied.

    This is what makes the migration free: the slot is what identifiers key
    on and what addresses the credential on the lock, so a user whose number
    changed would have their entities orphaned AND their code written to a
    different index.
    """
    _, assignment, _ = users_from_slots(slots)

    for slot_num, slot in slots.items():
        assert assignment.slot(slot[CONF_NAME]) == slot_num


@given(slots=slot_mappings())
def test_conversion_round_trips(slots: dict[int, dict]) -> None:
    """The original slot-keyed configuration can be reconstructed exactly.

    A stronger statement than the two above together: it rules out any
    reshaping that happens to preserve counts and fields separately while
    still pairing them up wrongly.
    """
    users, assignment, _ = users_from_slots(slots)

    rebuilt = {
        assignment.slot(name): {CONF_NAME: name, **user} for name, user in users.items()
    }
    assert rebuilt == slots


START = st.integers(min_value=1, max_value=6)


def _reconcile(assignment, names, **kwargs):
    """Reconcile with a start slot supplied, since it is required."""
    return assignment.reconcile(names, start=kwargs.pop("start", 1), **kwargs)


@given(history=EDIT_HISTORIES, start=START)
def test_a_surviving_user_is_never_renumbered(
    history: list[list[str]], start: int
) -> None:
    """Adding or removing users must not move anyone else.

    Renumbering a bystander rewrites their credential to a different index on
    every lock.
    """
    assignment = SlotAssignment.empty()
    for configured in history:
        after = _reconcile(assignment, configured, start=start)
        for name in set(assignment.slots) & {_identity(n) for n in configured}:
            assert after.slot(name) == assignment.slot(name)
        assignment = after


@given(history=EDIT_HISTORIES, start=START)
def test_two_users_never_share_a_slot(history: list[list[str]], start: int) -> None:
    """A slot addresses one credential, so two users in it overwrite each other."""
    assignment = SlotAssignment.empty()
    for configured in history:
        assignment = _reconcile(assignment, configured, start=start)
        numbers = list(assignment.slots.values())
        assert len(numbers) == len(set(numbers))


@given(history=EDIT_HISTORIES, start=START)
def test_every_configured_user_holds_a_slot(
    history: list[list[str]], start: int
) -> None:
    """Nobody configured is left without a slot, however the set churns."""
    assignment = SlotAssignment.empty()
    for configured in history:
        assignment = _reconcile(assignment, configured, start=start)
        assert {_identity(n) for n in configured} == set(assignment.slots)


NEWCOMERS = st.lists(st.sampled_from(["Zoe", "Yan", "Xavier"]), max_size=2).map(
    lambda ns: list(dict.fromkeys(ns))
)


@given(
    names=NAME_SETS,
    renames=st.dictionaries(NAMES, st.sampled_from(["Wren", "Vic"]), max_size=2),
    newcomers=NEWCOMERS,
    start=START,
    data=st.data(),
)
def test_renaming_keeps_the_users_slot(
    names: list[str],
    renames: dict[str, str],
    newcomers: list[str],
    start: int,
    data: st.DataObject,
) -> None:
    """A rename changes who holds a slot, never which slot they hold.

    Two things make this property actually bite, and it did not before.

    It filters on the IDENTITY form. Filtering on the raw name went vacuous
    the moment keys became casefolded -- every generated name is capitalized,
    so the filter emptied the map and every assertion was trivially true.

    And it adds users in a SHUFFLED order alongside the rename. Without a
    competing addition, ignoring renames entirely still passes: the newcomer
    is handed the very slot the "departed" user just freed, so delete-plus-add
    and rename coincide. They only diverge when somebody else is in line for
    that slot, which is exactly the case that reorders two users' credential
    indices on a real lock.
    """
    before = _reconcile(SlotAssignment.empty(), names, start=start)
    moves = {old: new for old, new in renames.items() if _identity(old) in before.slots}
    assume(len({_identity(v) for v in moves.values()}) == len(moves))

    after_names = data.draw(
        st.permutations([moves.get(n, n) for n in names] + newcomers)
    )

    after = _reconcile(before, after_names, renames=moves, start=start)

    for old, new in moves.items():
        assert after.slot(new) == before.slot(old)
    for name in names:
        if name not in moves:
            assert after.slot(name) == before.slot(name)


@given(names=NAME_SETS, renames=st.dictionaries(NAMES, NAMES, max_size=3), start=START)
def test_renaming_does_not_depend_on_insertion_order(
    names: list[str], renames: dict[str, str], start: int
) -> None:
    """The same edit gives the same answer whatever order the mapping holds."""
    before = _reconcile(SlotAssignment.empty(), names, start=start)
    moves = {old: new for old, new in renames.items() if _identity(old) in before.slots}
    assume(len({_identity(v) for v in moves.values()}) == len(moves))
    after_names = [moves.get(n, n) for n in names]

    flipped = SlotAssignment(slots=dict(reversed(list(before.slots.items()))))

    assert dict(
        _reconcile(before, after_names, renames=moves, start=start).slots
    ) == dict(_reconcile(flipped, after_names, renames=moves, start=start).slots)


@given(names=NAME_SETS, start=START)
def test_reconciling_the_same_users_twice_changes_nothing(
    names: list[str], start: int
) -> None:
    """No renames and the same names is a no-op, so callers do not write."""
    once = _reconcile(SlotAssignment.empty(), names, start=start)

    assert once.reconcile(names, start=start) is once


@given(
    names=NAME_SETS,
    start=START,
    unavailable=st.sets(st.integers(min_value=1, max_value=12), max_size=4),
)
def test_new_users_respect_the_start_slot_and_reserved_numbers(
    names: list[str], start: int, unavailable: set[int]
) -> None:
    """A newly issued slot is never below the start, nor one already spoken for.

    The start slot is usually chosen because the numbers below it hold codes
    programmed by hand, and another entry may own slots on the same lock. The
    slot IS the credential index on most providers, so issuing one of those
    overwrites a real code on a real door.
    """
    assignment = SlotAssignment.empty().reconcile(
        names, start=start, unavailable=unavailable
    )

    for number in assignment.slots.values():
        assert number >= start
        assert number not in unavailable


@given(names=NAME_SETS, start=START)
def test_case_only_differences_are_the_same_user(names: list[str], start: int) -> None:
    """``Bob`` and ``BOB`` are one user, as ``names.deduplicate`` already says."""
    assignment = _reconcile(SlotAssignment.empty(), names, start=start)

    for name in names:
        assert assignment.slot(name.upper()) == assignment.slot(name.lower())
    assert assignment.reconcile([n.upper() for n in names], start=start) is assignment


@given(names=NAME_SETS, pad=st.sampled_from([" ", "  ", "\t"]), start=START)
def test_a_whitespace_variant_is_the_same_user(
    names: list[str], pad: str, start: int
) -> None:
    """``"Raman "`` and ``"Raman"`` must never be two different users."""
    bare = _reconcile(SlotAssignment.empty(), names, start=start)

    assert bare.reconcile([f"{pad}{n}{pad}" for n in names], start=start) is bare

    stored_padded = SlotAssignment(
        slots={f"{pad}{n}{pad}": s for n, s in bare.slots.items()}
    )
    for name in names:
        assert stored_padded.slot(name) == bare.slot(name)


@given(stored=stored_slot_mappings(), later=NAME_SETS, start=START)
def test_editing_after_a_migration_never_double_books_a_slot(
    stored: dict, later: list[str], start: int
) -> None:
    """Migrate, then keep editing -- the two halves composed.

    Production reaches reconcile from a MIGRATED assignment built out of
    storage, never from ``empty()``. That gap hid a string-key collision that
    issued one credential index to two users.
    """
    _, assignment, _ = users_from_slots(stored)
    after = _reconcile(assignment, [*assignment.slots, *later], start=start)

    numbers = list(after.slots.values())
    assert len(numbers) == len(set(numbers))
    assert all(isinstance(s, int) for s in numbers)


@given(stored=stored_slot_mappings())
def test_migration_always_produces_one_user_per_slot(stored: dict) -> None:
    """No slot is lost to a missing or duplicated name."""
    users, assignment, _ = users_from_slots(stored)

    assert len(users) == len(stored)
    assert len(assignment.slots) == len(stored)
    assert set(assignment.slots.values()) == {int(k) for k in stored}


@given(stored=stored_slot_mappings())
def test_repair_pairs_each_user_with_their_own_fields_and_slot(stored: dict) -> None:
    """Repaired names stay attached to the right fields and the right slot."""
    users, assignment, _ = users_from_slots(stored)
    repaired, _ = normalize_slot_names(stored)

    for raw_key, slot in repaired.items():
        name = slot[CONF_NAME]
        assert assignment.slot(name) == int(raw_key)
        assert users[name] == {k: v for k, v in slot.items() if k != CONF_NAME}


@given(history=EDIT_HISTORIES, start=START)
def test_assignment_survives_a_round_trip_through_storage(
    history: list[list[str]], start: int
) -> None:
    """What is persisted reloads identically.

    The assignment shares the entry with the configuration; a write that
    dropped it would renumber every user on the next start.
    """
    assignment = SlotAssignment.empty()
    for configured in history:
        assignment = assignment.reconcile(configured, start=start)

    restored = SlotAssignment.from_mapping(assignment.to_dict())

    assert dict(restored.slots) == dict(assignment.slots)
    # Equal assignments must hash equally. ``frozen=True`` advertises
    # hashability, but the generated __hash__ hashes the mapping field and
    # raises, so the promise has to be kept explicitly or it is a runtime trap
    # for the first caller to use one as a dict key or in a set.
    assert restored == assignment
    assert hash(restored) == hash(assignment)
