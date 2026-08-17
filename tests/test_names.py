"""Tests for the user-name rules that make the name a usable identity."""

import pytest

from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN

from custom_components.lock_code_manager.domain.names import (
    check_name,
    deduplicate,
    fallback_name,
    name_error,
    normalize_slot_names,
    validate_slot_names,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Raman", None),
        ("  Raman  ", None),
        (None, "name_required"),
        ("", "name_required"),
        ("   ", "name_required"),
        ("Ra|man", "name_has_separator"),
        ("|", "name_has_separator"),
    ],
)
def test_name_error(name: str | None, expected: str | None) -> None:
    """A name must be present and free of the identifier separator."""
    assert name_error(name) == expected


def test_fallback_name_reads_like_the_slot_it_replaces() -> None:
    """An auto-named slot should not read as something new."""
    assert fallback_name(3) == "User 3"


@pytest.mark.parametrize(
    ("name", "taken", "expected"),
    [
        ("Raman", [], "Raman"),
        ("Raman", ["Alice"], "Raman"),
        ("Raman", ["Raman"], "Raman 2"),
        ("Raman", ["Raman", "Raman 2"], "Raman 3"),
        # Case-insensitive: two users differing only in case would be
        # indistinguishable in the frontend and would collide once slugified.
        ("raman", ["Raman"], "raman 2"),
        ("RAMAN", ["raman", "RAMAN 2"], "RAMAN 3"),
    ],
)
def test_deduplicate(name: str, taken: list[str], expected: str) -> None:
    """Colliding names gain the lowest free numeric suffix."""
    assert deduplicate(name, taken) == expected


def test_normalize_leaves_valid_unique_names_untouched() -> None:
    """A name the user chose is never rewritten.

    Rewriting one would also rename that user on every lock that stores a
    user name, so an unnecessary rewrite is a device write, not a no-op.
    """
    slots = {
        1: {CONF_NAME: "Raman", CONF_ENABLED: True, CONF_PIN: "1234"},
        2: {CONF_NAME: "Alice", CONF_ENABLED: True},
    }

    repaired, changed = normalize_slot_names(slots)

    assert changed == []
    assert repaired[1][CONF_NAME] == "Raman"
    assert repaired[2][CONF_NAME] == "Alice"
    # Other fields survive untouched.
    assert repaired[1][CONF_PIN] == "1234"


def test_normalize_names_unnamed_slots() -> None:
    """A slot with no name gets one derived from its number."""
    slots = {1: {CONF_ENABLED: True}, 2: {CONF_NAME: None, CONF_ENABLED: True}}

    repaired, changed = normalize_slot_names(slots)

    assert repaired[1][CONF_NAME] == "User 1"
    assert repaired[2][CONF_NAME] == "User 2"
    assert sorted(changed) == ["1", "2"]


def test_normalize_strips_the_separator() -> None:
    """The separator is replaced rather than the name rejected."""
    repaired, changed = normalize_slot_names({1: {CONF_NAME: "Ra|man"}})

    assert repaired[1][CONF_NAME] == "Ra man"
    assert changed == ["1"]


def test_normalize_falls_back_when_a_name_is_only_separators() -> None:
    """A name that collapses to empty after stripping falls back."""
    repaired, changed = normalize_slot_names({4: {CONF_NAME: "||"}})

    assert repaired[4][CONF_NAME] == "User 4"
    assert changed == ["4"]


def test_normalize_deduplicates_across_slots() -> None:
    """Two slots sharing a name are made unique, lowest slot number winning."""
    slots = {2: {CONF_NAME: "Raman"}, 1: {CONF_NAME: "Raman"}}

    repaired, changed = normalize_slot_names(slots)

    assert repaired[1][CONF_NAME] == "Raman"
    assert repaired[2][CONF_NAME] == "Raman 2"
    assert changed == ["2"]


def test_normalize_is_deterministic_regardless_of_mapping_order() -> None:
    """Iteration order must not decide who keeps the undecorated name.

    Slots are processed in numeric order, so a migration that runs twice --
    or on two machines -- produces the same names. Mapping order would make
    it a coin flip which slot got renamed.
    """
    forward = {1: {CONF_NAME: "Raman"}, 2: {CONF_NAME: "Raman"}}
    reverse = {2: {CONF_NAME: "Raman"}, 1: {CONF_NAME: "Raman"}}

    assert normalize_slot_names(forward) == normalize_slot_names(reverse)


def test_normalize_is_idempotent() -> None:
    """Running the repair over its own output changes nothing."""
    once, _ = normalize_slot_names({1: {CONF_ENABLED: True}, 2: {CONF_NAME: "|"}})
    twice, changed = normalize_slot_names(once)

    assert twice == once
    assert changed == []


