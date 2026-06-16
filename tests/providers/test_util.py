"""Tests for the shared provider helpers in ``_util.py``."""

from __future__ import annotations

import pytest

from custom_components.lock_code_manager.providers._util import (
    make_compact_tagged_name,
    make_tagged_name,
    parse_slot_num,
    parse_tag,
)


class TestMakeTaggedName:
    """Canonical ``lcm:<slot>:`` builder."""

    @pytest.mark.parametrize(
        ("slot", "name", "expected"),
        [
            pytest.param(1, "Guest", "lcm:1:Guest", id="with-name"),
            pytest.param(5, None, "lcm:5:Code Slot 5", id="default-name"),
            pytest.param(255, "X", "lcm:255:X", id="max-slot-min-name"),
            pytest.param(7, "Bob Jones", "lcm:7:Bob Jones", id="multi-word"),
            pytest.param(3, "", "lcm:3:Code Slot 3", id="empty-name-uses-default"),
        ],
    )
    def test_make_tagged_name(self, slot: int, name: str | None, expected: str) -> None:
        assert make_tagged_name(slot, name) == expected


class TestMakeCompactTaggedName:
    """Compact ``lcm<slot>`` builder for charset-restrictive locks."""

    @pytest.mark.parametrize(
        ("slot", "expected"),
        [
            pytest.param(1, "lcm1", id="single-digit"),
            pytest.param(42, "lcm42", id="multi-digit"),
            pytest.param(255, "lcm255", id="max-slot"),
        ],
    )
    def test_make_compact_tagged_name(self, slot: int, expected: str) -> None:
        assert make_compact_tagged_name(slot) == expected


class TestParseTag:
    """Tolerant parser that accepts every format LCM has ever written."""

    @pytest.mark.parametrize(
        ("input_name", "expected"),
        [
            # Canonical ``lcm:<slot>:<name>``.
            pytest.param("lcm:1:Guest", (1, "Guest"), id="canonical-simple"),
            pytest.param("lcm:255:Family", (255, "Family"), id="canonical-large-slot"),
            pytest.param(
                "lcm:7:Bob Jones", (7, "Bob Jones"), id="canonical-multi-word"
            ),
            pytest.param("lcm:5:", (5, ""), id="canonical-empty-display"),
            pytest.param(
                "lcm:5: Alice",
                (5, "Alice"),
                id="canonical-trims-whitespace-after-colon",
            ),
            pytest.param(
                "lcm:5:   Alice",
                (5, "Alice"),
                id="canonical-trims-multiple-whitespace-after-colon",
            ),
            pytest.param(
                "lcm:5:lcm6:nested",
                (5, "lcm6:nested"),
                id="canonical-display-looks-like-tag",
            ),
            # Legacy ``[LCM:<slot>] <name>`` -- read-only.
            pytest.param("[LCM:1] Guest", (1, "Guest"), id="legacy-simple"),
            pytest.param("[LCM:99] Family", (99, "Family"), id="legacy-large-slot"),
            pytest.param(
                "[LCM:5]Tight", (5, "Tight"), id="legacy-no-space-after-bracket"
            ),
            # Compact fallback -- written when the lock firmware
            # rejects the colons in the canonical prefix.
            pytest.param("lcm1", (1, ""), id="compact-single-digit"),
            pytest.param("lcm42", (42, ""), id="compact-multi-digit"),
            pytest.param("lcm255", (255, ""), id="compact-max-slot"),
            # Slot-only fallback -- written when neither canonical nor
            # compact tier survives the lock's constraints.
            pytest.param("5", (5, ""), id="slot-only-single-digit"),
            pytest.param("255", (255, ""), id="slot-only-multi-digit"),
            # Untagged or malformed -- no match.
            pytest.param("Guest Code", (None, "Guest Code"), id="untagged"),
            pytest.param("", (None, ""), id="empty-string"),
            pytest.param(
                "lcm 5:Alice",
                (None, "lcm 5:Alice"),
                id="space-after-prefix-rejected",
            ),
            pytest.param(
                "lcm-5:Alice",
                (None, "lcm-5:Alice"),
                id="negative-slot-rejected",
            ),
            pytest.param(
                ":lcm5:Alice",
                (None, ":lcm5:Alice"),
                id="leading-colon-rejected",
            ),
            pytest.param(
                "LCM5:Alice",
                (None, "LCM5:Alice"),
                id="uppercase-prefix-rejected",
            ),
        ],
    )
    def test_parse_tag(self, input_name: str, expected: tuple[int | None, str]) -> None:
        assert parse_tag(input_name) == expected

    def test_compact_regex_does_not_match_canonical(self) -> None:
        """Anchored regexes prevent the compact arm from claiming a canonical match.

        The compact regex is ``^lcm(\\d+)$`` (full-anchored) and would
        not match ``lcm:5:Alice`` (the trailing ``:5:Alice`` violates
        the ``$`` anchor). Pinning this in a dedicated test so a future
        edit that relaxes the anchor (or drops it entirely) gets
        caught.
        """
        assert parse_tag("lcm:5:Alice") == (5, "Alice")

    def test_canonical_prefix_wins_when_display_contains_legacy_text(self) -> None:
        """The canonical prefix is consumed first; the display is taken verbatim.

        Both regexes are anchored to the start of the string, so only one can
        match a given input -- this isn't an ambiguity case. The test pins the
        intended behavior when the *display portion* (everything after
        ``lcm:<slot>:``) happens to contain something that looks like a legacy
        tag: the canonical prefix wins, slot 5 is the LCM slot, and the rest
        (including the literal ``[LCM:6] x``) is returned as the friendly name
        without further parsing.
        """
        assert parse_tag("lcm:5:[LCM:6] x") == (5, "[LCM:6] x")


class TestParseSlotNum:
    """Numeric coercion helper used by providers that read string-keyed slots."""

    @pytest.mark.parametrize(
        ("input_value", "expected"),
        [
            pytest.param(5, 5, id="int-passthrough"),
            pytest.param("5", 5, id="str-digits"),
            pytest.param("042", 42, id="str-padded"),
            pytest.param("five", None, id="str-non-numeric"),
            pytest.param(None, None, id="none"),
            pytest.param([], None, id="non-coercible-type"),
        ],
    )
    def test_parse_slot_num(self, input_value: object, expected: int | None) -> None:
        assert parse_slot_num(input_value) == expected
