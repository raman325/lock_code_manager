"""
Property-based tests for the PIN masking and de-obfuscation round trip.

Masking is what lets users paste debug logs into a public issue, and
de-obfuscation is what lets the maintainer read them back. Both directions
matter: a mask that leaks is a disclosure, and a round trip that loses the
value makes the whole scheme pointless. Example-based tests pin a handful of
PINs; these pin the invariants over the whole input space.
"""

from __future__ import annotations

import re

from hypothesis import given, strategies as st

from custom_components.lock_code_manager.domain.util import (
    deobfuscate_pins,
    mask_pin,
)

# Real PINs are digit strings; the lock firmware cannot store anything else.
PINS = st.text(alphabet="0123456789", min_size=1, max_size=12)
SLOTS = st.integers(min_value=1, max_value=9999)
INSTANCE_IDS = st.text(alphabet="0123456789abcdef", min_size=8, max_size=32)

TOKEN_RE = re.compile(r"^pin#[0-9a-f]{8}$")


@given(pin=PINS, slot=SLOTS, instance_id=INSTANCE_IDS)
def test_mask_emits_the_documented_token_shape(
    pin: str, slot: int, instance_id: str
) -> None:
    """Every masked PIN is the fixed ``pin#`` + 8 lowercase hex form."""
    assert TOKEN_RE.match(mask_pin(pin, slot, instance_id))


@given(pin=PINS, slot=SLOTS, instance_id=INSTANCE_IDS)
def test_mask_does_not_leak_pin_length(pin: str, slot: int, instance_id: str) -> None:
    """
    The token is a constant width whatever the PIN's length.

    A variable-width token would disclose the PIN's length, which for a
    4-to-8-digit keypad code is a meaningful chunk of the search space.
    """
    assert len(mask_pin(pin, slot, instance_id)) == len("pin#") + 8


@given(pin=PINS, slot=SLOTS, instance_id=INSTANCE_IDS)
def test_mask_is_deterministic(pin: str, slot: int, instance_id: str) -> None:
    """
    The same PIN on the same slot always masks identically.

    This is what makes a log readable: one PIN reads as one token throughout,
    so the reader can follow it across lines without ever seeing its value.
    """
    assert mask_pin(pin, slot, instance_id) == mask_pin(pin, slot, instance_id)


@given(pin=PINS, slot=SLOTS, other_slot=SLOTS, instance_id=INSTANCE_IDS)
def test_same_pin_on_different_slots_masks_differently(
    pin: str, slot: int, other_slot: int, instance_id: str
) -> None:
    """
    The slot is part of the salt, so one PIN in two slots reads as two tokens.

    Without this, a log would reveal that two slots share a PIN.
    """
    if slot == other_slot:
        return
    assert mask_pin(pin, slot, instance_id) != mask_pin(pin, other_slot, instance_id)


@given(slot=SLOTS, instance_id=INSTANCE_IDS)
def test_empty_pin_is_not_masked_into_a_token(slot: int, instance_id: str) -> None:
    """A missing PIN reads as ``<empty>``, never as a token implying a value."""
    for empty in (None, ""):
        assert mask_pin(empty, slot, instance_id) == "<empty>"


@given(pin=PINS, slot=SLOTS, instance_id=INSTANCE_IDS)
def test_round_trip_recovers_the_pin(pin: str, slot: int, instance_id: str) -> None:
    """A masked PIN embedded in log text is recovered exactly by the table."""
    token = mask_pin(pin, slot, instance_id)
    text = f"Setting usercode on lock.front slot {slot} (pin={token}, source=sync)"

    deobfuscated, summary = deobfuscate_pins(text, {token: pin})

    assert deobfuscated == text.replace(token, pin)
    assert summary["total"] == 1
    assert summary["matched"] == 1
    assert summary["unmatched_tokens"] == []


@given(pin=PINS, slot=SLOTS, instance_id=INSTANCE_IDS)
def test_unknown_token_is_left_verbatim(pin: str, slot: int, instance_id: str) -> None:
    """
    A token with no table entry survives unchanged and is reported.

    The output stays paste-compatible with the original log, which is what
    makes it safe to run against a log whose PINs have since been rotated.
    """
    token = mask_pin(pin, slot, instance_id)
    text = f"slot {slot} pin={token}"

    deobfuscated, summary = deobfuscate_pins(text, {})

    assert deobfuscated == text
    assert summary["total"] == 1
    assert summary["matched"] == 0
    assert summary["unmatched_tokens"] == [token]


@given(text=st.text(max_size=200))
def test_deobfuscate_is_total_and_summary_is_coherent(text: str) -> None:
    """
    Arbitrary text never raises, and the summary always describes the output.

    This runs over user-supplied log paste, so it has to survive anything.
    """
    deobfuscated, summary = deobfuscate_pins(text, {})

    # With an empty table nothing can be substituted.
    assert deobfuscated == text
    assert summary["matched"] == 0
    assert summary["matched"] <= summary["total"]
    assert summary["unmatched_tokens"] == sorted(set(summary["unmatched_tokens"]))
    assert len(summary["unmatched_tokens"]) <= summary["total"]
