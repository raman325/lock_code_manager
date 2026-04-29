"""Test the coordinator module."""

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
from custom_components.lock_code_manager.providers.virtual import VirtualLock

from .common import MockLCMLock, MockLCMPushLock


def _make_lock(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    cls: type[MockLCMLock] = MockLCMLock,
) -> MockLCMLock:
    """Create a mock lock instance bound to a config entry."""
    entity_reg = er.async_get(hass)
    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock",
        config_entry=config_entry,
    )
    return cls(
        hass,
        dr.async_get(hass),
        entity_reg,
        config_entry,
        lock_entity,
    )


def _make_coordinator(
    hass: HomeAssistant,
    lock: MockLCMLock,
    config_entry: MockConfigEntry,
) -> LockUsercodeUpdateCoordinator:
    """Create a coordinator for a mock lock."""
    return LockUsercodeUpdateCoordinator(hass, lock, config_entry)


@pytest.fixture
def lcm_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Return a minimal config entry added to hass."""
    entry = MockConfigEntry(domain=DOMAIN)
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def poll_lock(hass: HomeAssistant, lcm_config_entry: MockConfigEntry) -> MockLCMLock:
    """Return a poll-based mock lock."""
    return _make_lock(hass, lcm_config_entry)


@pytest.fixture
def push_lock(
    hass: HomeAssistant, lcm_config_entry: MockConfigEntry
) -> MockLCMPushLock:
    """Return a push-based mock lock."""
    return _make_lock(hass, lcm_config_entry, cls=MockLCMPushLock)


@pytest.fixture
def poll_coordinator(
    hass: HomeAssistant, poll_lock: MockLCMLock, lcm_config_entry: MockConfigEntry
) -> LockUsercodeUpdateCoordinator:
    """Return a coordinator with a poll-based lock."""
    return _make_coordinator(hass, poll_lock, lcm_config_entry)


@pytest.fixture
def push_coordinator(
    hass: HomeAssistant, push_lock: MockLCMPushLock, lcm_config_entry: MockConfigEntry
) -> LockUsercodeUpdateCoordinator:
    """Return a coordinator with a push-based lock (with hard refresh enabled)."""
    push_lock._hard_refresh_interval = timedelta(hours=1)
    return _make_coordinator(hass, push_lock, lcm_config_entry)


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
    poll_lock: MockLCMLock,
    lcm_config_entry: MockConfigEntry,
):
    """Test that drift detection timer IS created when hard_refresh_interval is set."""
    poll_lock._hard_refresh_interval = timedelta(hours=1)

    coordinator = _make_coordinator(hass, poll_lock, lcm_config_entry)

    # With hard_refresh_interval set, drift timer SHOULD be created
    assert coordinator._drift_unsub is not None


async def test_coordinator_disables_polling_for_push_enabled_lock(
    push_coordinator: LockUsercodeUpdateCoordinator,
):
    """Test that coordinator disables polling when lock supports push."""
    # When supports_push is True, update_interval should be None (polling disabled)
    assert push_coordinator.update_interval is None


async def test_coordinator_enables_polling_for_non_push_lock(
    poll_coordinator: LockUsercodeUpdateCoordinator,
    poll_lock: MockLCMLock,
):
    """Test that coordinator enables polling when lock doesn't support push."""
    # When supports_push is False, update_interval should be set (polling enabled)
    assert poll_coordinator.update_interval == poll_lock.usercode_scan_interval


async def test_push_update_updates_coordinator_data(
    push_coordinator: LockUsercodeUpdateCoordinator,
):
    """Test that push_update correctly updates coordinator data."""
    push_coordinator.data = {1: "1111", 2: "2222"}

    # Push a single update
    push_coordinator.push_update({1: "9999"})

    # Verify data was updated
    assert push_coordinator.data[1] == "9999"
    assert push_coordinator.data[2] == "2222"  # Unchanged


async def test_push_update_bulk_updates(
    push_coordinator: LockUsercodeUpdateCoordinator,
):
    """Test that push_update correctly handles bulk updates."""
    push_coordinator.data = {1: "1111", 2: "2222", 3: "3333"}

    # Push bulk update
    push_coordinator.push_update({1: "9999", 3: ""})

    # Verify all updates applied
    assert push_coordinator.data[1] == "9999"
    assert push_coordinator.data[2] == "2222"  # Unchanged
    assert push_coordinator.data[3] == ""  # Cleared


