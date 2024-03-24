"""Test the Z-Wave JS lock platform."""

from homeassistant.core import HomeAssistant

SCHLAGE_BE469_LOCK_ENTITY = "lock.touchscreen_deadbolt"


async def test_door_lock(hass: HomeAssistant) -> None:
    """Test a lock entity with door lock command class."""
    pass
