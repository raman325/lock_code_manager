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

from custom_components.lock_code_manager.domain.slot_assignment import (
    SlotAssignment,
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
    assert set(assignment.slots) == set(users)


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


@given(history=EDIT_HISTORIES)
def test_a_surviving_user_is_never_renumbered(history: list[list[str]]) -> None:
    """Adding or removing users must not move anyone else.

    Renumbering a bystander rewrites their credential to a different index on
    every lock. This is the invariant that a naive "renumber from one on every
    edit" implementation violates, and it needs a sequence to expose.
    """
    assignment = SlotAssignment.empty()
    for configured in history:
        after = assignment.assign(configured)
        for name in set(assignment.slots) & set(configured):
            assert after.slot(name) == assignment.slot(name)
        assignment = after


@given(history=EDIT_HISTORIES)
def test_two_users_never_share_a_slot(history: list[list[str]]) -> None:
    """A slot addresses one credential, so two users in it would overwrite each other."""
    assignment = SlotAssignment.empty()
    for configured in history:
        assignment = assignment.assign(configured)
        numbers = list(assignment.slots.values())
        assert len(numbers) == len(set(numbers))


@given(history=EDIT_HISTORIES)
def test_slots_never_exceed_the_most_users_ever_configured_at_once(
    history: list[list[str]],
) -> None:
    """Numbers stay inside the high-water mark of CONCURRENT users.

    Not the current count -- a survivor keeps their number when someone below
    them leaves, which is required and is the property above. The bound that
    does hold is the most users ever configured simultaneously, and it is the
    one that matters: capacity is sized against how many users exist at once,
    and on most providers the slot IS the lock's credential index, where a
    number above the advertised count can never be written.

    A never-reused number satisfies neither bound: it climbs with every user
    ever created and eventually leaves the lock's range entirely. Reuse is
    what keeps the numbering inside capacity, which is why the slot number is
    the right thing to key on and a monotonic handle is not.
    """
    assignment = SlotAssignment.empty()
    high_water = 0
    for configured in history:
        assignment = assignment.assign(configured)
        high_water = max(high_water, len(configured))
        assert all(1 <= s <= high_water for s in assignment.slots.values())


@given(stored=stored_slot_mappings(), later=NAME_SETS)
def test_editing_after_a_migration_never_double_books_a_slot(
    stored: dict, later: list[str]
) -> None:
    """Migrate, then keep editing -- the two halves composed.

    Every other assignment property starts from ``SlotAssignment.empty()``,
    which is not how production reaches ``assign``: it reaches it from a
    migrated assignment, built from storage. That gap hid a real defect --
    string keys from JSON survived into the assignment, so ``candidate in
    taken`` compared an int against a string, missed, and issued an occupied
    slot to the next user. Two users on one credential index means one
    overwrites the other on every lock.
    """
    _, assignment, _ = users_from_slots(stored)
    after = assignment.assign([*assignment.slots, *later])

    numbers = list(after.slots.values())
    assert len(numbers) == len(set(numbers))
    assert all(isinstance(s, int) for s in numbers)


@given(stored=stored_slot_mappings())
def test_migration_always_produces_one_user_per_slot(stored: dict) -> None:
    """No slot is lost to a missing or duplicated name.

    Both are reachable from a version 2 entry. Before the repair was folded
    in, two slots sharing a name collapsed into one user -- losing that
    user's code AND renumbering the survivor -- and a nameless slot raised
    mid-migration, on a path with no rollback.
    """
    users, assignment, _ = users_from_slots(stored)

    assert len(users) == len(stored)
    assert len(assignment.slots) == len(stored)
    assert set(assignment.slots.values()) == {int(k) for k in stored}


@given(names=NAME_SETS, renames=st.dictionaries(NAMES, NAMES, max_size=3))
def test_renaming_never_renumbers_anyone(
    names: list[str], renames: dict[str, str]
) -> None:
    """A rename changes who holds a slot, never which slot they hold.

    ``assign`` alone cannot see a rename -- it looks like a deletion plus an
    addition, so it frees the old name's slot and reissues it in iteration
    order, landing two renamed users on each other's index. Re-keying first
    is what keeps the rename free, including for a swap.
    """
    before = SlotAssignment.empty().assign(names)
    renames = {old: new for old, new in renames.items() if old in before.slots}
    # A rename onto a name someone else still holds is a collision the name
    # rules reject upstream, not something this has to resolve.
    assume(len(set(renames.values())) == len(renames))
    assume(not set(renames.values()) & (set(before.slots) - set(renames)))

    after = before.with_renames(renames)

    for old, new in renames.items():
        assert after.slot(new) == before.slot(old)
    for name in set(before.slots) - set(renames):
        assert after.slot(name) == before.slot(name)


@given(names=NAME_SETS)
def test_assignment_is_idempotent(names: list[str]) -> None:
    """Assigning the same users twice changes nothing."""
    once = SlotAssignment.empty().assign(names)

    assert once.assign(names) is once


@given(history=EDIT_HISTORIES)
def test_assignment_survives_a_round_trip_through_storage(
    history: list[list[str]],
) -> None:
    """What is persisted reloads identically.

    The assignment shares the entry with the configuration; a write that
    dropped it would renumber every user on the next start.
    """
    assignment = SlotAssignment.empty()
    for configured in history:
        assignment = assignment.assign(configured)

    restored = SlotAssignment.from_mapping(assignment.to_dict())

    assert dict(restored.slots) == dict(assignment.slots)