async def test_push_update_ignores_empty_updates(
    push_coordinator: LockUsercodeUpdateCoordinator,
):
    """Test that push_update ignores empty update dict."""
    push_coordinator.data = {1: "1111"}

    # Track async_set_updated_data calls
    with patch.object(push_coordinator, "async_set_updated_data") as mock_set_updated:
        push_coordinator.push_update({})

        # Should not call async_set_updated_data for empty updates
        mock_set_updated.assert_not_called()


async def test_push_update_notifies_listeners(
    push_coordinator: LockUsercodeUpdateCoordinator,
):
    """Test that push_update notifies coordinator listeners."""
    push_coordinator.data = {1: "1111"}

    # Track listener callbacks
    listener_called = [False]

    @callback
    def listener():
        listener_called[0] = True

    push_coordinator.async_add_listener(listener)

    # Push an update
    push_coordinator.push_update({1: "9999"})

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

    lock = MockLCMPushLock(
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
    push_lock: MockLCMPushLock,
    lcm_config_entry: MockConfigEntry,
):
    """Test that unsubscribe_push_updates is called during async_unload."""
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
        await push_lock.async_setup_internal(lcm_config_entry)

    # Unload
    assert not push_lock._unsubscribe_called
    await push_lock.async_unload(remove_permanently=False)
    assert push_lock._unsubscribe_called


async def test_subscribe_push_not_called_for_non_push_lock(
    hass: HomeAssistant,
    push_lock: MockLCMPushLock,
    lcm_config_entry: MockConfigEntry,
):
    """Test that subscribe_push_updates is NOT called for non-push locks."""
    # Disable push support
    push_lock._supports_push = False

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
        await push_lock.async_setup_internal(lcm_config_entry)
        # subscribe_push_updates should NOT have been called
        assert not push_lock._subscribe_called


async def test_async_shutdown_cleans_up_drift_timer(
    push_coordinator: LockUsercodeUpdateCoordinator,
):
    """Test that async_shutdown cleans up the drift detection timer."""
    # Drift timer should be set up (push_coordinator fixture sets hard_refresh_interval)
    assert push_coordinator._drift_unsub is not None

    # Shutdown coordinator
    await push_coordinator.async_shutdown()

    # Drift timer should be cleaned up
    assert push_coordinator._drift_unsub is None


async def test_drift_check_calls_hard_refresh(
    push_coordinator: LockUsercodeUpdateCoordinator,
    push_lock: MockLCMPushLock,
):
    """Test that _async_drift_check calls async_internal_hard_refresh_codes."""
    # Mock the hard refresh method. Return a real dict so the coordinator's
    # int-key normalization (applied to drift-check results) can iterate it.
    mock_hard_refresh = AsyncMock(return_value={1: "1234"})

    with patch.object(
        push_lock, "async_internal_hard_refresh_codes", mock_hard_refresh
    ):
        await push_coordinator._async_drift_check(dt_util.utcnow())

        mock_hard_refresh.assert_called_once()


async def test_coordinator_lock_property(
    poll_coordinator: LockUsercodeUpdateCoordinator,
    poll_lock: MockLCMLock,
):
    """Test that coordinator.lock returns the lock instance."""
    assert poll_coordinator.lock is poll_lock


async def test_drift_check_skips_before_initial_success(
    push_coordinator: LockUsercodeUpdateCoordinator,
    push_lock: MockLCMPushLock,
):
    """Test that _async_drift_check skips if initial data hasn't loaded."""
    # Simulate no successful update yet
    push_coordinator.last_update_success = False

    mock_hard_refresh = AsyncMock()
    with patch.object(
        push_lock, "async_internal_hard_refresh_codes", mock_hard_refresh
    ):
        await push_coordinator._async_drift_check(dt_util.utcnow())

        # Should not call hard refresh when last_update_success is False
        mock_hard_refresh.assert_not_called()


