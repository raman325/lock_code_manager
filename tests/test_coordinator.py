"""Test the coordinator module."""

from dataclasses import dataclass, field
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from custom_components.lock_code_manager.const import DOMAIN
from custom_components.lock_code_manager.coordinator import (
    LockUsercodeUpdateCoordinator,
)
from custom_components.lock_code_manager.exceptions import LockDisconnected
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
    mock_lock_config_entry,
):
    """Test that drift detection timer is NOT created when hard_refresh_interval is None."""
    entity_reg = er.async_get(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock",
        config_entry=mock_lock_config_entry,
    )

    lock = VirtualLock(
        hass,
        dr.async_get(hass),
        entity_reg,
        mock_lock_config_entry,
        lock_entity,
    )

    # VirtualLock doesn't override hard_refresh_interval, so it should be None
    assert lock.hard_refresh_interval is None

    coordinator = LockUsercodeUpdateCoordinator(hass, lock, mock_lock_config_entry)

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
    mock_lock_config_entry,
):
    """Test that subscribe_push_updates is called during async_setup."""
    entity_reg = er.async_get(hass)
    await hass.config_entries.async_reload(mock_lock_config_entry.entry_id)
    await hass.async_block_till_done()

    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock",
        config_entry=mock_lock_config_entry,
    )

    lock = MockLockWithPush(
        hass,
        dr.async_get(hass),
        entity_reg,
        mock_lock_config_entry,
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
        await lock.async_setup(mock_lock_config_entry)
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


async def test_coordinator_lock_property(
    hass: HomeAssistant,
):
    """Test that coordinator.lock returns the lock instance."""
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

    # Verify lock property returns the lock
    assert coordinator.lock is lock


async def test_drift_check_skips_before_initial_success(
    hass: HomeAssistant,
):
    """Test that _async_drift_check skips if initial data hasn't loaded."""
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
    # Simulate no successful update yet
    coordinator.last_update_success = False

    mock_hard_refresh = AsyncMock()
    with patch.object(lock, "async_internal_hard_refresh_codes", mock_hard_refresh):
        await coordinator._async_drift_check(dt_util.utcnow())

        # Should not call hard refresh when last_update_success is False
        mock_hard_refresh.assert_not_called()


async def test_drift_check_handles_hard_refresh_error(
    hass: HomeAssistant,
):
    """Test that _async_drift_check handles hard refresh errors gracefully."""
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
    coordinator.last_update_success = True
    coordinator.data = {1: "1234"}

    # Mock hard refresh to raise an exception
    mock_hard_refresh = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(lock, "async_internal_hard_refresh_codes", mock_hard_refresh):
        # Should not raise, should handle gracefully
        await coordinator._async_drift_check(dt_util.utcnow())

        # Data should remain unchanged
        assert coordinator.data == {1: "1234"}


# =========================================================================
# Sync Operation Tests
# =========================================================================


async def test_get_sync_state_returns_none_for_unknown_slot(
    hass: HomeAssistant,
):
    """Test that get_sync_state returns None for slots not in _sync_state."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock", "test", "test_lock", config_entry=config_entry
    )

    lock = MockLockWithHardRefresh(
        hass, dr.async_get(hass), entity_reg, config_entry, lock_entity
    )
    coordinator = LockUsercodeUpdateCoordinator(hass, lock, config_entry)

    # No sync state set, should return None
    assert coordinator.get_sync_state(1) is None
    assert coordinator.get_sync_state(99) is None


async def test_get_sync_state_returns_correct_state(
    hass: HomeAssistant,
):
    """Test that get_sync_state returns the correct sync state."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock", "test", "test_lock", config_entry=config_entry
    )

    lock = MockLockWithHardRefresh(
        hass, dr.async_get(hass), entity_reg, config_entry, lock_entity
    )
    coordinator = LockUsercodeUpdateCoordinator(hass, lock, config_entry)

    # Set internal state directly
    coordinator._sync_state[1] = True
    coordinator._sync_state[2] = False

    assert coordinator.get_sync_state(1) is True
    assert coordinator.get_sync_state(2) is False


