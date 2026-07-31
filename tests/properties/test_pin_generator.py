"""Property-based tests for random PIN generation."""

from __future__ import annotations

from hypothesis import given, strategies as st
import pytest

from custom_components.lock_code_manager.domain.pin_generator import (
    MAX_PIN_LENGTH,
    MIN_PIN_LENGTH,
    generate_pin,
    is_unsafe_pin,
)

LENGTHS = st.integers(min_value=MIN_PIN_LENGTH, max_value=MAX_PIN_LENGTH)
DIGITS = "0123456789"


@given(length=LENGTHS)
def test_generate_pin_returns_safe_digits_of_requested_length(length: int) -> None:
    """Generated PINs are always the right length, numeric, and never unsafe."""
    pin = generate_pin(length)
    assert len(pin) == length
    assert pin.isdigit()
    assert not is_unsafe_pin(pin)


@given(
    length=st.integers(min_value=-100, max_value=100).filter(
        lambda n: not MIN_PIN_LENGTH <= n <= MAX_PIN_LENGTH
    )
)
def test_generate_pin_rejects_out_of_range_lengths(length: int) -> None:
    """Lengths outside [MIN_PIN_LENGTH, MAX_PIN_LENGTH] raise ValueError."""
    with pytest.raises(ValueError):
        generate_pin(length)


@given(digit=st.sampled_from(DIGITS), length=LENGTHS)
def test_all_same_digits_is_unsafe(digit: str, length: int) -> None:
    """Any repdigit PIN is rejected as unsafe."""
    assert is_unsafe_pin(digit * length)


@given(
    base=st.text(alphabet=DIGITS, min_size=1, max_size=4),
    repeats=st.integers(min_value=2, max_value=4),
)
def test_repeating_subsequence_is_unsafe(base: str, repeats: int) -> None:
    """Any PIN that is a shorter block repeated (1212, 123123, ...) is unsafe."""
    assert is_unsafe_pin(base * repeats)


@given(
    start=st.integers(min_value=0, max_value=9),
    step=st.sampled_from([1, -1]),
    length=LENGTHS,
)
def test_sequential_with_wrap_is_unsafe(start: int, step: int, length: int) -> None:
    """Fully ascending/descending runs, including 9->0 / 0->9 wrap, are unsafe."""
    pin = "".join(str((start + i * step) % 10) for i in range(length))
    assert is_unsafe_pin(pin)
