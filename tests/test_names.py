"""Tests for the user-name rules that make the name a usable identity."""

import pytest

from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN

from custom_components.lock_code_manager.domain.names import (
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
        # The separator rule is gone. It existed only while identifiers were
        # keyed by name and delimited by "|"; they are keyed by slot number,
        # so a name carrying one splits nothing.
        ("Ra|man", None),
        ("|", None),
    ],
)
def test_name_error(name: str | None, expected: str | None) -> None:
    """A name only has to be present."""
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


def test_normalize_leaves_a_separator_alone() -> None:
    """A "|" in a name is now ordinary text and must survive untouched.

    The repair used to rewrite it to a space, which renamed the user on every
    lock that stores names. That was only ever needed because identifiers were
    delimited by "|" and keyed by the name; they are keyed by the slot number.
    """
    repaired, changed = normalize_slot_names({1: {CONF_NAME: "Ra|man"}})

    assert repaired[1][CONF_NAME] == "Ra|man"
    assert changed == []


def test_normalize_falls_back_only_for_an_empty_name() -> None:
    """A name that is empty or whitespace still falls back."""
    repaired, changed = normalize_slot_names({4: {CONF_NAME: "   "}})

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