async def test_drift_check_handles_hard_refresh_error(
    push_coordinator: LockUsercodeUpdateCoordinator,
    push_lock: MockLCMPushLock,
):
    """Test that _async_drift_check handles hard refresh errors gracefully."""
    push_coordinator.last_update_success = True
    push_coordinator.data = {1: "1234"}

    # Mock hard refresh to raise an exception
    mock_hard_refresh = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(
        push_lock, "async_internal_hard_refresh_codes", mock_hard_refresh
    ):
        # Should not raise, should handle gracefully
        await push_coordinator._async_drift_check(dt_util.utcnow())

        # Data should remain unchanged
        assert push_coordinator.data == {1: "1234"}


# --- Backoff tests ---


async def test_backoff_failure_counter_increments(
    poll_coordinator: LockUsercodeUpdateCoordinator,
    poll_lock: MockLCMLock,
) -> None:
    """Test that consecutive failure counter increments on each failure."""
    # last_update_success=True is required for UpdateFailed to be raised on next failure.
    poll_coordinator.last_update_success = True

    mock_get = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(poll_lock, "async_internal_get_usercodes", mock_get):
        for i in range(1, 4):
            with pytest.raises(UpdateFailed):
                await poll_coordinator.async_get_usercodes()
            assert poll_coordinator._consecutive_failures == i


async def test_backoff_first_failure_returns_empty_dict(
    poll_coordinator: LockUsercodeUpdateCoordinator,
    poll_lock: MockLCMLock,
) -> None:
    """Test that first failure returns empty dict when no prior success."""
    # No successful update yet
    poll_coordinator.last_update_success = False

    mock_get = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(poll_lock, "async_internal_get_usercodes", mock_get):
        result = await poll_coordinator.async_get_usercodes()

    assert result == {}
    assert poll_coordinator._consecutive_failures == 1


async def test_backoff_subsequent_failure_raises_update_failed(
    poll_coordinator: LockUsercodeUpdateCoordinator,
    poll_lock: MockLCMLock,
) -> None:
    """Test that subsequent failures raise UpdateFailed after prior success."""
    poll_coordinator.last_update_success = True

    mock_get = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(poll_lock, "async_internal_get_usercodes", mock_get):
        with pytest.raises(UpdateFailed):
            await poll_coordinator.async_get_usercodes()

    assert poll_coordinator._consecutive_failures == 1


async def test_backoff_activates_after_threshold(
    poll_coordinator: LockUsercodeUpdateCoordinator,
    poll_lock: MockLCMLock,
) -> None:
    """Test that backoff activates after BACKOFF_FAILURE_THRESHOLD failures."""
    original_interval = poll_coordinator.update_interval
    poll_coordinator.last_update_success = True

    mock_get = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(poll_lock, "async_internal_get_usercodes", mock_get):
        # Failures below threshold should not change interval
        for _ in range(BACKOFF_FAILURE_THRESHOLD - 1):
            with pytest.raises(UpdateFailed):
                await poll_coordinator.async_get_usercodes()

        assert poll_coordinator.update_interval == original_interval

        # Failure at threshold should activate backoff
        with pytest.raises(UpdateFailed):
            await poll_coordinator.async_get_usercodes()

        assert poll_coordinator._consecutive_failures == BACKOFF_FAILURE_THRESHOLD
        expected_backoff = timedelta(
            seconds=BACKOFF_INITIAL_SECONDS * 2**0  # 2^(3-3) = 1
        )
        assert poll_coordinator.update_interval == expected_backoff


async def test_backoff_interval_increases_exponentially(
    poll_coordinator: LockUsercodeUpdateCoordinator,
    poll_lock: MockLCMLock,
) -> None:
    """Test that update_interval increases exponentially for poll-based providers."""
    poll_coordinator.last_update_success = True

    mock_get = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(poll_lock, "async_internal_get_usercodes", mock_get):
        # Reach threshold + additional failures
        for _ in range(BACKOFF_FAILURE_THRESHOLD + 3):
            with pytest.raises(UpdateFailed):
                await poll_coordinator.async_get_usercodes()

    # After threshold+3 failures, exponent = 3, backoff = 60 * 2^3 = 480s
    expected_backoff = timedelta(seconds=BACKOFF_INITIAL_SECONDS * 2**3)
    assert poll_coordinator.update_interval == expected_backoff


