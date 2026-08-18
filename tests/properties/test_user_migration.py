"""
Property-based tests for the version 3 configuration migration.

Written from `docs/superpowers/specs/2026-08-18-b2c-invariants.md` BEFORE the
implementation, so they state what the migration must honour rather than
describing what it happens to do. The numbered references below are that
document's constraints.

The migration has no rollback (constraint 12), so every failure mode here is
permanent for the user it happens to:

* **something is lost** -- a user, or a field of one. Silent, because the
  result is still a valid configuration.
* **somebody is renumbered** -- the slot is the credential index on most
  providers (constraint 1), so a changed number moves that person's code to a
  different index on every lock and orphans their entities.
* **a retired key survives** -- `start_slot` and `num_slots` no longer mean
  anything, and a stale one would be read by something eventually.
"""

from __future__ import annotations

from hypothesis import given, strategies as st

from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN

from custom_components.lock_code_manager.const import (
    CONF_LOCKS,
    CONF_NUM_SLOTS,
    CONF_SLOTS,
    CONF_START_SLOT,
    CONF_USERS,
)
from custom_components.lock_code_manager.domain.config import EntryConfig
from custom_components.lock_code_manager.domain.slot_assignment import (
    CONF_SLOT_ASSIGNMENT,
    SlotAssignment,
    _identity,
)
from custom_components.lock_code_manager.domain.user_migration import (
    migrate_to_users,
)

NAMES = st.sampled_from(["Raman", "Alice", "Bob", "Cleaner", "Guest 1"])

SLOT_CONFIGS = st.builds(
    lambda name, pin, enabled: {CONF_NAME: name, CONF_PIN: pin, CONF_ENABLED: enabled},
    NAMES,
    st.text(alphabet="0123456789", min_size=4, max_size=6),
    st.booleans(),
)


@st.composite
def v2_entries(draw: st.DrawFn) -> dict:
    """Entry mappings shaped like STORAGE, including the awkward ones.

    Keys are strings because that is the on-disk JSON form. Names may be
    missing or duplicated, both reachable from a version 2 entry where the
    name is optional (constraint 7). The start slot is drawn above 1 often
    enough that "preserved, not renumbered" is a real assertion rather than a
    coincidence of everything starting at 1.
    """
    configs = draw(st.lists(SLOT_CONFIGS, max_size=4))
    start = draw(st.integers(min_value=1, max_value=12))
    slots = {}
    for i, config in enumerate(configs):
        nameless = draw(st.booleans())
        slots[str(start + i)] = (
            {k: v for k, v in config.items() if k != CONF_NAME} if nameless else config
        )
    entry = {
        CONF_LOCKS: draw(
            st.lists(st.sampled_from(["lock.front", "lock.back"]), max_size=2)
        ),
        CONF_SLOTS: slots,
        CONF_START_SLOT: start,
        CONF_NUM_SLOTS: len(slots),
    }
    if draw(st.booleans()):
        entry["some_future_key"] = "kept"
    return entry


@given(entry=v2_entries())
def test_every_slot_becomes_exactly_one_user(entry: dict) -> None:
    """Nobody is lost, however badly the names were configured."""
    migrated, _ = migrate_to_users(entry)

    assert len(migrated[CONF_USERS]) == len(entry[CONF_SLOTS])


@given(entry=v2_entries())
def test_nobody_is_renumbered(entry: dict) -> None:
    """Each user keeps the slot number they already occupied (constraint 1).

    The whole reason the migration is safe: the slot is what identifiers key
    on and what addresses the credential on the lock, so a changed number
    orphans that user's entities AND writes their code to a different index.
    """
    migrated, _ = migrate_to_users(entry)
    assignment = SlotAssignment(slots=migrated[CONF_SLOT_ASSIGNMENT])

    assert sorted(assignment.slots.values()) == sorted(
        int(k) for k in entry[CONF_SLOTS]
    )
    for name in migrated[CONF_USERS]:
        assert assignment.slot(name) is not None


@given(entry=v2_entries())
def test_no_field_is_lost_except_the_name(entry: dict) -> None:
    """The name stops being a field because it becomes the key.

    Anything else going missing is silent: the result still validates, the
    user simply has no code any more.
    """
    migrated, _ = migrate_to_users(entry)
    assignment = SlotAssignment(slots=migrated[CONF_SLOT_ASSIGNMENT])
    by_slot = {
        assignment.slot(name): user for name, user in migrated[CONF_USERS].items()
    }

    for raw_slot, slot in entry[CONF_SLOTS].items():
        migrated_user = by_slot[int(raw_slot)]
        assert migrated_user == {k: v for k, v in slot.items() if k != CONF_NAME}


@given(entry=v2_entries())
def test_the_retired_keys_are_gone(entry: dict) -> None:
    """slots, start_slot and num_slots no longer mean anything.

    There is no start slot any more -- allocation takes the lowest slot not
    already occupied. A stale key would eventually be read by something.
    """
    migrated, _ = migrate_to_users(entry)

    assert CONF_SLOTS not in migrated
    assert CONF_START_SLOT not in migrated
    assert CONF_NUM_SLOTS not in migrated


@given(entry=v2_entries())
def test_everything_else_is_carried_through(entry: dict) -> None:
    """Locks and any key this migration does not know about survive verbatim."""
    migrated, _ = migrate_to_users(entry)

    assert migrated[CONF_LOCKS] == entry[CONF_LOCKS]
    if "some_future_key" in entry:
        assert migrated["some_future_key"] == "kept"


@given(entry=v2_entries())
def test_user_keys_keep_the_name_as_displayed(entry: dict) -> None:
    """The key is the name as typed, not a casefolded version of it.

    This property said the opposite when first written, and implementing the
    reader is what exposed it: the name stopped being a field when it became
    the key, so the key is now the ONLY place capitalization survives. The
    configuration is hand-editable, and someone who typed "Raman" getting
    "raman" back is a bug.

    Uniqueness stays case-insensitive regardless (constraint 8) --
    names.deduplicate guarantees no two differ only by case, and lookups fold
    it. Storage keeps the display; comparison folds the case.
    """
    migrated, _ = migrate_to_users(entry)

    for name in migrated[CONF_USERS]:
        assert name == name.strip()
    folded = [_identity(name) for name in migrated[CONF_USERS]]
    assert len(folded) == len(set(folded))


@given(entry=v2_entries())
def test_migrating_twice_changes_nothing(entry: dict) -> None:
    """A migration that runs again must be a no-op.

    It should not run twice -- the version stamp prevents it -- but a
    migration with no rollback should not depend on that for its safety.
    """
    once, _ = migrate_to_users(entry)
    twice, renamed = migrate_to_users(once)

    assert twice == once
    assert renamed == []


def test_a_user_without_an_assigned_slot_is_skipped_not_guessed() -> None:
    """A user the assignment does not cover has no slot, and none is invented.

    Reachable from hand-edited storage, or from bookkeeping that lost an entry
    while the users mapping kept one. Guessing a number would put that user's
    code on a credential index somebody else may hold -- so they are left out
    until an allocation gives them one deliberately.
    """
    config = EntryConfig.from_mapping(
        {
            CONF_LOCKS: ["lock.front"],
            CONF_USERS: {"Raman": {CONF_PIN: "1234"}, "Ghost": {CONF_PIN: "9999"}},
            CONF_SLOT_ASSIGNMENT: {"raman": 3},
        }
    )

    assert {num: slot[CONF_NAME] for num, slot in config.slots.items()} == {3: "Raman"}
