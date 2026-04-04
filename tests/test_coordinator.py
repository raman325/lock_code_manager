"""Test the coordinator module."""

from dataclasses import dataclass, field
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.issue_registry import async_get as async_get_issue_registry
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from custom_components.lock_code_manager.const import (
    BACKOFF_FAILURE_THRESHOLD,
    BACKOFF_INITIAL_SECONDS,
    BACKOFF_MAX_SECONDS,
    DOMAIN,
    POLL_FAILURE_ALERT_THRESHOLD,
)
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

    def is_integration_connected(self) -> bool:
        """Return whether the integration's client/driver/broker is connected."""
        return self._is_connected

    def hard_refresh_codes(self) -> dict[int, str | None]:
        """Perform hard refresh and return all codes."""
        return self.get_usercodes()

    def get_usercodes(self) -> dict[int, str | None]:
        """Get dictionary of code slots and usercodes."""
        return {}

    def set_usercode(
        self, code_slot: int, usercode: str, name: str | None = None
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

    def setup_push_subscription(self) -> None:
        """Subscribe to push-based value updates."""
        self._subscribe_called = True

    def teardown_push_subscription(self) -> None:
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
    listener_called = [False]

    @callback
    def listener():
        listener_called[0] = True

    coordinator.async_add_listener(listener)

    # Push an update
    coordinator.push_update({1: "9999"})

    # Verify listener was called
    assert listener_called[0]


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
        await lock.async_setup_internal(mock_lock_config_entry)
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
        await lock.async_setup_internal(config_entry)

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
        await lock.async_setup_internal(config_entry)
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


# --- Backoff tests ---


def _create_poll_coordinator(
    hass: HomeAssistant,
) -> tuple[LockUsercodeUpdateCoordinator, MockLockWithHardRefresh]:
    """Create a coordinator with a poll-based (non-push) lock."""
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
    return coordinator, lock


def _create_push_coordinator(
    hass: HomeAssistant,
) -> tuple[LockUsercodeUpdateCoordinator, MockLockWithPush]:
    """Create a coordinator with a push-based lock."""
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
    return coordinator, lock


async def test_backoff_failure_counter_increments(hass: HomeAssistant) -> None:
    """Test that consecutive failure counter increments on each failure."""
    coordinator, lock = _create_poll_coordinator(hass)
    # Simulate that we previously had a successful update so UpdateFailed is raised
    coordinator.last_update_success = True

    mock_get = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(lock, "async_internal_get_usercodes", mock_get):
        for i in range(1, 4):
            with pytest.raises(UpdateFailed):
                await coordinator.async_get_usercodes()
            assert coordinator._consecutive_failures == i


async def test_backoff_first_failure_returns_empty_dict(
    hass: HomeAssistant,
) -> None:
    """Test that first failure returns empty dict when no prior success."""
    coordinator, lock = _create_poll_coordinator(hass)
    # No successful update yet
    coordinator.last_update_success = False

    mock_get = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(lock, "async_internal_get_usercodes", mock_get):
        result = await coordinator.async_get_usercodes()

    assert result == {}
    assert coordinator._consecutive_failures == 1


async def test_backoff_subsequent_failure_raises_update_failed(
    hass: HomeAssistant,
) -> None:
    """Test that subsequent failures raise UpdateFailed after prior success."""
    coordinator, lock = _create_poll_coordinator(hass)
    coordinator.last_update_success = True

    mock_get = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(lock, "async_internal_get_usercodes", mock_get):
        with pytest.raises(UpdateFailed):
            await coordinator.async_get_usercodes()

    assert coordinator._consecutive_failures == 1


async def test_backoff_activates_after_threshold(hass: HomeAssistant) -> None:
    """Test that backoff activates after BACKOFF_FAILURE_THRESHOLD failures."""
    coordinator, lock = _create_poll_coordinator(hass)
    original_interval = coordinator.update_interval
    coordinator.last_update_success = True

    mock_get = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(lock, "async_internal_get_usercodes", mock_get):
        # Failures below threshold should not change interval
        for _ in range(BACKOFF_FAILURE_THRESHOLD - 1):
            with pytest.raises(UpdateFailed):
                await coordinator.async_get_usercodes()

        assert coordinator.update_interval == original_interval

        # Failure at threshold should activate backoff
        with pytest.raises(UpdateFailed):
            await coordinator.async_get_usercodes()

        assert coordinator._consecutive_failures == BACKOFF_FAILURE_THRESHOLD
        expected_backoff = timedelta(
            seconds=BACKOFF_INITIAL_SECONDS * 2**0  # 2^(3-3) = 1
        )
        assert coordinator.update_interval == expected_backoff


async def test_backoff_interval_increases_exponentially(
    hass: HomeAssistant,
) -> None:
    """Test that update_interval increases exponentially for poll-based providers."""
    coordinator, lock = _create_poll_coordinator(hass)
    coordinator.last_update_success = True

    mock_get = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(lock, "async_internal_get_usercodes", mock_get):
        # Reach threshold + additional failures
        for _ in range(BACKOFF_FAILURE_THRESHOLD + 3):
            with pytest.raises(UpdateFailed):
                await coordinator.async_get_usercodes()

    # After threshold+3 failures, exponent = 3, backoff = 60 * 2^3 = 480s
    expected_backoff = timedelta(seconds=BACKOFF_INITIAL_SECONDS * 2**3)
    assert coordinator.update_interval == expected_backoff


async def test_backoff_caps_at_max(hass: HomeAssistant) -> None:
    """Test that backoff interval is capped at BACKOFF_MAX_SECONDS."""
    coordinator, lock = _create_poll_coordinator(hass)
    coordinator.last_update_success = True

    mock_get = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(lock, "async_internal_get_usercodes", mock_get):
        # Many failures to exceed max
        for _ in range(BACKOFF_FAILURE_THRESHOLD + 20):
            with pytest.raises(UpdateFailed):
                await coordinator.async_get_usercodes()

    assert coordinator.update_interval == timedelta(seconds=BACKOFF_MAX_SECONDS)


async def test_backoff_resets_on_success(hass: HomeAssistant) -> None:
    """Test that counters and interval reset on success."""
    coordinator, lock = _create_poll_coordinator(hass)
    original_interval = coordinator.update_interval
    coordinator.last_update_success = True

    mock_get_fail = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(lock, "async_internal_get_usercodes", mock_get_fail):
        # Accumulate failures past threshold
        for _ in range(BACKOFF_FAILURE_THRESHOLD + 1):
            with pytest.raises(UpdateFailed):
                await coordinator.async_get_usercodes()

    assert coordinator._consecutive_failures == BACKOFF_FAILURE_THRESHOLD + 1
    assert coordinator.update_interval != original_interval

    # Now succeed
    mock_get_success = AsyncMock(return_value={1: "1234"})
    with patch.object(lock, "async_internal_get_usercodes", mock_get_success):
        result = await coordinator.async_get_usercodes()

    assert result == {1: "1234"}
    assert coordinator._consecutive_failures == 0
    assert coordinator.update_interval == original_interval


async def test_backoff_no_reset_when_no_prior_failures(
    hass: HomeAssistant,
) -> None:
    """Test that success with no prior failures does not modify interval."""
    coordinator, lock = _create_poll_coordinator(hass)
    original_interval = coordinator.update_interval

    mock_get = AsyncMock(return_value={1: "1234"})
    with patch.object(lock, "async_internal_get_usercodes", mock_get):
        result = await coordinator.async_get_usercodes()

    assert result == {1: "1234"}
    assert coordinator._consecutive_failures == 0
    assert coordinator.update_interval == original_interval


async def test_drift_check_skipped_during_backoff(hass: HomeAssistant) -> None:
    """Test that drift check is skipped when in backoff."""
    coordinator, lock = _create_push_coordinator(hass)
    coordinator.last_update_success = True
    coordinator._consecutive_failures = BACKOFF_FAILURE_THRESHOLD

    mock_hard_refresh = AsyncMock()
    with patch.object(lock, "async_internal_hard_refresh_codes", mock_hard_refresh):
        await coordinator._async_drift_check(dt_util.utcnow())

        # Hard refresh should NOT be called during backoff
        mock_hard_refresh.assert_not_called()


async def test_drift_check_runs_below_backoff_threshold(
    hass: HomeAssistant,
) -> None:
    """Test that drift check runs when failures are below threshold."""
    coordinator, lock = _create_push_coordinator(hass)
    coordinator.last_update_success = True
    coordinator._consecutive_failures = BACKOFF_FAILURE_THRESHOLD - 1

    mock_hard_refresh = AsyncMock(return_value={1: "1234"})
    with patch.object(lock, "async_internal_hard_refresh_codes", mock_hard_refresh):
        await coordinator._async_drift_check(dt_util.utcnow())

        # Hard refresh SHOULD be called below threshold
        mock_hard_refresh.assert_called_once()


async def test_backoff_push_provider_does_not_change_interval(
    hass: HomeAssistant,
) -> None:
    """Test that push-based providers do not modify update_interval during backoff."""
    coordinator, lock = _create_push_coordinator(hass)
    # Push providers have update_interval=None
    assert coordinator.update_interval is None
    coordinator.last_update_success = True

    mock_get = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(lock, "async_internal_get_usercodes", mock_get):
        for _ in range(BACKOFF_FAILURE_THRESHOLD + 2):
            with pytest.raises(UpdateFailed):
                await coordinator.async_get_usercodes()

    # update_interval should remain None for push providers
    assert coordinator.update_interval is None
    # But failure counter should still be tracked
    assert coordinator._consecutive_failures == BACKOFF_FAILURE_THRESHOLD + 2


async def test_backoff_init_stores_original_interval(
    hass: HomeAssistant,
) -> None:
    """Test that __init__ stores the original update interval."""
    coordinator, lock = _create_poll_coordinator(hass)
    assert coordinator._original_update_interval == lock.usercode_scan_interval
    assert coordinator._consecutive_failures == 0


async def test_backoff_init_push_stores_none_interval(
    hass: HomeAssistant,
) -> None:
    """Test that __init__ stores None for push-based providers."""
    coordinator, _ = _create_push_coordinator(hass)
    assert coordinator._original_update_interval is None
    assert coordinator._consecutive_failures == 0


async def test_push_update_resets_backoff(hass: HomeAssistant) -> None:
    """Test that push_update resets backoff state when data changes."""
    coordinator, lock = _create_push_coordinator(hass)
    coordinator.last_update_success = True

    # Simulate failures past threshold
    mock_get = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(lock, "async_internal_get_usercodes", mock_get):
        for _ in range(BACKOFF_FAILURE_THRESHOLD + 2):
            with pytest.raises(UpdateFailed):
                await coordinator.async_get_usercodes()

    assert coordinator._consecutive_failures == BACKOFF_FAILURE_THRESHOLD + 2

    # Push update with new data should reset backoff
    coordinator.data = {1: "old"}
    coordinator.push_update({1: "1234"})

    assert coordinator._consecutive_failures == 0


async def test_push_update_no_reset_when_data_unchanged(
    hass: HomeAssistant,
) -> None:
    """Test that push_update does not reset backoff when data is unchanged."""
    coordinator, lock = _create_push_coordinator(hass)
    coordinator.last_update_success = True

    # Simulate failures past threshold
    mock_get = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(lock, "async_internal_get_usercodes", mock_get):
        for _ in range(BACKOFF_FAILURE_THRESHOLD + 1):
            with pytest.raises(UpdateFailed):
                await coordinator.async_get_usercodes()

    assert coordinator._consecutive_failures == BACKOFF_FAILURE_THRESHOLD + 1

    # Push update with same data should NOT reset backoff
    coordinator.data = {1: "1234"}
    coordinator.push_update({1: "1234"})

    assert coordinator._consecutive_failures == BACKOFF_FAILURE_THRESHOLD + 1


async def test_poll_failure_alert_created_after_threshold(
    hass: HomeAssistant,
) -> None:
    """Test that a repair issue is created after POLL_FAILURE_ALERT_THRESHOLD failures."""
    coordinator, lock = _create_poll_coordinator(hass)
    coordinator.last_update_success = True

    mock_get = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(lock, "async_internal_get_usercodes", mock_get):
        for _ in range(POLL_FAILURE_ALERT_THRESHOLD):
            with pytest.raises(UpdateFailed):
                await coordinator.async_get_usercodes()

    issue_registry = async_get_issue_registry(hass)
    issue_id = f"lock_offline_{lock.lock.entity_id}"
    issue = issue_registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.severity == "warning"
    assert issue.is_fixable is False


async def test_poll_failure_alert_not_created_before_threshold(
    hass: HomeAssistant,
) -> None:
    """Test that no repair issue exists before reaching the alert threshold."""
    coordinator, lock = _create_poll_coordinator(hass)
    coordinator.last_update_success = True

    mock_get = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(lock, "async_internal_get_usercodes", mock_get):
        for _ in range(POLL_FAILURE_ALERT_THRESHOLD - 1):
            with pytest.raises(UpdateFailed):
                await coordinator.async_get_usercodes()

    issue_registry = async_get_issue_registry(hass)
    issue_id = f"lock_offline_{lock.lock.entity_id}"
    issue = issue_registry.async_get_issue(DOMAIN, issue_id)
    assert issue is None


async def test_poll_failure_alert_dismissed_on_recovery(
    hass: HomeAssistant,
) -> None:
    """Test that the repair issue is dismissed when the lock recovers."""
    coordinator, lock = _create_poll_coordinator(hass)
    coordinator.last_update_success = True

    mock_get_fail = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(lock, "async_internal_get_usercodes", mock_get_fail):
        for _ in range(POLL_FAILURE_ALERT_THRESHOLD):
            with pytest.raises(UpdateFailed):
                await coordinator.async_get_usercodes()

    # Verify issue exists
    issue_registry = async_get_issue_registry(hass)
    issue_id = f"lock_offline_{lock.lock.entity_id}"
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is not None

    # Now succeed
    mock_get_success = AsyncMock(return_value={1: "1234"})
    with patch.object(lock, "async_internal_get_usercodes", mock_get_success):
        await coordinator.async_get_usercodes()

    # Issue should be dismissed
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None