async def test_backoff_caps_at_max(
    poll_coordinator: LockUsercodeUpdateCoordinator,
    poll_lock: MockLCMLock,
) -> None:
    """Test that backoff interval is capped at BACKOFF_MAX_SECONDS."""
    poll_coordinator.last_update_success = True

    mock_get = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(poll_lock, "async_internal_get_usercodes", mock_get):
        # Many failures to exceed max
        for _ in range(BACKOFF_FAILURE_THRESHOLD + 20):
            with pytest.raises(UpdateFailed):
                await poll_coordinator.async_get_usercodes()

    assert poll_coordinator.update_interval == timedelta(seconds=BACKOFF_MAX_SECONDS)


async def test_backoff_resets_on_success(
    poll_coordinator: LockUsercodeUpdateCoordinator,
    poll_lock: MockLCMLock,
) -> None:
    """Test that counters and interval reset on success."""
    original_interval = poll_coordinator.update_interval
    poll_coordinator.last_update_success = True

    mock_get_fail = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(poll_lock, "async_internal_get_usercodes", mock_get_fail):
        # Accumulate failures past threshold
        for _ in range(BACKOFF_FAILURE_THRESHOLD + 1):
            with pytest.raises(UpdateFailed):
                await poll_coordinator.async_get_usercodes()

    assert poll_coordinator._consecutive_failures == BACKOFF_FAILURE_THRESHOLD + 1
    assert poll_coordinator.update_interval != original_interval

    # Now succeed
    mock_get_success = AsyncMock(return_value={1: "1234"})
    with patch.object(poll_lock, "async_internal_get_usercodes", mock_get_success):
        result = await poll_coordinator.async_get_usercodes()

    assert result == {1: "1234"}
    assert poll_coordinator._consecutive_failures == 0
    assert poll_coordinator.update_interval == original_interval


async def test_backoff_no_reset_when_no_prior_failures(
    poll_coordinator: LockUsercodeUpdateCoordinator,
    poll_lock: MockLCMLock,
) -> None:
    """Test that success with no prior failures does not modify interval."""
    original_interval = poll_coordinator.update_interval

    mock_get = AsyncMock(return_value={1: "1234"})
    with patch.object(poll_lock, "async_internal_get_usercodes", mock_get):
        result = await poll_coordinator.async_get_usercodes()

    assert result == {1: "1234"}
    assert poll_coordinator._consecutive_failures == 0
    assert poll_coordinator.update_interval == original_interval


async def test_drift_check_skipped_during_backoff(
    push_coordinator: LockUsercodeUpdateCoordinator,
    push_lock: MockLCMPushLock,
) -> None:
    """Test that drift check is skipped when in backoff."""
    push_coordinator.last_update_success = True
    push_coordinator._consecutive_failures = BACKOFF_FAILURE_THRESHOLD

    mock_hard_refresh = AsyncMock()
    with patch.object(
        push_lock, "async_internal_hard_refresh_codes", mock_hard_refresh
    ):
        await push_coordinator._async_drift_check(dt_util.utcnow())

        # Hard refresh should NOT be called during backoff
        mock_hard_refresh.assert_not_called()


async def test_drift_check_runs_below_backoff_threshold(
    push_coordinator: LockUsercodeUpdateCoordinator,
    push_lock: MockLCMPushLock,
) -> None:
    """Test that drift check runs when failures are below threshold."""
    push_coordinator.last_update_success = True
    push_coordinator._consecutive_failures = BACKOFF_FAILURE_THRESHOLD - 1

    mock_hard_refresh = AsyncMock(return_value={1: "1234"})
    with patch.object(
        push_lock, "async_internal_hard_refresh_codes", mock_hard_refresh
    ):
        await push_coordinator._async_drift_check(dt_util.utcnow())

        # Hard refresh SHOULD be called below threshold
        mock_hard_refresh.assert_called_once()