async def test_mark_synced_sets_state_and_notifies(
    hass: HomeAssistant,
):
    """Test that mark_synced sets sync state and notifies listeners."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock", "test", "test_lock", config_entry=config_entry
    )

    lock = MockLockWithHardRefresh(
        hass, dr.async_get(hass), entity_reg, config_entry, lock_entity
    )
    coordinator = LockUsercodeUpdateCoordinator(hass, lock, config_entry)
    coordinator.data = {1: "1234"}

    listener_called = False

    @callback
    def listener():
        nonlocal listener_called
        listener_called = True

    coordinator.async_add_listener(listener)

    # Mark slot 1 as synced
    coordinator.mark_synced(1)

    assert coordinator.get_sync_state(1) is True
    assert listener_called


async def test_mark_synced_cancels_pending_retry(
    hass: HomeAssistant,
):
    """Test that mark_synced cancels any pending retry for the slot."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock", "test", "test_lock", config_entry=config_entry
    )

    lock = MockLockWithHardRefresh(
        hass, dr.async_get(hass), entity_reg, config_entry, lock_entity
    )
    coordinator = LockUsercodeUpdateCoordinator(hass, lock, config_entry)
    coordinator.data = {1: "1234"}

    # Simulate a pending retry
    cancel_called = False

    def fake_cancel():
        nonlocal cancel_called
        cancel_called = True

    coordinator._pending_retries[1] = fake_cancel

    # Mark as synced should cancel the pending retry
    coordinator.mark_synced(1)

    assert cancel_called
    assert 1 not in coordinator._pending_retries


async def test_mark_synced_no_op_if_already_synced(
    hass: HomeAssistant,
):
    """Test that mark_synced doesn't notify if already synced."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock", "test", "test_lock", config_entry=config_entry
    )

    lock = MockLockWithHardRefresh(
        hass, dr.async_get(hass), entity_reg, config_entry, lock_entity
    )
    coordinator = LockUsercodeUpdateCoordinator(hass, lock, config_entry)
    coordinator.data = {1: "1234"}
    coordinator._sync_state[1] = True  # Already synced

    listener_called = False

    @callback
    def listener():
        nonlocal listener_called
        listener_called = True

    coordinator.async_add_listener(listener)

    # Mark synced again - should be a no-op
    coordinator.mark_synced(1)

    assert not listener_called


async def test_async_request_sync_set_operation_success(
    hass: HomeAssistant,
):
    """Test async_request_sync for a successful set operation."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock", "test", "test_lock", config_entry=config_entry
    )

    lock = MockLockWithHardRefresh(
        hass, dr.async_get(hass), entity_reg, config_entry, lock_entity
    )
    coordinator = LockUsercodeUpdateCoordinator(hass, lock, config_entry)
    coordinator.data = {1: ""}

    mock_set = AsyncMock()
    mock_refresh = AsyncMock()

    with (
        patch.object(lock, "async_internal_set_usercode", mock_set),
        patch.object(coordinator, "async_request_refresh", mock_refresh),
    ):
        result = await coordinator.async_request_sync(1, "set", "1234", "Test User")

    assert result is True
    mock_set.assert_called_once_with(1, "1234", "Test User")
    mock_refresh.assert_called_once()


async def test_async_request_sync_clear_operation_success(
    hass: HomeAssistant,
):
    """Test async_request_sync for a successful clear operation."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock", "test", "test_lock", config_entry=config_entry
    )

    lock = MockLockWithHardRefresh(
        hass, dr.async_get(hass), entity_reg, config_entry, lock_entity
    )
    coordinator = LockUsercodeUpdateCoordinator(hass, lock, config_entry)
    coordinator.data = {1: "1234"}

    mock_clear = AsyncMock()
    mock_refresh = AsyncMock()

    with (
        patch.object(lock, "async_internal_clear_usercode", mock_clear),
        patch.object(coordinator, "async_request_refresh", mock_refresh),
    ):
        result = await coordinator.async_request_sync(1, "clear")

    assert result is True
    mock_clear.assert_called_once_with(1)
    mock_refresh.assert_called_once()


async def test_async_request_sync_marks_out_of_sync_immediately(
    hass: HomeAssistant,
):
    """Test that async_request_sync marks slot as out of sync before operation."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock", "test", "test_lock", config_entry=config_entry
    )

    lock = MockLockWithHardRefresh(
        hass, dr.async_get(hass), entity_reg, config_entry, lock_entity
    )
    coordinator = LockUsercodeUpdateCoordinator(hass, lock, config_entry)
    coordinator.data = {1: "1234"}
    coordinator._sync_state[1] = True  # Start as synced

    sync_state_during_operation = None

    async def capture_state(*args, **kwargs):
        nonlocal sync_state_during_operation
        sync_state_during_operation = coordinator.get_sync_state(1)

    with (
        patch.object(lock, "async_internal_set_usercode", capture_state),
        patch.object(coordinator, "async_request_refresh", AsyncMock()),
    ):
        await coordinator.async_request_sync(1, "set", "5678")

    # During the operation, sync state should have been False
    assert sync_state_during_operation is False