def test_normalize_accepts_string_slot_keys() -> None:
    """On-disk JSON uses string slot keys; the repair must handle them."""
    repaired, changed = normalize_slot_names({"1": {CONF_ENABLED: True}})

    assert repaired["1"][CONF_NAME] == "User 1"
    assert changed == ["1"]


@pytest.mark.parametrize(
    ("slots", "expected"),
    [
        ({1: {CONF_NAME: "Raman"}, 2: {CONF_NAME: "Alice"}}, None),
        ({1: {CONF_ENABLED: True}}, ("1", "name_required")),
        ({1: {CONF_NAME: "  "}}, ("1", "name_required")),
        ({1: {CONF_NAME: "Ra|man"}}, ("1", "name_has_separator")),
        ({1: {CONF_NAME: "Raman"}, 2: {CONF_NAME: "raman"}}, ("2", "name_not_unique")),
        # Whitespace-padded duplicates must be caught in BOTH orders. Comparing
        # a stripped candidate against unstripped stored names caught only one.
        ({1: {CONF_NAME: "Raman "}, 2: {CONF_NAME: "Raman"}}, ("2", "name_not_unique")),
        (
            {1: {CONF_NAME: "Raman"}, 2: {CONF_NAME: " Raman "}},
            ("2", "name_not_unique"),
        ),
    ],
)
def test_validate_slot_names(slots, expected) -> None:
    """Whole-set validation reports the offending slot and why."""
    assert validate_slot_names(slots) == expected


def test_normalize_does_not_steal_a_later_slots_valid_name() -> None:
    """Repairing a duplicate must not push an innocent slot off its name.

    A single pass renames slot 2 to "Raman 2" and then finds slot 3 already
    holds that, pushing it to "Raman 2 2" -- renaming a user who did nothing
    wrong, and costing a write on every lock that stores names.
    """
    slots = {
        1: {CONF_NAME: "Raman"},
        2: {CONF_NAME: "Raman"},
        3: {CONF_NAME: "Raman 2"},
    }

    repaired, changed = normalize_slot_names(slots)

    assert repaired[1][CONF_NAME] == "Raman"
    assert repaired[3][CONF_NAME] == "Raman 2"  # untouched
    assert repaired[2][CONF_NAME] == "Raman 3"  # took the next free suffix
    assert changed == ["2"]


def test_normalize_strips_whitespace_from_otherwise_valid_names() -> None:
    """Padding is normalized away so two identical-looking names cannot coexist."""
    repaired, changed = normalize_slot_names({1: {CONF_NAME: "  Raman  "}})

    assert repaired[1][CONF_NAME] == "Raman"
    assert changed == ["1"]


@pytest.mark.parametrize(
    ("candidate", "taken", "expected_name", "expected_error"),
    [
        ("Raman", {}, "Raman", None),
        ("  Raman  ", {}, "Raman", None),
        ("Raman", {1: "Alice"}, "Raman", None),
        (None, {}, None, "name_required"),
        ("  ", {}, None, "name_required"),
        ("Ra|man", {}, None, "name_has_separator"),
        ("Raman", {1: "Raman"}, None, "name_not_unique"),
        ("raman", {1: "Raman"}, None, "name_not_unique"),
        # Padded on either side must collide in BOTH directions. Comparing a
        # stripped candidate against unstripped stored names caught only one.
        ("Raman", {1: "Raman "}, None, "name_not_unique"),
        ("  Raman  ", {1: "Raman"}, None, "name_not_unique"),
    ],
)
def test_check_name(candidate, taken, expected_name, expected_error) -> None:
    """One checker decides for every write path."""
    result = check_name(candidate, taken)

    assert result.name == expected_name
    assert result.error == expected_error


def test_check_name_reports_the_conflicting_slot() -> None:
    """The caller needs to say WHICH slot already holds the name."""
    assert check_name("Raman", {3: "raman"}).conflicting_slot == 3


def test_check_name_lets_a_slot_keep_its_own_name() -> None:
    """Renaming excludes the slot being renamed, or nobody could fix a typo.

    Callers omit the slot they are editing from ``taken``; this pins that
    the checker itself imposes no self-collision.
    """
    assert check_name("Raman", {}).error is None


def test_write_paths_agree_on_what_they_accept() -> None:
    """The whole-set validator and the per-name checker must not diverge.

    They used to be separate implementations of the same rules, and the
    differences between the copies were real bugs. This fails if one grows a
    rule the other lacks.
    """
    cases = ["Raman", "  Raman  ", "", "   ", "Ra|man", None]
    for candidate in cases:
        per_name = check_name(candidate, {})
        whole_set = validate_slot_names({1: {CONF_NAME: candidate}})

        assert (whole_set is None) == (per_name.error is None), candidate
        if whole_set is not None:
            assert whole_set[1] == per_name.error, candidate