async def test_backoff_push_provider_does_not_change_interval(
    push_coordinator: LockUsercodeUpdateCoordinator,
    push_lock: MockLCMPushLock,
) -> None:
    """Test that push-based providers do not modify update_interval during backoff."""
    # Push providers have update_interval=None
    assert push_coordinator.update_interval is None
    push_coordinator.last_update_success = True

    mock_get = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(push_lock, "async_internal_get_usercodes", mock_get):
        for _ in range(BACKOFF_FAILURE_THRESHOLD + 2):
            with pytest.raises(UpdateFailed):
                await push_coordinator.async_get_usercodes()

    # update_interval should remain None for push providers
    assert push_coordinator.update_interval is None
    # But failure counter should still be tracked
    assert push_coordinator._consecutive_failures == BACKOFF_FAILURE_THRESHOLD + 2


async def test_backoff_init_stores_original_interval(
    poll_coordinator: LockUsercodeUpdateCoordinator,
    poll_lock: MockLCMLock,
) -> None:
    """Test that __init__ stores the original update interval."""
    assert (
        poll_coordinator._original_update_interval == poll_lock.usercode_scan_interval
    )
    assert poll_coordinator._consecutive_failures == 0


async def test_backoff_init_push_stores_none_interval(
    push_coordinator: LockUsercodeUpdateCoordinator,
) -> None:
    """Test that __init__ stores None for push-based providers."""
    assert push_coordinator._original_update_interval is None
    assert push_coordinator._consecutive_failures == 0


async def test_push_update_resets_backoff(
    push_coordinator: LockUsercodeUpdateCoordinator,
    push_lock: MockLCMPushLock,
) -> None:
    """Test that push_update resets backoff state when data changes."""
    push_coordinator.last_update_success = True

    # Simulate failures past threshold
    mock_get = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(push_lock, "async_internal_get_usercodes", mock_get):
        for _ in range(BACKOFF_FAILURE_THRESHOLD + 2):
            with pytest.raises(UpdateFailed):
                await push_coordinator.async_get_usercodes()

    assert push_coordinator._consecutive_failures == BACKOFF_FAILURE_THRESHOLD + 2

    # Push update with new data should reset backoff
    push_coordinator.data = {1: "old"}
    push_coordinator.push_update({1: "1234"})

    assert push_coordinator._consecutive_failures == 0


async def test_push_update_no_reset_when_data_unchanged(
    push_coordinator: LockUsercodeUpdateCoordinator,
    push_lock: MockLCMPushLock,
) -> None:
    """Test that push_update does not reset backoff when data is unchanged."""
    push_coordinator.last_update_success = True

    # Simulate failures past threshold
    mock_get = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(push_lock, "async_internal_get_usercodes", mock_get):
        for _ in range(BACKOFF_FAILURE_THRESHOLD + 1):
            with pytest.raises(UpdateFailed):
                await push_coordinator.async_get_usercodes()

    assert push_coordinator._consecutive_failures == BACKOFF_FAILURE_THRESHOLD + 1

    # Push update with same data should NOT reset backoff
    push_coordinator.data = {1: "1234"}
    push_coordinator.push_update({1: "1234"})

    assert push_coordinator._consecutive_failures == BACKOFF_FAILURE_THRESHOLD + 1


async def test_poll_failure_alert_created_after_threshold(
    poll_coordinator: LockUsercodeUpdateCoordinator,
    poll_lock: MockLCMLock,
    hass: HomeAssistant,
) -> None:
    """Test that a repair issue is created after POLL_FAILURE_ALERT_THRESHOLD failures."""
    poll_coordinator.last_update_success = True

    mock_get = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(poll_lock, "async_internal_get_usercodes", mock_get):
        for _ in range(POLL_FAILURE_ALERT_THRESHOLD):
            with pytest.raises(UpdateFailed):
                await poll_coordinator.async_get_usercodes()

    issue_registry = async_get_issue_registry(hass)
    issue_id = f"lock_offline_{poll_lock.lock.entity_id}"
    issue = issue_registry.async_get_issue(DOMAIN, issue_id)
    assert issue is not None
    assert issue.severity == "warning"
    assert issue.is_fixable is False


