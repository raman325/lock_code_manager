"""Tests for the PIN generator."""

import pytest

from custom_components.lock_code_manager.pin_generator import (
    COMMON_WEAK_PINS_4,
    DEFAULT_PIN_LENGTH,
    generate_pin,
    is_unsafe_pin,
)


class TestIsUnsafePin:
    """is_unsafe_pin should reject all flagged patterns and accept reasonable ones."""

    @pytest.mark.parametrize("pin", ["0000", "1111", "5555", "9999"])
    def test_all_same_digits_unsafe(self, pin: str) -> None:
        assert is_unsafe_pin(pin)

    @pytest.mark.parametrize("pin", ["1234", "2345", "6789", "7890", "8901"])
    def test_sequential_ascending_unsafe(self, pin: str) -> None:
        assert is_unsafe_pin(pin)

    @pytest.mark.parametrize("pin", ["9876", "5432", "1098", "3210"])
    def test_sequential_descending_unsafe(self, pin: str) -> None:
        assert is_unsafe_pin(pin)

    @pytest.mark.parametrize("pin", ["1212", "1010", "7878", "121212", "123123"])
    def test_repeating_pattern_unsafe(self, pin: str) -> None:
        assert is_unsafe_pin(pin)

    @pytest.mark.parametrize("pin", sorted(COMMON_WEAK_PINS_4))
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

    @pytest.mark.parametrize("length", [4, 5, 6, 8, 10, 12])
    def test_returns_requested_length(self, length: int) -> None:
        pin = generate_pin(length)
        assert len(pin) == length
        assert pin.isdigit()

    @pytest.mark.parametrize("length", [3, 13, 0, -1])
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
