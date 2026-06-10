"""Tests for the shared provider helpers in ``_util.py``."""

from __future__ import annotations

import pytest

from custom_components.lock_code_manager.providers._util import (
    make_new_tagged_name,
    make_tagged_name,
    parse_slot_num,
    parse_tag,
    parse_tag_with_rewrite,
)


class TestMakeTaggedName:
    """Legacy ``[LCM:<slot>]`` builder; preserved for Schlage/Akuvox writes."""

    @pytest.mark.parametrize(
        ("slot", "name", "expected"),
        [
            pytest.param(1, "Guest", "[LCM:1] Guest", id="with-name"),
            pytest.param(5, None, "[LCM:5] Code Slot 5", id="default-name"),
            pytest.param(
                99, "Family Member", "[LCM:99] Family Member", id="multi-word"
            ),
        ],
    )
    def test_make_tagged_name(self, slot: int, name: str | None, expected: str) -> None:
        assert make_tagged_name(slot, name) == expected


class TestMakeNewTaggedName:
    """New compact ``lcm<slot>:`` builder."""

    @pytest.mark.parametrize(
        ("slot", "name", "expected"),
        [
            pytest.param(1, "Guest", "lcm1:Guest", id="with-name"),
            pytest.param(5, None, "lcm5:Code Slot 5", id="default-name"),
            pytest.param(255, "X", "lcm255:X", id="max-slot-min-name"),
            pytest.param(7, "Bob Jones", "lcm7:Bob Jones", id="multi-word"),
            pytest.param(3, "", "lcm3:Code Slot 3", id="empty-name-uses-default"),
        ],
    )
    def test_make_new_tagged_name(
        self, slot: int, name: str | None, expected: str
    ) -> None:
        assert make_new_tagged_name(slot, name) == expected


class TestParseTagWithRewrite:
    """Tolerant parser that accepts both formats and flags legacy matches."""

    @pytest.mark.parametrize(
        ("input_name", "expected"),
        [
            # New format -- no rewrite needed.
            pytest.param("lcm1:Guest", (1, "Guest", False), id="new-simple"),
            pytest.param("lcm255:Family", (255, "Family", False), id="new-large-slot"),
            pytest.param(
                "lcm7:Bob Jones", (7, "Bob Jones", False), id="new-multi-word"
            ),
            pytest.param("lcm5:", (5, "", False), id="new-empty-display"),
            pytest.param(
                "lcm5:lcm6:nested",
                (5, "lcm6:nested", False),
                id="new-display-looks-like-tag",
            ),
            # Legacy format -- rewrite needed.
            pytest.param("[LCM:1] Guest", (1, "Guest", True), id="legacy-simple"),
            pytest.param(
                "[LCM:99] Family", (99, "Family", True), id="legacy-large-slot"
            ),
            pytest.param(
                "[LCM:5]Tight", (5, "Tight", True), id="legacy-no-space-after-bracket"
            ),
            # Untagged or malformed -- no match, no rewrite.
            pytest.param("Guest Code", (None, "Guest Code", False), id="untagged"),
            pytest.param("", (None, "", False), id="empty-string"),
            pytest.param(
                "lcm 5:Alice",
                (None, "lcm 5:Alice", False),
                id="space-after-prefix-rejected",
            ),
            pytest.param(
                "lcm-5:Alice",
                (None, "lcm-5:Alice", False),
                id="negative-slot-rejected",
            ),
            pytest.param(
                ":lcm5:Alice",
                (None, ":lcm5:Alice", False),
                id="leading-colon-rejected",
            ),
            pytest.param(
                "LCM5:Alice",
                (None, "LCM5:Alice", False),
                id="uppercase-prefix-rejected",
            ),
        ],
    )
    def test_parse_tag_with_rewrite(
        self, input_name: str, expected: tuple[int | None, str, bool]
    ) -> None:
        assert parse_tag_with_rewrite(input_name) == expected

    def test_new_format_preferred_when_ambiguous(self) -> None:
        """A name that happens to satisfy both regexes uses the new format match.

        Not a realistic name, but pins the precedence so reviewers know which
        branch wins if the parser is fed an oddball value.
        """
        # The new-format regex matches eagerly from the start of the string;
        # the legacy regex would not match this input, but the test asserts
        # that the new-format check runs first.
        assert parse_tag_with_rewrite("lcm5:[LCM:6] x") == (5, "[LCM:6] x", False)


class TestParseTag:
    """Thin 2-tuple wrapper -- drops the rewrite flag."""

    @pytest.mark.parametrize(
        ("input_name", "expected"),
        [
            pytest.param("lcm5:Alice", (5, "Alice"), id="new-format"),
            pytest.param("[LCM:5] Alice", (5, "Alice"), id="legacy-format"),
            pytest.param("Alice", (None, "Alice"), id="untagged"),
            pytest.param("", (None, ""), id="empty"),
        ],
    )
    def test_parse_tag(self, input_name: str, expected: tuple[int | None, str]) -> None:
        assert parse_tag(input_name) == expected


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