async def test_poll_failure_alert_not_created_before_threshold(
    poll_coordinator: LockUsercodeUpdateCoordinator,
    poll_lock: MockLCMLock,
    hass: HomeAssistant,
) -> None:
    """Test that no repair issue exists before reaching the alert threshold."""
    poll_coordinator.last_update_success = True

    mock_get = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(poll_lock, "async_internal_get_usercodes", mock_get):
        for _ in range(POLL_FAILURE_ALERT_THRESHOLD - 1):
            with pytest.raises(UpdateFailed):
                await poll_coordinator.async_get_usercodes()

    issue_registry = async_get_issue_registry(hass)
    issue_id = f"lock_offline_{poll_lock.lock.entity_id}"
    issue = issue_registry.async_get_issue(DOMAIN, issue_id)
    assert issue is None


async def test_poll_failure_alert_dismissed_on_recovery(
    poll_coordinator: LockUsercodeUpdateCoordinator,
    poll_lock: MockLCMLock,
    hass: HomeAssistant,
) -> None:
    """Test that the repair issue is dismissed when the lock recovers."""
    poll_coordinator.last_update_success = True

    mock_get_fail = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(poll_lock, "async_internal_get_usercodes", mock_get_fail):
        for _ in range(POLL_FAILURE_ALERT_THRESHOLD):
            with pytest.raises(UpdateFailed):
                await poll_coordinator.async_get_usercodes()

    # Verify issue exists
    issue_registry = async_get_issue_registry(hass)
    issue_id = f"lock_offline_{poll_lock.lock.entity_id}"
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is not None

    # Now succeed
    mock_get_success = AsyncMock(return_value={1: "1234"})
    with patch.object(poll_lock, "async_internal_get_usercodes", mock_get_success):
        await poll_coordinator.async_get_usercodes()

    # Issue should be dismissed
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None


async def test_coordinator_suspended_flag_defaults_false(
    hass: HomeAssistant, poll_lock, lcm_config_entry
) -> None:
    """Coordinator starts with suspended=False."""
    coordinator = LockUsercodeUpdateCoordinator(hass, poll_lock, lcm_config_entry)
    assert coordinator.slot_sync_mgrs_suspended is False


async def test_coordinator_suspended_cleared_on_successful_poll(
    hass: HomeAssistant, poll_lock, poll_coordinator
) -> None:
    """Successful poll clears suspended flag."""
    poll_coordinator.suspend_slot_sync_mgrs()
    await poll_coordinator.async_refresh()
    assert poll_coordinator.slot_sync_mgrs_suspended is False


async def test_coordinator_suspended_cleared_on_push_update(
    hass: HomeAssistant, push_lock, push_coordinator
) -> None:
    """Push update clears suspended flag."""
    push_coordinator.suspend_slot_sync_mgrs()
    push_coordinator.push_update({1: "1234"})
    assert push_coordinator.slot_sync_mgrs_suspended is False


async def test_coordinator_suspended_not_cleared_on_failed_poll(
    hass: HomeAssistant, poll_lock, poll_coordinator
) -> None:
    """Failed poll does not clear suspended flag."""
    poll_coordinator.suspend_slot_sync_mgrs()
    poll_lock.set_connected(False)
    await poll_coordinator.async_refresh()
    assert poll_coordinator.slot_sync_mgrs_suspended is True


async def test_lock_offline_issue_persists_across_shutdown(
    poll_coordinator: LockUsercodeUpdateCoordinator,
    poll_lock: MockLCMLock,
    hass: HomeAssistant,
) -> None:
    """
    Test that lock_offline repair issue persists across coordinator shutdown.

    The issue is persistent and only cleaned up on entry unload or recovery.
    """
    poll_coordinator.last_update_success = True

    mock_get_fail = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(poll_lock, "async_internal_get_usercodes", mock_get_fail):
        for _ in range(POLL_FAILURE_ALERT_THRESHOLD):
            with pytest.raises(UpdateFailed):
                await poll_coordinator.async_get_usercodes()

    issue_registry = async_get_issue_registry(hass)
    issue_id = f"lock_offline_{poll_lock.lock.entity_id}"
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is not None

    # Shutdown should NOT delete the issue — it persists across restarts
    await poll_coordinator.async_shutdown()
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is not None
