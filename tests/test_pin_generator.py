"""Tests for the PIN generator."""

from unittest.mock import patch

import pytest

from custom_components.lock_code_manager.model import pin_generator
from custom_components.lock_code_manager.model.pin_generator import (
    DEFAULT_PIN_LENGTH,
    generate_pin,
    is_unsafe_pin,
)


class TestIsUnsafePin:
    """is_unsafe_pin should reject all flagged patterns and accept reasonable ones."""

    # Repdigits, plain runs and repeating blocks are covered by the
    # properties in tests/properties/test_pin_generator.py. The wrap-around
    # runs stay here until the properties pin them with @example.
    @pytest.mark.parametrize("pin", ["7890", "8901"])
    def test_sequential_ascending_unsafe(self, pin: str) -> None:
        assert is_unsafe_pin(pin)

    @pytest.mark.parametrize("pin", ["1098"])
    def test_sequential_descending_unsafe(self, pin: str) -> None:
        assert is_unsafe_pin(pin)

    # Hard-coded rather than read from COMMON_WEAK_PINS_4, so dropping one
    # from the list fails here. The rest of the list is caught by the
    # same-digit, repeating-block or sequence rule before the list is read.
    @pytest.mark.parametrize("pin", ["1004", "1122", "2000", "2001", "6969"])
    def test_common_weak_pins_unsafe(self, pin: str) -> None:
        assert is_unsafe_pin(pin)

    @pytest.mark.parametrize("pin", ["5739", "8027", "4691", "3582"])
    def test_random_safe_pins_accepted(self, pin: str) -> None:
        assert not is_unsafe_pin(pin)

    def test_longer_pin_with_partial_sequence_accepted(self) -> None:
        """Sequence rule is fully-sequential: a single break should accept."""
        assert not is_unsafe_pin("12349")

    def test_longer_pin_strict_repeating_pattern_rejected(self) -> None:
        """Repeating-pattern rule rejects sub-sequences that tile the PIN."""
        assert is_unsafe_pin("123123")
        assert not is_unsafe_pin("142857")

    def test_empty_string_unsafe(self) -> None:
        """An empty PIN is treated as unsafe (defensive)."""
        assert is_unsafe_pin("")


class TestGeneratePin:
    """generate_pin should return digits-only strings of requested length, never unsafe."""

    def test_default_length_is_4(self) -> None:
        pin = generate_pin()
        assert len(pin) == DEFAULT_PIN_LENGTH
        assert pin.isdigit()

    @pytest.mark.parametrize("length", [4, 12])
    def test_returns_requested_length(self, length: int) -> None:
        pin = generate_pin(length)
        assert len(pin) == length
        assert pin.isdigit()

    @pytest.mark.parametrize("length", [3, 13])
    def test_invalid_length_raises(self, length: int) -> None:
        with pytest.raises(ValueError):
            generate_pin(length)

    def test_generated_pins_pass_unsafe_filter(self) -> None:
        """Statistical check: every output across many calls must pass the filter."""
        for _ in range(500):
            assert not is_unsafe_pin(generate_pin())

    def test_generated_pins_have_entropy(self) -> None:
        """Statistical check: the generator should not be returning a fixed value."""
        pins = {generate_pin() for _ in range(50)}
        assert len(pins) > 30

    def test_gives_up_after_100_failed_attempts(self) -> None:
        """A filter that rejects every candidate raises RuntimeError, not an infinite loop."""
        with patch.object(pin_generator, "is_unsafe_pin", return_value=True):
            with pytest.raises(RuntimeError, match="after 100 attempts"):
                generate_pin()


def _digits(*pins: str):
    """Patch the random source to spell out ``pins``, one after another."""
    return patch.object(
        pin_generator.secrets,
        "randbelow",
        side_effect=[int(digit) for pin in pins for digit in pin],
    )


def test_a_pin_to_avoid_is_drawn_again() -> None:
    """A candidate in ``avoid`` is not returned, as an unsafe one is not."""
    with _digits("2468", "1397"):
        assert generate_pin(4, avoid={"2468"}) == "1397"


def test_nothing_to_avoid_keeps_the_first_safe_pin() -> None:
    """Without ``avoid`` the generator is what it was."""
    with _digits("2468"):
        assert generate_pin(4) == "2468"
