"""Test the coordinator module."""

from dataclasses import dataclass, field
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.lock_code_manager.const import DOMAIN
from custom_components.lock_code_manager.coordinator import (
    LockUsercodeUpdateCoordinator,
)
from custom_components.lock_code_manager.providers._base import BaseLock
from custom_components.lock_code_manager.providers.virtual import VirtualLock


@dataclass(repr=False, eq=False)
class MockLockWithHardRefresh(BaseLock):
    """Mock lock with configurable hard_refresh_interval."""

    _hard_refresh_interval: timedelta | None = field(default=None, init=False)
    _is_connected: bool = field(default=True, init=False)

    @property
    def domain(self) -> str:
        """Return integration domain."""
        return "test"

    @property
    def hard_refresh_interval(self) -> timedelta | None:
        """Return configurable hard refresh interval."""
        return self._hard_refresh_interval

    def is_connection_up(self) -> bool:
        """Return whether connection to lock is up."""
        return self._is_connected

    def hard_refresh_codes(self) -> None:
        """Perform hard refresh of all codes."""
        pass

    def get_usercodes(self) -> dict[int, int | str]:
        """Get dictionary of code slots and usercodes."""
        return {}

    def set_usercode(
        self, code_slot: int, usercode: int | str, name: str | None = None
    ) -> bool:
        """Set a usercode on a code slot."""
        return True

    def clear_usercode(self, code_slot: int) -> bool:
        """Clear a usercode on a code slot."""
        return True


async def test_coordinator_should_hard_refresh_disabled_by_default(
    hass: HomeAssistant,
):
    """Test that _should_hard_refresh returns False when hard_refresh_interval is None."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    # Create a proper registry entry
    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock",
        config_entry=config_entry,
    )

    lock = VirtualLock(
        hass,
        dr.async_get(hass),
        entity_reg,
        config_entry,
        lock_entity,
    )

    # VirtualLock doesn't override hard_refresh_interval, so it should be None
    assert lock.hard_refresh_interval is None

    coordinator = LockUsercodeUpdateCoordinator(hass, lock, config_entry)

    # With no hard_refresh_interval, _should_hard_refresh should return False
    assert coordinator._should_hard_refresh() is False


async def test_coordinator_should_hard_refresh_first_time(
    hass: HomeAssistant,
):
    """Test that _should_hard_refresh returns True on first call when interval is set."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock",
        config_entry=config_entry,
    )

    lock = MockLockWithHardRefresh(
        hass,
        dr.async_get(hass),
        entity_reg,
        config_entry,
        lock_entity,
    )
    lock._hard_refresh_interval = timedelta(hours=1)

    coordinator = LockUsercodeUpdateCoordinator(hass, lock, config_entry)

    # First call should return True (no previous refresh)
    assert coordinator._last_hard_refresh is None
    assert coordinator._should_hard_refresh() is True


async def test_coordinator_should_hard_refresh_after_interval(
    hass: HomeAssistant,
):
    """Test that _should_hard_refresh returns True after interval has elapsed."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock",
        config_entry=config_entry,
    )

    lock = MockLockWithHardRefresh(
        hass,
        dr.async_get(hass),
        entity_reg,
        config_entry,
        lock_entity,
    )
    lock._hard_refresh_interval = timedelta(hours=1)

    coordinator = LockUsercodeUpdateCoordinator(hass, lock, config_entry)

    # Set last_hard_refresh to 2 hours ago
    two_hours_ago = dt_util.utcnow() - timedelta(hours=2)
    coordinator._last_hard_refresh = two_hours_ago

    # With 1 hour interval, should return True (2 hours > 1 hour)
    assert coordinator._should_hard_refresh() is True


async def test_coordinator_should_hard_refresh_before_interval(
    hass: HomeAssistant,
):
    """Test that _should_hard_refresh returns False before interval has elapsed."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock",
        config_entry=config_entry,
    )

    lock = MockLockWithHardRefresh(
        hass,
        dr.async_get(hass),
        entity_reg,
        config_entry,
        lock_entity,
    )
    lock._hard_refresh_interval = timedelta(hours=1)

    coordinator = LockUsercodeUpdateCoordinator(hass, lock, config_entry)

    # Set last_hard_refresh to 30 minutes ago
    thirty_min_ago = dt_util.utcnow() - timedelta(minutes=30)
    coordinator._last_hard_refresh = thirty_min_ago

    # With 1 hour interval, should return False (30 min < 1 hour)
    assert coordinator._should_hard_refresh() is False


async def test_coordinator_triggers_hard_refresh_on_update(
    hass: HomeAssistant,
):
    """Test that coordinator triggers hard refresh when interval has elapsed."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock",
        config_entry=config_entry,
    )

    lock = MockLockWithHardRefresh(
        hass,
        dr.async_get(hass),
        entity_reg,
        config_entry,
        lock_entity,
    )
    lock._hard_refresh_interval = timedelta(hours=1)

    # Set up lock with coordinator (but don't do full setup which would refresh)
    lock.coordinator = LockUsercodeUpdateCoordinator(hass, lock, config_entry)

    # Mock the hard refresh method
    mock_hard_refresh = AsyncMock()

    with patch.object(lock, "async_internal_hard_refresh_codes", mock_hard_refresh):
        # First update should trigger hard refresh (no previous refresh)
        lock.coordinator._last_hard_refresh = None
        await lock.coordinator.async_get_usercodes()

        mock_hard_refresh.assert_called_once()
        assert lock.coordinator._last_hard_refresh is not None


async def test_coordinator_skips_hard_refresh_within_interval(
    hass: HomeAssistant,
):
    """Test that coordinator skips hard refresh when within interval."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock",
        config_entry=config_entry,
    )

    lock = MockLockWithHardRefresh(
        hass,
        dr.async_get(hass),
        entity_reg,
        config_entry,
        lock_entity,
    )
    lock._hard_refresh_interval = timedelta(hours=1)

    # Set up lock with coordinator
    lock.coordinator = LockUsercodeUpdateCoordinator(hass, lock, config_entry)

    # Mock the hard refresh method
    mock_hard_refresh = AsyncMock()

    # Set last refresh to recent time
    lock.coordinator._last_hard_refresh = dt_util.utcnow() - timedelta(minutes=30)

    with patch.object(lock, "async_internal_hard_refresh_codes", mock_hard_refresh):
        await lock.coordinator.async_get_usercodes()

        # Hard refresh should NOT be called (within interval)
        mock_hard_refresh.assert_not_called()
