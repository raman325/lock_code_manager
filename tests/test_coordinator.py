"""Test the coordinator module."""

from dataclasses import dataclass, field
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant, callback
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

    def hard_refresh_codes(self) -> dict[int, int | str]:
        """Perform hard refresh and return all codes."""
        return self.get_usercodes()

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


@dataclass(repr=False, eq=False)
class MockLockWithPush(MockLockWithHardRefresh):
    """Mock lock that supports push-based updates."""

    _supports_push: bool = field(default=True, init=False)
    _subscribe_called: bool = field(default=False, init=False)
    _unsubscribe_called: bool = field(default=False, init=False)

    @property
    def supports_push(self) -> bool:
        """Return whether this lock supports push-based updates."""
        return self._supports_push

    def subscribe_push_updates(self) -> None:
        """Subscribe to push-based value updates."""
        self._subscribe_called = True

    def unsubscribe_push_updates(self) -> None:
        """Unsubscribe from push-based value updates."""
        self._unsubscribe_called = True


async def test_drift_timer_not_created_without_hard_refresh_interval(
    hass: HomeAssistant,
):
    """Test that drift detection timer is NOT created when hard_refresh_interval is None."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

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

    # With no hard_refresh_interval, drift timer should NOT be created
    assert coordinator._drift_unsub is None


async def test_drift_timer_created_for_lock_with_hard_refresh_interval(
    hass: HomeAssistant,
):
    """Test that drift detection timer IS created when hard_refresh_interval is set."""
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

    # With hard_refresh_interval set, drift timer SHOULD be created
    assert coordinator._drift_unsub is not None


async def test_coordinator_disables_polling_for_push_enabled_lock(
    hass: HomeAssistant,
):
    """Test that coordinator disables polling when lock supports push."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock",
        config_entry=config_entry,
    )

    lock = MockLockWithPush(
        hass,
        dr.async_get(hass),
        entity_reg,
        config_entry,
        lock_entity,
    )

    coordinator = LockUsercodeUpdateCoordinator(hass, lock, config_entry)

    # When supports_push is True, update_interval should be None (polling disabled)
    assert coordinator.update_interval is None


async def test_coordinator_enables_polling_for_non_push_lock(
    hass: HomeAssistant,
):
    """Test that coordinator enables polling when lock doesn't support push."""
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

    coordinator = LockUsercodeUpdateCoordinator(hass, lock, config_entry)

    # When supports_push is False, update_interval should be set (polling enabled)
    assert coordinator.update_interval == lock.usercode_scan_interval


async def test_push_update_updates_coordinator_data(
    hass: HomeAssistant,
):
    """Test that push_update correctly updates coordinator data."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock",
        config_entry=config_entry,
    )

    lock = MockLockWithPush(
        hass,
        dr.async_get(hass),
        entity_reg,
        config_entry,
        lock_entity,
    )

    coordinator = LockUsercodeUpdateCoordinator(hass, lock, config_entry)
    coordinator.data = {1: "1111", 2: "2222"}

    # Push a single update
    coordinator.push_update({1: "9999"})

    # Verify data was updated
    assert coordinator.data[1] == "9999"
    assert coordinator.data[2] == "2222"  # Unchanged


async def test_push_update_bulk_updates(
    hass: HomeAssistant,
):
    """Test that push_update correctly handles bulk updates."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock",
        config_entry=config_entry,
    )

    lock = MockLockWithPush(
        hass,
        dr.async_get(hass),
        entity_reg,
        config_entry,
        lock_entity,
    )

    coordinator = LockUsercodeUpdateCoordinator(hass, lock, config_entry)
    coordinator.data = {1: "1111", 2: "2222", 3: "3333"}

    # Push bulk update
    coordinator.push_update({1: "9999", 3: ""})

    # Verify all updates applied
    assert coordinator.data[1] == "9999"
    assert coordinator.data[2] == "2222"  # Unchanged
    assert coordinator.data[3] == ""  # Cleared


async def test_push_update_ignores_empty_updates(
    hass: HomeAssistant,
):
    """Test that push_update ignores empty update dict."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock",
        config_entry=config_entry,
    )

    lock = MockLockWithPush(
        hass,
        dr.async_get(hass),
        entity_reg,
        config_entry,
        lock_entity,
    )

    coordinator = LockUsercodeUpdateCoordinator(hass, lock, config_entry)
    coordinator.data = {1: "1111"}

    # Track async_set_updated_data calls
    with patch.object(coordinator, "async_set_updated_data") as mock_set_updated:
        coordinator.push_update({})

        # Should not call async_set_updated_data for empty updates
        mock_set_updated.assert_not_called()


async def test_push_update_notifies_listeners(
    hass: HomeAssistant,
):
    """Test that push_update notifies coordinator listeners."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock",
        config_entry=config_entry,
    )

    lock = MockLockWithPush(
        hass,
        dr.async_get(hass),
        entity_reg,
        config_entry,
        lock_entity,
    )

    coordinator = LockUsercodeUpdateCoordinator(hass, lock, config_entry)
    coordinator.data = {1: "1111"}

    # Track listener callbacks
    listener_called = False

    @callback
    def listener():
        nonlocal listener_called
        listener_called = True

    coordinator.async_add_listener(listener)

    # Push an update
    coordinator.push_update({1: "9999"})

    # Verify listener was called
    assert listener_called


