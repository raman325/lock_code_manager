"""Test utility functions."""

import asyncio
from datetime import timedelta
import re

import pytest

from homeassistant.core import HomeAssistant

from custom_components.lock_code_manager.util import OneShotRetry, mask_pin

INSTANCE_ID = "test-instance-uuid"
LOCK = "lock.front_door"


@pytest.mark.parametrize("pin", ["1234", "5678", "0"])
def test_mask_pin_returns_hash(pin: str):
    """Test mask_pin returns a pin# prefixed 8-char lowercase hex hash."""
    result = mask_pin(pin, LOCK, INSTANCE_ID)
    assert re.fullmatch(r"pin#[0-9a-f]{8}", result), f"Unexpected format: {result}"


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


async def test_oneshot_retry_schedule_idempotent(hass: HomeAssistant):
    """Test that calling schedule() multiple times is idempotent."""
    counts = [0]

    def sync_target() -> None:
        counts[0] += 1

    retry = OneShotRetry(hass, timedelta(seconds=1), sync_target, "test retry")

    # Schedule multiple times - should only execute once
    retry.schedule()
    retry.schedule()
    retry.schedule()

    # Wait for execution
    await asyncio.sleep(1.5)

    assert counts[0] == 1


async def test_oneshot_retry_active_property(hass: HomeAssistant):
    """Test that active property is True during execution."""
    active_states = []

    async def async_target() -> None:
        active_states.append(retry.active)
        await asyncio.sleep(0.1)
        active_states.append(retry.active)

    retry = OneShotRetry(hass, timedelta(milliseconds=100), async_target, "test retry")

    # Check active before execution
    assert not retry.active

    retry.schedule()
    await asyncio.sleep(0.3)

    # Active should be True during first check, then execution completes
    assert len(active_states) == 2
    assert active_states[0] is True  # Active during execution
    # After full execution + finally block completes, should be False
    assert not retry.active


async def test_oneshot_retry_pending_property(hass: HomeAssistant):
    """Test that pending property reflects scheduled state."""

    async def async_target() -> None:
        await asyncio.sleep(0.1)

    retry = OneShotRetry(hass, timedelta(seconds=1), async_target, "test retry")

    # Initially not pending
    assert not retry.pending

    # After schedule, pending is True
    retry.schedule()
    assert retry.pending

    # After execution, pending is False again
    await asyncio.sleep(1.2)
    assert not retry.pending


async def test_oneshot_retry_schedule_when_already_pending(hass: HomeAssistant):
    """Test that schedule() is no-op when retry is already pending."""
    counts = [0]

    def sync_target() -> None:
        counts[0] += 1

    retry = OneShotRetry(hass, timedelta(seconds=1), sync_target, "test retry")

    # First schedule
    retry.schedule()

    # Second schedule should be no-op (early return)
    retry.schedule()

    await asyncio.sleep(1.2)

    # Should only execute once
    assert counts[0] == 1


async def test_oneshot_retry_cancel(hass: HomeAssistant):
    """Test that cancel() prevents scheduled retry."""
    counts = [0]

    def sync_target() -> None:
        counts[0] += 1

    retry = OneShotRetry(hass, timedelta(seconds=1), sync_target, "test retry")

    # Schedule retry
    retry.schedule()
    assert retry.pending

    # Cancel retry
    retry.cancel()
    assert not retry.pending

    # Wait to ensure it doesn't execute
    await asyncio.sleep(1.2)

    # Should not have executed
    assert counts[0] == 0
