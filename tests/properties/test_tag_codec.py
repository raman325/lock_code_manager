"""Property-based tests for the provider slot-tag codec."""

from __future__ import annotations

from hypothesis import given, strategies as st

from custom_components.lock_code_manager.providers._util import (
    make_compact_tagged_name,
    make_tagged_name,
    parse_slot_num,
    parse_tag,
)

SLOT_NUMS = st.integers(min_value=1, max_value=9999)

# Documented contract edges (see _util.py regexes): the canonical pattern
# strips leading whitespace after "lcm:<slot>:" via \s*, and "." never
# crosses a newline — so names starting with whitespace or containing
# newlines cannot round-trip by design. The strategy encodes that contract.
NAMES = st.text(min_size=1, max_size=40).filter(
    lambda s: "\n" not in s and not s[0].isspace()
)


@given(slot=SLOT_NUMS, name=NAMES)
def test_canonical_round_trip(slot: int, name: str) -> None:
    """Canonical lcm:<slot>:<name> encodes and decodes losslessly."""
    assert parse_tag(make_tagged_name(slot, name)) == (slot, name)


@given(slot=SLOT_NUMS)
def test_default_name_round_trip(slot: int) -> None:
    """Omitted name falls back to the documented 'Code Slot N' display."""
    assert parse_tag(make_tagged_name(slot)) == (slot, f"Code Slot {slot}")


@given(slot=SLOT_NUMS)
def test_compact_round_trip(slot: int) -> None:
    """Compact lcm<slot> preserves the slot binding with empty display."""
    assert parse_tag(make_compact_tagged_name(slot)) == (slot, "")


@given(slot=SLOT_NUMS)
def test_slot_only_round_trip(slot: int) -> None:
    """Bare digits parse as a slot tag (documented, intentionally ambiguous)."""
    assert parse_tag(str(slot)) == (slot, "")


@given(slot=SLOT_NUMS, name=NAMES)
def test_legacy_format_still_parses(slot: int, name: str) -> None:
    """Read-only legacy [LCM:<slot>] <name> is still recognized."""
    assert parse_tag(f"[LCM:{slot}] {name}") == (slot, name)


@given(name=st.text(max_size=60))
def test_parse_tag_is_total(name: str) -> None:
    """parse_tag never raises; non-tags come back unchanged."""
    slot, friendly = parse_tag(name)
    if slot is None:
        assert friendly == name
    else:
        assert isinstance(slot, int)


@given(
    value=st.one_of(
        st.none(),
        st.booleans(),
        st.integers(),
        st.floats(allow_nan=True, allow_infinity=True),
        st.text(),
        st.lists(st.integers(), max_size=3),
    )
)
def test_parse_slot_num_is_total(value: object) -> None:
    """parse_slot_num never raises; bools are rejected, ints pass through."""
    result = parse_slot_num(value)
    assert result is None or isinstance(result, int)
    if isinstance(value, bool):
        assert result is None
    elif isinstance(value, int):
        assert result == value