async def test_subscribe_push_updates_called_during_setup(
    hass: HomeAssistant,
):
    """Test that subscribe_push_updates is called during async_setup."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock",
        config_entry=config_entry,
    )

    lock = MockLockWithPush(
        hass,
        dr.async_get(hass),
        entity_reg,
        config_entry,
        lock_entity,
    )

    # Mock coordinator refreshes
    with (
        patch(
            "custom_components.lock_code_manager.coordinator."
            "LockUsercodeUpdateCoordinator.async_config_entry_first_refresh"
        ),
        patch(
            "custom_components.lock_code_manager.coordinator."
            "LockUsercodeUpdateCoordinator.async_refresh"
        ),
    ):
        assert not lock._subscribe_called
        await lock.async_setup(config_entry)
        assert lock._subscribe_called


async def test_unsubscribe_push_updates_called_during_unload(
    hass: HomeAssistant,
):
    """Test that unsubscribe_push_updates is called during async_unload."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock",
        config_entry=config_entry,
    )

    lock = MockLockWithPush(
        hass,
        dr.async_get(hass),
        entity_reg,
        config_entry,
        lock_entity,
    )

    # Setup first
    with (
        patch(
            "custom_components.lock_code_manager.coordinator."
            "LockUsercodeUpdateCoordinator.async_config_entry_first_refresh"
        ),
        patch(
            "custom_components.lock_code_manager.coordinator."
            "LockUsercodeUpdateCoordinator.async_refresh"
        ),
    ):
        await lock.async_setup(config_entry)

    # Unload
    assert not lock._unsubscribe_called
    await lock.async_unload(remove_permanently=False)
    assert lock._unsubscribe_called


async def test_subscribe_push_not_called_for_non_push_lock(
    hass: HomeAssistant,
):
    """Test that subscribe_push_updates is NOT called for non-push locks."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock",
        config_entry=config_entry,
    )

    lock = MockLockWithPush(
        hass,
        dr.async_get(hass),
        entity_reg,
        config_entry,
        lock_entity,
    )
    # Disable push support
    lock._supports_push = False

    # Mock coordinator refreshes
    with (
        patch(
            "custom_components.lock_code_manager.coordinator."
            "LockUsercodeUpdateCoordinator.async_config_entry_first_refresh"
        ),
        patch(
            "custom_components.lock_code_manager.coordinator."
            "LockUsercodeUpdateCoordinator.async_refresh"
        ),
    ):
        await lock.async_setup(config_entry)
        # subscribe_push_updates should NOT have been called
        assert not lock._subscribe_called


async def test_async_shutdown_cleans_up_drift_timer(
    hass: HomeAssistant,
):
    """Test that async_shutdown cleans up the drift detection timer."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock",
        config_entry=config_entry,
    )

    lock = MockLockWithPush(
        hass,
        dr.async_get(hass),
        entity_reg,
        config_entry,
        lock_entity,
    )
    lock._hard_refresh_interval = timedelta(hours=1)

    coordinator = LockUsercodeUpdateCoordinator(hass, lock, config_entry)

    # Drift timer should be set up
    assert coordinator._drift_unsub is not None

    # Shutdown coordinator
    await coordinator.async_shutdown()

    # Drift timer should be cleaned up
    assert coordinator._drift_unsub is None


async def test_drift_check_calls_hard_refresh(
    hass: HomeAssistant,
):
    """Test that _async_drift_check calls async_internal_hard_refresh_codes."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock",
        config_entry=config_entry,
    )

    lock = MockLockWithPush(
        hass,
        dr.async_get(hass),
        entity_reg,
        config_entry,
        lock_entity,
    )
    lock._hard_refresh_interval = timedelta(hours=1)

    coordinator = LockUsercodeUpdateCoordinator(hass, lock, config_entry)

    # Mock the hard refresh method
    mock_hard_refresh = AsyncMock()

    with patch.object(lock, "async_internal_hard_refresh_codes", mock_hard_refresh):
        await coordinator._async_drift_check(dt_util.utcnow())

        mock_hard_refresh.assert_called_once()
