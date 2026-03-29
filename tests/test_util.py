"""Test utility functions."""

import pytest

from custom_components.lock_code_manager.util import mask_pin

INSTANCE_ID = "test-instance-uuid"
LOCK = "lock.front_door"


@pytest.mark.parametrize(
    ("pin", "expected_prefix"),
    [
        ("1234", "pin#"),
        ("5678", "pin#"),
        ("0", "pin#"),
    ],
)
def test_mask_pin_returns_hash(pin: str, expected_prefix: str):
    """Test mask_pin returns a pin# prefixed 8-char hex hash."""
    result = mask_pin(pin, LOCK, INSTANCE_ID)
    assert result.startswith(expected_prefix)
    assert len(result) == 12  # "pin#" + 8 hex chars


def test_mask_pin_deterministic():
    """Test same PIN + lock always produces the same hash."""
    assert mask_pin("1234", LOCK, INSTANCE_ID) == mask_pin("1234", LOCK, INSTANCE_ID)


def test_mask_pin_differs_by_lock():
    """Test same PIN on different locks produces different hashes."""
    assert mask_pin("1234", "lock.front", INSTANCE_ID) != mask_pin(
        "1234", "lock.back", INSTANCE_ID
    )


def test_mask_pin_differs_by_pin():
    """Test different PINs on the same lock produce different hashes."""
    assert mask_pin("1234", LOCK, INSTANCE_ID) != mask_pin("5678", LOCK, INSTANCE_ID)


def test_mask_pin_differs_by_instance():
    """Test same PIN+lock on different instances produces different hashes."""
    assert mask_pin("1234", LOCK, "instance-a") != mask_pin("1234", LOCK, "instance-b")


@pytest.mark.parametrize(
    ("pin", "expected"),
    [
        (None, "<empty>"),
        ("", "<empty>"),
    ],
)
def test_mask_pin_empty(pin: str | None, expected: str):
    """Test mask_pin returns <empty> for None and empty string."""
    assert mask_pin(pin, LOCK, INSTANCE_ID) == expected
