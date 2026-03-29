"""Test utility functions."""

import pytest

from custom_components.lock_code_manager.util import mask_pin, set_instance_id


@pytest.fixture(autouse=True)
def _setup_instance_id():
    """Set a test instance ID for all tests."""
    set_instance_id("test-instance-uuid")
    yield
    set_instance_id("")


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
    result = mask_pin(pin, "lock.front_door")
    assert result.startswith(expected_prefix)
    assert len(result) == 12  # "pin#" + 8 hex chars


def test_mask_pin_deterministic():
    """Test same PIN + lock always produces the same hash."""
    a = mask_pin("1234", "lock.front_door")
    b = mask_pin("1234", "lock.front_door")
    assert a == b


def test_mask_pin_differs_by_lock():
    """Test same PIN on different locks produces different hashes."""
    a = mask_pin("1234", "lock.front_door")
    b = mask_pin("1234", "lock.back_door")
    assert a != b


def test_mask_pin_differs_by_pin():
    """Test different PINs on the same lock produce different hashes."""
    a = mask_pin("1234", "lock.front_door")
    b = mask_pin("5678", "lock.front_door")
    assert a != b


def test_mask_pin_differs_by_instance():
    """Test same PIN+lock on different instances produces different hashes."""
    set_instance_id("instance-a")
    a = mask_pin("1234", "lock.front_door")
    set_instance_id("instance-b")
    b = mask_pin("1234", "lock.front_door")
    assert a != b


@pytest.mark.parametrize(
    ("pin", "expected"),
    [
        (None, "<empty>"),
        ("", "<empty>"),
    ],
)
def test_mask_pin_empty(pin: str | None, expected: str):
    """Test mask_pin returns <empty> for None and empty string."""
    assert mask_pin(pin, "lock.front_door") == expected
