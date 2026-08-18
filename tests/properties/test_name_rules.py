"""
Property-based tests for the user-name rules.

Mined from the review findings on #1417 and #1422. Names became the identity
everything keys on, and almost every finding in that area was a bug in
*checking* a property rather than in the property itself:

* a uniqueness check compared a stripped candidate against unstripped stored
  names, so a padded duplicate was refused in one order and accepted in the
  other -- an **asymmetry**;
* three write paths each ran their own combination of validation,
  normalization, and a case-folded scan, and drifted -- an **agreement**
  failure;
* the repair pass could hand one slot a name that pushed a different,
  already-valid slot off its own -- an **idempotence and stability** failure.

Each of those is a property. The example-based tests that shipped alongside
covered the cases their author thought of, and the reviews found the ones he
did not.
"""

from __future__ import annotations

from hypothesis import assume, given, strategies as st

from homeassistant.const import CONF_NAME

from custom_components.lock_code_manager.domain.names import (
    NAME_SEPARATOR,
    check_name,
    normalize_name,
    normalize_slot_names,
    validate_slot_names,
)

# Deliberately includes padding, case variation, the reserved separator, and
# the generated fallback shape, because every one of those produced a finding.
NAMES = st.one_of(
    st.sampled_from(
        ["Raman", "raman", "RAMAN", " Raman ", "Raman ", "", "   ", "Ra|man", "User 1"]
    ),
    st.text(max_size=12),
    st.none(),
)

SLOT_NUMS = st.integers(min_value=1, max_value=6)


@given(a=NAMES, b=NAMES)
def test_collision_detection_is_symmetric(a: str | None, b: str | None) -> None:
    """If a collides with b, b collides with a.

    The asymmetric version compared a stripped candidate against unstripped
    stored names, so "Raman " then "Raman" was accepted while the reverse was
    refused -- and the example test pinned only the direction that passed.
    """
    assume(check_name(a, {}).error is None)
    assume(check_name(b, {}).error is None)

    a_sees_b = check_name(a, {1: b}).error == "name_not_unique"
    b_sees_a = check_name(b, {1: a}).error == "name_not_unique"

    assert a_sees_b == b_sees_a


@given(name=NAMES)
def test_a_name_never_collides_with_nothing(name: str | None) -> None:
    """An empty set of taken names can never produce a collision."""
    assert check_name(name, {}).error != "name_not_unique"


@given(name=NAMES)
def test_accepted_names_are_stored_normalized(name: str | None) -> None:
    """What check_name returns is what identifiers are built from.

    Identifiers are built from the normalized form, so storing anything else
    makes every later comparison against the stored value miss -- which is
    how a padded name ended up with a device nothing matched.
    """
    result = check_name(name, {})
    assume(result.error is None)

    assert result.name == normalize_name(name)
    assert result.name == normalize_name(result.name)


@given(name=NAMES)
def test_the_two_write_paths_agree(name: str | None) -> None:
    """The per-name checker and the whole-set validator accept the same names.

    They were separate implementations of the same rules, and the differences
    between the copies were the bugs.
    """
    per_name = check_name(name, {})
    whole_set = validate_slot_names({1: {CONF_NAME: name}})

    assert (whole_set is None) == (per_name.error is None)
    if whole_set is not None:
        assert whole_set[1] == per_name.error


@given(
    slots=st.dictionaries(
        SLOT_NUMS, st.builds(lambda n: {CONF_NAME: n}, NAMES), max_size=5
    )
)
def test_repair_always_produces_something_valid(slots: dict) -> None:
    """Whatever goes in, the repaired slots pass validation.

    This is the contract the migration depends on: after it runs, the name is
    usable as an identifier segment for every slot.
    """
    repaired, _ = normalize_slot_names(slots)

    assert validate_slot_names(repaired) is None


@given(
    slots=st.dictionaries(
        SLOT_NUMS, st.builds(lambda n: {CONF_NAME: n}, NAMES), max_size=5
    )
)
def test_repair_is_idempotent(slots: dict) -> None:
    """Repairing repaired slots changes nothing.

    A second pass that still reports changes means the first did not converge
    -- and every reported change is a rename on every lock storing user names.
    """
    once, _ = normalize_slot_names(slots)
    twice, changed = normalize_slot_names(once)

    assert twice == once
    assert changed == []


@given(
    slots=st.dictionaries(
        SLOT_NUMS, st.builds(lambda n: {CONF_NAME: n}, NAMES), max_size=5
    )
)
def test_repair_never_produces_a_separator(slots: dict) -> None:
    """No repaired name contains the reserved delimiter.

    A name carrying it splits into the wrong fields coming back out of an
    identifier, which is the one thing the whole scheme cannot tolerate.
    """
    repaired, _ = normalize_slot_names(slots)

    for slot in repaired.values():
        assert NAME_SEPARATOR not in slot[CONF_NAME]


@given(
    slots=st.dictionaries(
        SLOT_NUMS, st.builds(lambda n: {CONF_NAME: n}, NAMES), min_size=1, max_size=5
    )
)
def test_repair_reports_exactly_what_it_changed(slots: dict) -> None:
    """The changed list matches the slots whose name actually moved.

    The migration logs this list and it drives what users are told changed on
    their locks, so an over-report is a false alarm and an under-report hides
    a device write.
    """
    repaired, changed = normalize_slot_names(slots)

    actually_changed = {
        str(slot_num)
        for slot_num, slot in slots.items()
        if slot.get(CONF_NAME) != repaired[slot_num][CONF_NAME]
    }
    assert set(changed) == actually_changed


@given(
    slots=st.dictionaries(
        SLOT_NUMS, st.builds(lambda n: {CONF_NAME: n}, NAMES), max_size=5
    )
)
def test_repair_preserves_a_name_that_was_already_valid_and_unique(
    slots: dict,
) -> None:
    """A slot whose name needed nothing keeps it.

    The single-pass repair could hand one slot a suffixed name that collided
    with a *different* slot whose name was valid and unique all along,
    pushing that innocent slot off its own name. Every avoided rewrite is an
    avoided rename on every lock that stores user names, so this is a device
    write, not a cosmetic detail.

    Normalization is the one permitted change: "Raman " becomes "Raman"
    because a name that can differ invisibly is not a usable identity.
    """
    normalized_counts: dict[str, int] = {}
    for slot in slots.values():
        key = normalize_name(slot.get(CONF_NAME)).casefold()
        normalized_counts[key] = normalized_counts.get(key, 0) + 1

    repaired, _ = normalize_slot_names(slots)

    for slot_num, slot in slots.items():
        original = slot.get(CONF_NAME)
        if check_name(original, {}).error is not None:
            continue  # needed repair on its own merits
        if normalized_counts[normalize_name(original).casefold()] > 1:
            continue  # genuinely contended
        assert repaired[slot_num][CONF_NAME] == normalize_name(original)