async def test_async_request_sync_cancels_existing_retry(
    hass: HomeAssistant,
):
    """Test that async_request_sync cancels any pending retry for the slot."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock", "test", "test_lock", config_entry=config_entry
    )

    lock = MockLockWithHardRefresh(
        hass, dr.async_get(hass), entity_reg, config_entry, lock_entity
    )
    coordinator = LockUsercodeUpdateCoordinator(hass, lock, config_entry)
    coordinator.data = {1: "1234"}

    # Simulate a pending retry
    cancel_called = False

    def fake_cancel():
        nonlocal cancel_called
        cancel_called = True

    coordinator._pending_retries[1] = fake_cancel

    with (
        patch.object(lock, "async_internal_set_usercode", AsyncMock()),
        patch.object(coordinator, "async_request_refresh", AsyncMock()),
    ):
        await coordinator.async_request_sync(1, "set", "5678")

    assert cancel_called
    # After success, no new retry should be pending
    assert 1 not in coordinator._pending_retries


async def test_async_request_sync_schedules_retry_on_lock_disconnected(
    hass: HomeAssistant,
):
    """Test that async_request_sync schedules retry on LockDisconnected."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock", "test", "test_lock", config_entry=config_entry
    )

    lock = MockLockWithHardRefresh(
        hass, dr.async_get(hass), entity_reg, config_entry, lock_entity
    )
    coordinator = LockUsercodeUpdateCoordinator(hass, lock, config_entry)
    coordinator.data = {1: "1234"}

    mock_set = AsyncMock(side_effect=LockDisconnected("Lock offline"))

    with patch.object(lock, "async_internal_set_usercode", mock_set):
        result = await coordinator.async_request_sync(1, "set", "5678")

    assert result is False
    # A retry should be scheduled
    assert 1 in coordinator._pending_retries


async def test_async_request_sync_raises_on_missing_usercode_for_set(
    hass: HomeAssistant,
):
    """Test that async_request_sync raises ValueError if usercode is None for set."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock", "test", "test_lock", config_entry=config_entry
    )

    lock = MockLockWithHardRefresh(
        hass, dr.async_get(hass), entity_reg, config_entry, lock_entity
    )
    coordinator = LockUsercodeUpdateCoordinator(hass, lock, config_entry)
    coordinator.data = {1: ""}

    with pytest.raises(ValueError, match="usercode is required"):
        await coordinator.async_request_sync(1, "set", None)


async def test_cancel_retry_cancels_and_removes(
    hass: HomeAssistant,
):
    """Test that _cancel_retry cancels the callback and removes from dict."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock", "test", "test_lock", config_entry=config_entry
    )

    lock = MockLockWithHardRefresh(
        hass, dr.async_get(hass), entity_reg, config_entry, lock_entity
    )
    coordinator = LockUsercodeUpdateCoordinator(hass, lock, config_entry)

    cancel_called = False

    def fake_cancel():
        nonlocal cancel_called
        cancel_called = True

    coordinator._pending_retries[1] = fake_cancel

    coordinator._cancel_retry(1)

    assert cancel_called
    assert 1 not in coordinator._pending_retries


async def test_cancel_retry_no_op_for_unknown_slot(
    hass: HomeAssistant,
):
    """Test that _cancel_retry is a no-op for slots without pending retries."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock", "test", "test_lock", config_entry=config_entry
    )

    lock = MockLockWithHardRefresh(
        hass, dr.async_get(hass), entity_reg, config_entry, lock_entity
    )
    coordinator = LockUsercodeUpdateCoordinator(hass, lock, config_entry)

    # Should not raise
    coordinator._cancel_retry(99)


async def test_new_request_replaces_pending_retry(
    hass: HomeAssistant,
):
    """Test that a new sync request replaces a pending retry with new operation."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock", "test", "test_lock", config_entry=config_entry
    )

    lock = MockLockWithHardRefresh(
        hass, dr.async_get(hass), entity_reg, config_entry, lock_entity
    )
    coordinator = LockUsercodeUpdateCoordinator(hass, lock, config_entry)
    coordinator.data = {1: "1234"}

    # First request fails, schedules retry for "set"
    mock_set = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(lock, "async_internal_set_usercode", mock_set):
        await coordinator.async_request_sync(1, "set", "5678")

    old_retry = coordinator._pending_retries.get(1)
    assert old_retry is not None

    # Second request for "clear" should replace the "set" retry
    mock_clear = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(lock, "async_internal_clear_usercode", mock_clear):
        await coordinator.async_request_sync(1, "clear")

    # Old retry should have been cancelled and replaced
    new_retry = coordinator._pending_retries.get(1)
    assert new_retry is not None
    assert new_retry is not old_retry
