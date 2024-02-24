"""Test the Z-Wave JS lock platform."""

import pytest
from homeassistant.core import HomeAssistant

SCHLAGE_BE469_LOCK_ENTITY = "lock.touchscreen_deadbolt"


async def test_door_lock(
    hass: HomeAssistant,
    client,
    lock_schlage_be469,
    integration,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Test a lock entity with door lock command class."""
    pass
