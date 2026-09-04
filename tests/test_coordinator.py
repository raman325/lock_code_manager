"""Test the coordinator module."""

import asyncio
from collections.abc import AsyncGenerator
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from homeassistant.const import CONF_ENABLED, CONF_NAME, CONF_PIN
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.issue_registry import async_get as async_get_issue_registry
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.util import dt as dt_util

from custom_components.lock_code_manager.const import (
    BACKOFF_FAILURE_THRESHOLD,
    BACKOFF_INITIAL_SECONDS,
    BACKOFF_MAX_SECONDS,
    CONF_LOCKS,
    CONF_SLOTS,
    CONFIRM_READ_INTERVAL,
    DOMAIN,
    PENDING_WRITE_TTL,
    POLL_FAILURE_ALERT_THRESHOLD,
)
from custom_components.lock_code_manager.domain.coordinator import (
    LockUsercodeUpdateCoordinator,
)
from custom_components.lock_code_manager.domain.credentials import (
    CredentialAddress,
    CredentialType,
    pin_address,
)
from custom_components.lock_code_manager.domain.exceptions import (
    LockBusy,
    LockDisconnected,
)
from custom_components.lock_code_manager.domain.models import SlotCredential
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
async def poll_coordinator(
    hass: HomeAssistant, poll_lock: MockLCMLock, lcm_config_entry: MockConfigEntry
) -> AsyncGenerator[LockUsercodeUpdateCoordinator]:
    """Return a coordinator with a poll-based lock."""
    coordinator = _make_coordinator(hass, poll_lock, lcm_config_entry)
    yield coordinator
    await coordinator.async_shutdown()


@pytest.fixture
async def push_coordinator(
    hass: HomeAssistant, push_lock: MockLCMPushLock, lcm_config_entry: MockConfigEntry
) -> AsyncGenerator[LockUsercodeUpdateCoordinator]:
    """Return a coordinator with a push-based lock (with hard refresh enabled)."""
    push_lock._hard_refresh_interval = timedelta(hours=1)
    coordinator = _make_coordinator(hass, push_lock, lcm_config_entry)
    yield coordinator
    await coordinator.async_shutdown()


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
    push_coordinator.data = {
        pin_address(1): SlotCredential.known("1111"),
        pin_address(2): SlotCredential.known("2222"),
    }

    # Push a single update
    push_coordinator.push_update({1: SlotCredential.known("9999")})

    # Verify data was updated
    assert push_coordinator.data[pin_address(1)] == SlotCredential.known("9999")
    assert push_coordinator.data[pin_address(2)] == SlotCredential.known(
        "2222"
    )  # Unchanged


async def test_push_update_bulk_updates(
    push_coordinator: LockUsercodeUpdateCoordinator,
):
    """Test that push_update correctly handles bulk updates."""
    push_coordinator.data = {
        pin_address(1): SlotCredential.known("1111"),
        pin_address(2): SlotCredential.known("2222"),
        pin_address(3): SlotCredential.known("3333"),
    }

    # Push bulk update
    push_coordinator.push_update(
        {1: SlotCredential.known("9999"), 3: SlotCredential.empty()}
    )

    # Verify all updates applied
    assert push_coordinator.data[pin_address(1)] == SlotCredential.known("9999")
    assert push_coordinator.data[pin_address(2)] == SlotCredential.known(
        "2222"
    )  # Unchanged
    assert push_coordinator.data[pin_address(3)] == SlotCredential.empty()  # Cleared


async def test_is_verified_defaults_true_for_absent_slot(
    push_coordinator: LockUsercodeUpdateCoordinator,
):
    """A slot with no recorded verified flag reads as verified."""
    assert push_coordinator.is_verified(pin_address(7)) is True


async def test_push_update_default_marks_verified(
    push_coordinator: LockUsercodeUpdateCoordinator,
):
    """The default (optimistic=False) push marks the slot verified."""
    push_coordinator.push_update({1: SlotCredential.known("9999")})
    assert push_coordinator.is_verified(pin_address(1)) is True


async def test_push_update_ignores_empty_updates(
    push_coordinator: LockUsercodeUpdateCoordinator,
):
    """Test that push_update ignores empty update dict."""
    push_coordinator.data = {pin_address(1): SlotCredential.known("1111")}

    # Track async_set_updated_data calls
    with patch.object(push_coordinator, "async_set_updated_data") as mock_set_updated:
        push_coordinator.push_update({})

        # Should not call async_set_updated_data for empty updates
        mock_set_updated.assert_not_called()


async def test_push_update_notifies_listeners(
    push_coordinator: LockUsercodeUpdateCoordinator,
):
    """Test that push_update notifies coordinator listeners."""
    push_coordinator.data = {pin_address(1): SlotCredential.known("1111")}

    # Track listener callbacks
    listener_called = [False]

    @callback
    def listener():
        listener_called[0] = True

    push_coordinator.async_add_listener(listener)

    # Push an update
    push_coordinator.push_update({1: SlotCredential.known("9999")})

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
            "custom_components.lock_code_manager.domain.coordinator."
            "LockUsercodeUpdateCoordinator.async_config_entry_first_refresh"
        ),
        patch(
            "custom_components.lock_code_manager.domain.coordinator."
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
            "custom_components.lock_code_manager.domain.coordinator."
            "LockUsercodeUpdateCoordinator.async_config_entry_first_refresh"
        ),
        patch(
            "custom_components.lock_code_manager.domain.coordinator."
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
            "custom_components.lock_code_manager.domain.coordinator."
            "LockUsercodeUpdateCoordinator.async_config_entry_first_refresh"
        ),
        patch(
            "custom_components.lock_code_manager.domain.coordinator."
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
    mock_hard_refresh = AsyncMock(return_value={1: SlotCredential.known("1234")})

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
    push_coordinator.data = {pin_address(1): SlotCredential.known("1234")}

    # Mock hard refresh to raise an exception
    mock_hard_refresh = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(
        push_lock, "async_internal_hard_refresh_codes", mock_hard_refresh
    ):
        # Should not raise, should handle gracefully
        await push_coordinator._async_drift_check(dt_util.utcnow())

        # Data should remain unchanged
        assert push_coordinator.data == {pin_address(1): SlotCredential.known("1234")}


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
            assert poll_coordinator._lock_breaker.failure_count == i


async def test_cold_start_failure_raises_update_failed(
    poll_coordinator: LockUsercodeUpdateCoordinator,
    poll_lock: MockLCMLock,
) -> None:
    """A failure before any successful poll raises UpdateFailed, never a false success.

    Returning {} here would be recorded by DataUpdateCoordinator as a successful
    update, flipping last_update_success to True and logging a misleading
    "recovered" while the lock is still unreachable (issue #1268). The breaker
    still records the failure.
    """
    # No successful update yet (cold start).
    poll_coordinator.last_update_success = False

    mock_get = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(poll_lock, "async_internal_get_usercodes", mock_get):
        with pytest.raises(UpdateFailed):
            await poll_coordinator.async_get_usercodes()

    assert poll_coordinator._lock_breaker.failure_count == 1


async def test_cold_start_repeated_failures_keep_raising(
    poll_coordinator: LockUsercodeUpdateCoordinator,
    poll_lock: MockLCMLock,
) -> None:
    """Sustained cold-start failures keep raising; no tick masquerades as success.

    Regression for issue #1268: the old guard returned {} whenever
    last_update_success was False. DataUpdateCoordinator records that as a
    successful empty update -- flipping last_update_success True (logged
    "recovered" / "success: True" in 0.000s), feeding empty data to the sync
    layer ("Slot not in coordinator data, skipping"), then failing again next
    tick. With the guard gone, last_update_success staying False never diverts
    a failure into a fake success: every tick raises and the breaker counts it.
    """
    # Lock never reached; last_update_success stays False across the outage.
    poll_coordinator.last_update_success = False

    mock_get = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(poll_lock, "async_internal_get_usercodes", mock_get):
        for i in range(1, 6):
            with pytest.raises(UpdateFailed):
                await poll_coordinator.async_get_usercodes()
            assert poll_coordinator._lock_breaker.failure_count == i


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

    assert poll_coordinator._lock_breaker.failure_count == 1


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

        assert poll_coordinator._lock_breaker.failure_count == BACKOFF_FAILURE_THRESHOLD
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

    assert poll_coordinator._lock_breaker.failure_count == BACKOFF_FAILURE_THRESHOLD + 1
    assert poll_coordinator.update_interval != original_interval

    # Now succeed
    mock_get_success = AsyncMock(return_value={1: SlotCredential.known("1234")})
    with patch.object(poll_lock, "async_internal_get_usercodes", mock_get_success):
        result = await poll_coordinator.async_get_usercodes()

    assert result == {pin_address(1): SlotCredential.known("1234")}
    assert poll_coordinator._lock_breaker.failure_count == 0
    assert poll_coordinator.update_interval == original_interval


async def test_backoff_no_reset_when_no_prior_failures(
    poll_coordinator: LockUsercodeUpdateCoordinator,
    poll_lock: MockLCMLock,
) -> None:
    """Test that success with no prior failures does not modify interval."""
    original_interval = poll_coordinator.update_interval

    mock_get = AsyncMock(return_value={1: SlotCredential.known("1234")})
    with patch.object(poll_lock, "async_internal_get_usercodes", mock_get):
        result = await poll_coordinator.async_get_usercodes()

    assert result == {pin_address(1): SlotCredential.known("1234")}
    assert poll_coordinator._lock_breaker.failure_count == 0
    assert poll_coordinator.update_interval == original_interval


async def test_drift_check_skipped_during_backoff(
    push_coordinator: LockUsercodeUpdateCoordinator,
    push_lock: MockLCMPushLock,
) -> None:
    """Test that drift check is skipped when in backoff."""
    push_coordinator.last_update_success = True
    for _ in range(BACKOFF_FAILURE_THRESHOLD):
        push_coordinator._lock_breaker.record_failure()

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
    for _ in range(BACKOFF_FAILURE_THRESHOLD - 1):
        push_coordinator._lock_breaker.record_failure()

    mock_hard_refresh = AsyncMock(return_value={1: SlotCredential.known("1234")})
    with patch.object(
        push_lock, "async_internal_hard_refresh_codes", mock_hard_refresh
    ):
        await push_coordinator._async_drift_check(dt_util.utcnow())

        # Hard refresh SHOULD be called below threshold
        mock_hard_refresh.assert_called_once()


async def test_backoff_push_provider_polls_to_probe_recovery(
    push_coordinator: LockUsercodeUpdateCoordinator,
    push_lock: MockLCMPushLock,
) -> None:
    """A push provider starts polling on a backoff once the breaker trips, then stops on recovery."""
    # Push providers normally have update_interval=None (no polling)
    assert push_coordinator.update_interval is None
    push_coordinator.last_update_success = True

    mock_get = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(push_lock, "async_internal_get_usercodes", mock_get):
        for _ in range(BACKOFF_FAILURE_THRESHOLD + 2):
            with pytest.raises(UpdateFailed):
                await push_coordinator.async_get_usercodes()

    # While unreachable, the push provider polls on the backoff interval to
    # probe for recovery.
    assert push_coordinator.unreachable is True
    assert (
        push_coordinator.update_interval == push_coordinator._lock_breaker.backoff_delay
    )
    assert push_coordinator._lock_breaker.failure_count == BACKOFF_FAILURE_THRESHOLD + 2

    # A successful poll clears the breaker and stops the probe polling.
    mock_get_ok = AsyncMock(return_value={1: SlotCredential.known("1234")})
    with patch.object(push_lock, "async_internal_get_usercodes", mock_get_ok):
        await push_coordinator.async_get_usercodes()

    assert push_coordinator.unreachable is False
    assert push_coordinator.update_interval is None


async def test_push_initial_load_failure_schedules_retry(
    push_coordinator: LockUsercodeUpdateCoordinator,
    push_lock: MockLCMPushLock,
) -> None:
    """
    A push provider's failed initial load retries before the breaker trips.

    A push provider polls on no timer at all, so the recovery probe above can
    never start from a cold start: tripping the breaker takes repeated polls,
    and there is nothing scheduling them. One failed first load would strand
    every entity unavailable until a reload -- which re-runs the same first
    load and hits the same wall. Until the coordinator has been seeded once,
    a failure schedules a retry at the base backoff cadence.
    """
    assert push_coordinator.update_interval is None
    assert push_coordinator._reached_once is False

    mock_get = AsyncMock(side_effect=LockDisconnected("asleep at boot"))
    with (
        patch.object(push_lock, "async_internal_get_usercodes", mock_get),
        pytest.raises(UpdateFailed),
    ):
        await push_coordinator.async_get_usercodes()

    # One failure is far below the trip threshold, yet a retry is scheduled
    # because the lock has never been reached.
    assert push_coordinator._lock_breaker.failure_count == 1
    assert push_coordinator.unreachable is False
    assert push_coordinator.update_interval == timedelta(
        seconds=BACKOFF_INITIAL_SECONDS
    )

    mock_ok = AsyncMock(return_value={1: SlotCredential.known("1234")})
    with patch.object(push_lock, "async_internal_get_usercodes", mock_ok):
        await push_coordinator.async_get_usercodes()

    # Reached at last: push cadence (no timer) resumes.
    assert push_coordinator._reached_once is True
    assert push_coordinator.update_interval is None


async def test_poll_initial_load_failure_keeps_poll_cadence(
    hass: HomeAssistant,
    poll_lock: MockLCMLock,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """
    A poll provider's failed initial load leaves its own cadence alone.

    The retry arm exists only because a push provider has no timer; a poll
    provider already has one, and overriding it with the backoff cadence
    would re-pace a lock its provider paced deliberately. The scan interval
    here is chosen to differ from that cadence -- at the base default the two
    coincide and the assertion below would hold either way.
    """
    scan_interval = timedelta(seconds=BACKOFF_INITIAL_SECONDS * 7)
    with patch.object(type(poll_lock), "usercode_scan_interval", scan_interval):
        coordinator = _make_coordinator(hass, poll_lock, lcm_config_entry)
        assert coordinator.update_interval == scan_interval

        mock_get = AsyncMock(side_effect=LockDisconnected("asleep at boot"))
        with (
            patch.object(poll_lock, "async_internal_get_usercodes", mock_get),
            pytest.raises(UpdateFailed),
        ):
            await coordinator.async_get_usercodes()

    assert coordinator.unreachable is False
    assert coordinator.update_interval == scan_interval


async def test_push_failure_after_first_success_does_not_start_polling(
    push_coordinator: LockUsercodeUpdateCoordinator,
    push_lock: MockLCMPushLock,
) -> None:
    """
    Once seeded, a push provider's stray failure leaves it on no timer.

    The retry arm only exists to get the coordinator its first data; after
    that the entities have values to show and the breaker owns escalation.
    Polling on every blip would give a push provider a poll cadence it never
    asked for and never sheds until a success.
    """
    mock_ok = AsyncMock(return_value={1: SlotCredential.known("1234")})
    with patch.object(push_lock, "async_internal_get_usercodes", mock_ok):
        await push_coordinator.async_get_usercodes()
    assert push_coordinator._reached_once is True

    mock_get = AsyncMock(side_effect=LockDisconnected("one bad poll"))
    with (
        patch.object(push_lock, "async_internal_get_usercodes", mock_get),
        pytest.raises(UpdateFailed),
    ):
        await push_coordinator.async_get_usercodes()

    assert push_coordinator.unreachable is False
    assert push_coordinator.update_interval is None


async def test_push_retry_yields_to_the_breaker_once_it_trips(
    push_coordinator: LockUsercodeUpdateCoordinator,
    push_lock: MockLCMPushLock,
) -> None:
    """
    The initial-load retry composes with the breaker instead of fighting it.

    Both arms want to set an update interval on a lock that normally has
    none. The breaker's backoff grows with each failure and is the one that
    has to win, or a lock that is genuinely down would be probed forever at
    the initial cadence.
    """
    mock_get = AsyncMock(side_effect=LockDisconnected("still asleep"))
    with patch.object(push_lock, "async_internal_get_usercodes", mock_get):
        # Two failures past the threshold, so the escalated delay is distinct
        # from the initial cadence the retry arm would set.
        for _ in range(BACKOFF_FAILURE_THRESHOLD + 2):
            with pytest.raises(UpdateFailed):
                await push_coordinator.async_get_usercodes()

    assert push_coordinator._reached_once is False
    assert push_coordinator.unreachable is True
    assert push_coordinator.update_interval == timedelta(
        seconds=BACKOFF_INITIAL_SECONDS * 4
    )
    assert (
        push_coordinator.update_interval == push_coordinator._lock_breaker.backoff_delay
    )


async def test_note_connectivity_failure_kicks_probe_for_push(
    hass: HomeAssistant,
    push_coordinator: LockUsercodeUpdateCoordinator,
) -> None:
    """Set-side failures that trip the breaker start probe polling on a push provider."""
    assert push_coordinator.update_interval is None

    with patch.object(
        push_coordinator, "async_request_refresh", new_callable=AsyncMock
    ) as mock_refresh:
        for _ in range(BACKOFF_FAILURE_THRESHOLD):
            push_coordinator.note_connectivity_failure()
        await hass.async_block_till_done()

    assert push_coordinator.unreachable is True
    # Probe polling enabled at the backoff cadence.
    assert (
        push_coordinator.update_interval == push_coordinator._lock_breaker.backoff_delay
    )
    # A refresh was kicked once, on the trip transition, so a push provider
    # that otherwise never polls begins probing for recovery.
    mock_refresh.assert_called_once()


async def test_backoff_init_stores_original_interval(
    poll_coordinator: LockUsercodeUpdateCoordinator,
    poll_lock: MockLCMLock,
) -> None:
    """Test that __init__ stores the original update interval."""
    assert (
        poll_coordinator._original_update_interval == poll_lock.usercode_scan_interval
    )
    assert poll_coordinator._lock_breaker.failure_count == 0


async def test_backoff_init_push_stores_none_interval(
    push_coordinator: LockUsercodeUpdateCoordinator,
) -> None:
    """Test that __init__ stores None for push-based providers."""
    assert push_coordinator._original_update_interval is None
    assert push_coordinator._lock_breaker.failure_count == 0


class TestLatePushCapability:
    """A bridged lock that only learns it can push after the coordinator exists."""

    @staticmethod
    def _coordinator_for_a_lock_that_gains_push(
        hass: HomeAssistant,
        push_lock: MockLCMPushLock,
        lcm_config_entry: MockConfigEntry,
    ) -> LockUsercodeUpdateCoordinator:
        """
        Build a coordinator that picked a poll cadence, then grant push.

        A push provider that does not know it yet, which is exactly the
        bridged case: the capability is derived from discovery data that has
        not landed at construction time.
        """
        push_lock._supports_push = False
        coordinator = _make_coordinator(hass, push_lock, lcm_config_entry)
        assert coordinator.update_interval == push_lock.usercode_scan_interval
        push_lock._supports_push = True
        return coordinator

    async def test_polling_stops_once_the_lock_turns_out_to_push(
        self,
        hass: HomeAssistant,
        push_lock: MockLCMPushLock,
        lcm_config_entry: MockConfigEntry,
    ) -> None:
        """
        A bridged provider derives push from discovery data that lands late.

        A lock whose setup was deferred was very likely built before that
        answer existed, so it kept the cadence chosen for a lock with no push
        -- and then polled forever, at one api round trip per slot every five
        minutes, on a mesh this provider goes out of its way not to fill.
        """
        coordinator = self._coordinator_for_a_lock_that_gains_push(
            hass, push_lock, lcm_config_entry
        )
        coordinator._reached_once = True

        coordinator.note_push_capability()

        assert coordinator.update_interval is None
        assert coordinator._original_update_interval is None

    async def test_a_probe_arm_keeps_the_interval_it_owns(
        self,
        hass: HomeAssistant,
        push_lock: MockLCMPushLock,
        lcm_config_entry: MockConfigEntry,
    ) -> None:
        """
        The cadence is only applied when nothing else is currently driving it.

        A lock that has never been reached is being probed on the cold-start
        retry, and clearing the interval out from under that leaves it with
        nothing scheduling the poll that would seed it. The arm restores
        ``_original_update_interval`` when it lets go, and that is what has
        just been updated.
        """
        coordinator = self._coordinator_for_a_lock_that_gains_push(
            hass, push_lock, lcm_config_entry
        )
        coordinator._apply_backoff()
        probe_interval = coordinator.update_interval

        coordinator.note_push_capability()

        assert coordinator.update_interval == probe_interval
        assert coordinator._original_update_interval is None

    async def test_losing_push_is_not_chased(
        self, push_coordinator: LockUsercodeUpdateCoordinator, push_lock
    ) -> None:
        """
        Discovery data going transiently missing is not a lock that stopped pushing.

        Acting on it would restore a poll cadence, and the next publication
        would take it away again -- so the cadence thrashes with the broker.
        """
        push_lock._supports_push = False

        push_coordinator.note_push_capability()

        assert push_coordinator.update_interval is None
        assert push_coordinator._original_update_interval is None


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

    assert push_coordinator._lock_breaker.failure_count == BACKOFF_FAILURE_THRESHOLD + 2

    # Push update with new data should reset backoff
    push_coordinator.data = {pin_address(1): SlotCredential.known("old")}
    push_coordinator.push_update({1: SlotCredential.known("1234")})

    assert push_coordinator._lock_breaker.failure_count == 0


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

    assert push_coordinator._lock_breaker.failure_count == BACKOFF_FAILURE_THRESHOLD + 1

    # Push update with same data should NOT reset backoff
    push_coordinator.data = {pin_address(1): SlotCredential.known("1234")}
    push_coordinator.push_update({1: SlotCredential.known("1234")})

    assert push_coordinator._lock_breaker.failure_count == BACKOFF_FAILURE_THRESHOLD + 1


async def test_poll_failure_alert_created_after_threshold(
    poll_coordinator: LockUsercodeUpdateCoordinator,
    poll_lock: MockLCMLock,
    hass: HomeAssistant,
) -> None:
    """Test that a repair issue is created after POLL_FAILURE_ALERT_THRESHOLD failures."""
    poll_coordinator.last_update_success = True
    poll_coordinator._reached_once = True  # lock was online before going offline

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
    poll_coordinator._reached_once = True  # lock was online before going offline

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
    poll_coordinator._reached_once = True  # lock was online before going offline

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
    mock_get_success = AsyncMock(return_value={1: SlotCredential.known("1234")})
    with patch.object(poll_lock, "async_internal_get_usercodes", mock_get_success):
        await poll_coordinator.async_get_usercodes()

    # Issue should be dismissed
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None


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
    poll_coordinator._reached_once = True  # lock was online before going offline

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


async def test_lock_offline_not_created_when_never_reached(
    poll_coordinator: LockUsercodeUpdateCoordinator,
    poll_lock: MockLCMLock,
    hass: HomeAssistant,
) -> None:
    """
    A lock that has never been reached must not raise lock_offline.

    During the startup window (e.g. the lock's integration is still loading
    after a HA restart) every poll fails with a transient "not connected"
    error. Raising lock_offline there produces a repair that is created and
    then auto-cleared the moment the integration finishes loading -- the flap
    reported in issue #1257. ``_reached_once`` stays False until a real reach,
    so the alert is suppressed.
    """
    assert poll_coordinator._reached_once is False

    mock_get = AsyncMock(side_effect=LockDisconnected("Not connected"))
    with patch.object(poll_lock, "async_internal_get_usercodes", mock_get):
        for _ in range(POLL_FAILURE_ALERT_THRESHOLD + 2):
            with pytest.raises(UpdateFailed):
                await poll_coordinator.async_get_usercodes()

    issue_registry = async_get_issue_registry(hass)
    issue_id = f"lock_offline_{poll_lock.lock.entity_id}"
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is None


async def test_lock_offline_created_after_reach_then_drop(
    poll_coordinator: LockUsercodeUpdateCoordinator,
    poll_lock: MockLCMLock,
    hass: HomeAssistant,
) -> None:
    """Once reached, a later sustained outage raises lock_offline normally."""
    # A first successful poll proves the lock was online.
    mock_get_ok = AsyncMock(return_value={1: SlotCredential.known("1234")})
    with patch.object(poll_lock, "async_internal_get_usercodes", mock_get_ok):
        await poll_coordinator.async_get_usercodes()
    assert poll_coordinator._reached_once is True

    mock_get_fail = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(poll_lock, "async_internal_get_usercodes", mock_get_fail):
        for _ in range(POLL_FAILURE_ALERT_THRESHOLD):
            with pytest.raises(UpdateFailed):
                await poll_coordinator.async_get_usercodes()

    issue_registry = async_get_issue_registry(hass)
    issue_id = f"lock_offline_{poll_lock.lock.entity_id}"
    assert issue_registry.async_get_issue(DOMAIN, issue_id) is not None


async def test_push_update_marks_reached(
    push_coordinator: LockUsercodeUpdateCoordinator,
) -> None:
    """A push update proves the lock is reachable and marks it reached."""
    assert push_coordinator._reached_once is False
    push_coordinator.push_update({1: SlotCredential.known("9999")})
    assert push_coordinator._reached_once is True


async def test_drift_check_success_marks_reached(
    push_coordinator: LockUsercodeUpdateCoordinator,
    push_lock: MockLCMPushLock,
) -> None:
    """A successful drift hard refresh is a reach and marks the lock reached."""
    push_coordinator.last_update_success = True
    assert push_coordinator._reached_once is False

    with patch.object(
        push_lock,
        "async_internal_hard_refresh_codes",
        AsyncMock(return_value={1: SlotCredential.known("1234")}),
    ):
        await push_coordinator._async_drift_check(dt_util.utcnow())

    assert push_coordinator._reached_once is True


async def test_unreachable_reflects_backoff_trip(
    poll_coordinator: LockUsercodeUpdateCoordinator,
    poll_lock: MockLCMLock,
) -> None:
    """unreachable is False until the lock breaker trips, then True."""
    poll_coordinator.last_update_success = True
    assert poll_coordinator.unreachable is False

    mock_get = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(poll_lock, "async_internal_get_usercodes", mock_get):
        for _ in range(BACKOFF_FAILURE_THRESHOLD):
            with pytest.raises(UpdateFailed):
                await poll_coordinator.async_get_usercodes()

    assert poll_coordinator.unreachable is True


async def test_unreachable_clears_on_recovery(
    poll_coordinator: LockUsercodeUpdateCoordinator,
    poll_lock: MockLCMLock,
) -> None:
    """A successful poll resets the breaker and clears unreachable."""
    poll_coordinator.last_update_success = True
    mock_get = AsyncMock(side_effect=LockDisconnected("Lock offline"))
    with patch.object(poll_lock, "async_internal_get_usercodes", mock_get):
        for _ in range(BACKOFF_FAILURE_THRESHOLD):
            with pytest.raises(UpdateFailed):
                await poll_coordinator.async_get_usercodes()
    assert poll_coordinator.unreachable is True

    mock_get_ok = AsyncMock(return_value={1: SlotCredential.known("1234")})
    with patch.object(poll_lock, "async_internal_get_usercodes", mock_get_ok):
        await poll_coordinator.async_get_usercodes()

    assert poll_coordinator.unreachable is False
    assert (
        poll_coordinator.update_interval == poll_coordinator._original_update_interval
    )


async def test_desired_credential_disabled_slot_is_empty(
    hass: HomeAssistant,
    poll_coordinator: LockUsercodeUpdateCoordinator,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """A disabled slot's desired credential is empty even with a configured PIN."""
    hass.config_entries.async_update_entry(
        lcm_config_entry,
        data={
            CONF_LOCKS: [],
            CONF_SLOTS: {1: {CONF_NAME: "x", CONF_PIN: "1234", CONF_ENABLED: False}},
        },
    )
    assert poll_coordinator.desired_credential(pin_address(1)) == SlotCredential.empty()


async def test_desired_credential_enabled_blank_pin_is_empty(
    hass: HomeAssistant,
    poll_coordinator: LockUsercodeUpdateCoordinator,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """An enabled slot with no configured PIN has an empty desired credential."""
    hass.config_entries.async_update_entry(
        lcm_config_entry,
        data={
            CONF_LOCKS: [],
            CONF_SLOTS: {1: {CONF_NAME: "x", CONF_PIN: "", CONF_ENABLED: True}},
        },
    )
    assert poll_coordinator.desired_credential(pin_address(1)) == SlotCredential.empty()


async def test_desired_credential_enabled_with_pin_is_known(
    hass: HomeAssistant,
    poll_coordinator: LockUsercodeUpdateCoordinator,
    lcm_config_entry: MockConfigEntry,
) -> None:
    """An enabled slot with a configured PIN yields that PIN as the desired credential."""
    hass.config_entries.async_update_entry(
        lcm_config_entry,
        data={
            CONF_LOCKS: [],
            CONF_SLOTS: {1: {CONF_NAME: "x", CONF_PIN: "4242", CONF_ENABLED: True}},
        },
    )
    assert poll_coordinator.desired_credential(pin_address(1)) == SlotCredential.known(
        "4242"
    )


async def test_connection_check_swallows_lock_code_manager_error(
    push_coordinator: LockUsercodeUpdateCoordinator,
    push_lock: MockLCMPushLock,
) -> None:
    """A LockCodeManagerError from the reachability probe is logged and swallowed."""
    mock_check = AsyncMock(side_effect=LockDisconnected("offline"))
    with patch.object(push_lock, "async_is_integration_connected", mock_check):
        # Must not raise: this runs on a periodic timer with no caller to
        # observe an exception.
        await push_coordinator._async_connection_check(dt_util.utcnow())

    mock_check.assert_called_once()


@pytest.mark.parametrize(
    "accessor",
    ["is_verified", "has_pending_write", "pending_write", "desired_credential"],
)
async def test_non_pin_address_is_rejected(push_coordinator, accessor) -> None:
    """A non-PIN address is a programming error, not a missing entry.

    Storage is still slot-keyed and PIN-only, so serving another credential
    type would silently read and write the PIN's storage.
    """
    address = CredentialAddress(1, CredentialType.RFID)
    with pytest.raises(ValueError, match="Only PIN credentials are addressable"):
        getattr(push_coordinator, accessor)(address)


async def test_string_slot_number_still_resolves(push_coordinator) -> None:
    """A str user_ref indexes the int-keyed storage.

    Slot numbers reach entities as either int or str, which is why the
    integration is littered with int(self.slot_num). Without coercion here,
    is_verified would miss every int key and report True for a slot whose
    optimistic write is still unconfirmed.
    """
    push_coordinator.record_write(pin_address(1), "1234", believed=True)
    assert push_coordinator.is_verified(pin_address("1")) is False
    push_coordinator.drop_pending(pin_address("1"))
    assert push_coordinator.is_verified(pin_address(1)) is True


async def test_credentials_by_slot_filters_to_one_type(push_coordinator) -> None:
    """The slot-keyed projection returns one credential type, never a blend.

    Every stored credential is a Personal Identification Number today, so the
    filter never excludes anything in practice and line coverage cannot pin
    it. Seeding a second type directly is the only way to prove the websocket
    and diagnostics boundaries will not start emitting, say, a Radio Frequency
    Identification value under a PIN slot key once a second type is stored.
    """
    push_coordinator.data = {
        pin_address(1): SlotCredential.known("1111"),
        CredentialAddress(2, CredentialType.RFID): SlotCredential.known("A4B2C1"),
    }

    assert push_coordinator.credentials_by_slot() == {1: SlotCredential.known("1111")}
    assert push_coordinator.credentials_by_slot(CredentialType.RFID) == {
        2: SlotCredential.known("A4B2C1")
    }


async def test_credential_distinguishes_absent_from_empty(push_coordinator) -> None:
    """None means "never reported"; empty() is a positive observation of no code."""
    push_coordinator.data = {pin_address(1): SlotCredential.empty()}

    assert push_coordinator.credential(pin_address(1)) is SlotCredential.empty()
    assert push_coordinator.credential(pin_address(9)) is None
    assert push_coordinator.has_credential(pin_address(1)) is True
    assert push_coordinator.has_credential(pin_address(9)) is False


# =============================================================================
# Pending writes: the coordinator owns them from "sent" to "seen"
# =============================================================================


async def test_record_write_believed_pushes_the_value_and_is_unverified(
    push_coordinator: LockUsercodeUpdateCoordinator,
) -> None:
    """An optimistic write shows its PIN at once, and is not verified until seen."""
    push_coordinator.record_write(pin_address(1), "9999", believed=True)
    assert push_coordinator.data[pin_address(1)] == SlotCredential.known("9999")
    assert push_coordinator.is_verified(pin_address(1)) is False
    assert push_coordinator.pending_write(pin_address(1)).pin == "9999"


async def test_record_write_not_believed_leaves_data_alone(
    poll_coordinator: LockUsercodeUpdateCoordinator,
) -> None:
    """A polled lock's confirmed write is recorded without a value: the read will say."""
    poll_coordinator.push_update({1: SlotCredential.empty()})
    poll_coordinator.record_write(pin_address(1), "9999", believed=False)
    assert poll_coordinator.data[pin_address(1)] == SlotCredential.empty()
    assert poll_coordinator.is_verified(pin_address(1)) is False


async def test_is_verified_is_exactly_not_pending(
    poll_coordinator: LockUsercodeUpdateCoordinator,
) -> None:
    """Verified derives from the pending record; nothing is mirrored to drift."""
    address = pin_address(1)
    assert poll_coordinator.is_verified(address) is True
    poll_coordinator.record_write(address, "1234", believed=False)
    assert poll_coordinator.is_verified(address) is False
    poll_coordinator.drop_pending(address)
    assert poll_coordinator.is_verified(address) is True


async def test_record_write_arms_one_timer_for_the_whole_lock(
    poll_coordinator: LockUsercodeUpdateCoordinator,
) -> None:
    """N pending slots on one lock are one read apart, not N."""
    assert poll_coordinator._confirm_task is None
    poll_coordinator.record_write(pin_address(1), "1111", believed=False)
    first = poll_coordinator._confirm_task
    assert first is not None
    poll_coordinator.record_write(pin_address(2), "2222", believed=False)
    assert poll_coordinator._confirm_task is first


async def test_apply_read_confirms_a_present_slot_with_the_written_value(
    push_coordinator: LockUsercodeUpdateCoordinator,
) -> None:
    """Present -- even masked -- confirms the write; the written PIN is kept."""
    push_coordinator.record_write(pin_address(1), "9999", believed=True)
    out = push_coordinator._apply_read({pin_address(1): SlotCredential.unreadable()})
    assert out[pin_address(1)] == SlotCredential.known("9999")
    assert push_coordinator.is_verified(pin_address(1)) is True


async def test_apply_read_takes_differing_readable_external_change(
    push_coordinator: LockUsercodeUpdateCoordinator,
) -> None:
    """A readable read of a DIFFERENT code is an external change, and wins."""
    push_coordinator.record_write(pin_address(1), "1234", believed=True)
    out = push_coordinator._apply_read({pin_address(1): SlotCredential.known("9999")})
    assert out[pin_address(1)] == SlotCredential.known("9999")
    assert push_coordinator.is_verified(pin_address(1)) is True
    # The slot holds something else: our write did not take.
    assert push_coordinator.take_failed_write(pin_address(1)) is True


async def test_apply_read_keeps_an_absent_slot_pending_before_the_deadline(
    poll_coordinator: LockUsercodeUpdateCoordinator,
) -> None:
    """Absent before the deadline: the write may not have landed yet. Keep waiting."""
    poll_coordinator.record_write(pin_address(1), "9999", believed=False)
    out = poll_coordinator._apply_read({pin_address(1): SlotCredential.empty()})
    assert out[pin_address(1)] == SlotCredential.empty()
    assert poll_coordinator.is_verified(pin_address(1)) is False
    assert poll_coordinator.take_failed_write(pin_address(1)) is False


async def test_apply_read_fails_an_absent_slot_past_the_deadline_once(
    poll_coordinator: LockUsercodeUpdateCoordinator, freezer
) -> None:
    """Absent at the deadline: give the write up, take the observation, charge once.

    Expiry happens only inside a completed read, so nothing outside a read
    ever has to guess whether the lock was asked. ``take_failed_write`` hands the
    sync tick exactly one charge.
    """
    poll_coordinator.record_write(pin_address(1), "9999", believed=False)
    freezer.tick(timedelta(seconds=PENDING_WRITE_TTL + 1))
    out = poll_coordinator._apply_read({pin_address(1): SlotCredential.empty()})
    assert out[pin_address(1)] == SlotCredential.empty()
    assert poll_coordinator.is_verified(pin_address(1)) is True
    assert poll_coordinator.take_failed_write(pin_address(1)) is True
    assert poll_coordinator.take_failed_write(pin_address(1)) is False


async def test_drop_pending_leaves_nothing_to_charge(
    poll_coordinator: LockUsercodeUpdateCoordinator, freezer
) -> None:
    """A write no longer wanted is forgotten, not judged."""
    poll_coordinator.record_write(pin_address(1), "9999", believed=False)
    freezer.tick(timedelta(seconds=PENDING_WRITE_TTL + 1))
    poll_coordinator.drop_pending(pin_address(1))
    assert poll_coordinator.is_verified(pin_address(1)) is True
    assert poll_coordinator.take_failed_write(pin_address(1)) is False


async def test_observe_push_present_confirms_absent_ends(
    push_coordinator: LockUsercodeUpdateCoordinator,
) -> None:
    """A push is the lock speaking now: present confirms, absent is taken as its word."""
    push_coordinator.record_write(pin_address(1), "1234", believed=True)
    push_coordinator.observe_push(pin_address(1), SlotCredential.unreadable())
    assert push_coordinator.data[pin_address(1)] == SlotCredential.known("1234")
    assert push_coordinator.is_verified(pin_address(1)) is True

    push_coordinator.record_write(pin_address(2), "5678", believed=True)
    push_coordinator.observe_push(pin_address(2), SlotCredential.empty())
    assert push_coordinator.data[pin_address(2)] == SlotCredential.empty()
    assert push_coordinator.is_verified(pin_address(2)) is True

    push_coordinator.record_write(pin_address(3), "1111", believed=True)
    push_coordinator.observe_push(pin_address(3), SlotCredential.known("2222"))
    assert push_coordinator.data[pin_address(3)] == SlotCredential.known("2222")
    assert push_coordinator.take_failed_write(pin_address(3)) is True
    # ...and an absent push after a write counts it failed as well.
    assert push_coordinator.take_failed_write(pin_address(2)) is True
    assert push_coordinator.take_failed_write(pin_address(1)) is False


async def test_confirmation_read_on_a_poller_uses_the_ordinary_read_with_pending_scope(
    poll_lock: MockLCMLock, poll_coordinator: LockUsercodeUpdateCoordinator
) -> None:
    """A polled lock is read through its poll path, asked about every pending slot.

    Slot 9 is managed by nobody; a direct write there is still pending, so the
    read names it and the write is confirmed or given up like any other.
    """
    poll_lock.codes[9] = "4321"
    poll_coordinator.record_write(pin_address(9), "4321", believed=False)
    hard = poll_lock.service_calls["hard_refresh_codes"]
    reads = poll_lock.service_calls["get_usercodes"]
    before = (len(hard), len(reads))

    await poll_coordinator.async_confirm_pending_writes()

    assert len(hard) == before[0]
    assert len(reads) == before[1] + 1
    assert poll_coordinator.is_verified(pin_address(9)) is True
    assert poll_coordinator.data[pin_address(9)] == SlotCredential.known("4321")


async def test_confirmation_read_on_a_push_provider_hard_refreshes(
    push_lock: MockLCMPushLock, push_coordinator: LockUsercodeUpdateCoordinator
) -> None:
    """A push provider's ordinary read is a cache; confirmation goes to the device."""
    push_coordinator.record_write(pin_address(1), "9999", believed=True)
    with patch.object(
        push_lock,
        "async_hard_refresh_codes",
        AsyncMock(return_value={1: SlotCredential.unreadable()}),
    ) as hard:
        await push_coordinator.async_confirm_pending_writes()
    hard.assert_awaited_once()
    assert push_coordinator.is_verified(pin_address(1)) is True
    assert push_coordinator.data[pin_address(1)] == SlotCredential.known("9999")


async def test_confirmation_read_on_a_push_provider_asks_only_about_the_pending_slots(
    push_lock: MockLCMPushLock, push_coordinator: LockUsercodeUpdateCoordinator
) -> None:
    """The confirmation read is scoped to the pending slots; the drift read is not.

    On a lock that drops half its responses, re-reading one slot completes
    about half the time and a walk of the whole lock essentially never; the
    scope is what lets a write on such a lock ever confirm (issue #1549).
    """
    reads = push_lock.service_calls["hard_refresh_codes"]
    push_coordinator.record_write(pin_address(1), "9999", believed=True)
    push_coordinator.record_write(pin_address(3), "3333", believed=True)
    await push_coordinator.async_confirm_pending_writes()
    assert reads[-1] == ({1, 3},)

    await push_coordinator._async_drift_check(dt_util.utcnow())
    assert reads[-1] == (None,)


async def test_confirmation_read_noop_without_pending(
    push_lock: MockLCMPushLock, push_coordinator: LockUsercodeUpdateCoordinator
) -> None:
    """Nothing pending, nothing read."""
    with patch.object(push_lock, "async_hard_refresh_codes", AsyncMock()) as hard:
        await push_coordinator.async_confirm_pending_writes()
    hard.assert_not_called()


@pytest.mark.parametrize("failure", [LockDisconnected("offline"), RuntimeError("boom")])
async def test_confirmation_read_failure_is_non_fatal_and_keeps_waiting(
    push_lock: MockLCMPushLock,
    push_coordinator: LockUsercodeUpdateCoordinator,
    failure: Exception,
) -> None:
    """A failed read is not the lock's word: stay pending, touch no breaker."""
    push_coordinator.record_write(pin_address(1), "9999", believed=True)
    with patch.object(
        push_lock, "async_hard_refresh_codes", AsyncMock(side_effect=failure)
    ):
        await push_coordinator.async_confirm_pending_writes()
    assert push_coordinator.is_verified(pin_address(1)) is False
    assert push_coordinator.unreachable is False
    assert push_coordinator.take_failed_write(pin_address(1)) is False


async def test_confirmation_read_failure_past_the_deadline_gives_the_write_up(
    push_lock: MockLCMPushLock, push_coordinator: LockUsercodeUpdateCoordinator, freezer
) -> None:
    """Reads that keep failing do not keep a write alive forever.

    The lock has had the time to live to be seen holding the write and has not
    been; a lock whose writes land but whose reads never return still ends in
    a charged re-sync and a visible suspend, not a silent pending state.
    """
    push_coordinator.record_write(pin_address(1), "9999", believed=True)
    freezer.tick(timedelta(seconds=PENDING_WRITE_TTL + 1))
    with patch.object(
        push_lock,
        "async_hard_refresh_codes",
        AsyncMock(side_effect=LockDisconnected("offline")),
    ):
        await push_coordinator.async_confirm_pending_writes()
    assert push_coordinator.is_verified(pin_address(1)) is True
    assert push_coordinator.take_failed_write(pin_address(1)) is True


async def test_confirmation_timer_fires_reads_and_rearms_while_pending(
    hass: HomeAssistant,
    poll_lock: MockLCMLock,
    poll_coordinator: LockUsercodeUpdateCoordinator,
) -> None:
    """The coordinator schedules its own looks: now, then every interval, until settled."""
    poll_lock.codes.pop(1, None)  # every read comes back absent
    reads = poll_lock.service_calls["get_usercodes"]
    before = len(reads)

    poll_coordinator.record_write(pin_address(1), "9999", believed=False)
    await hass.async_block_till_done()  # the immediate first look
    assert len(reads) == before + 1
    assert poll_coordinator._confirm_unsub is not None  # re-armed: still pending

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=CONFIRM_READ_INTERVAL + 1)
    )
    await hass.async_block_till_done()
    assert len(reads) == before + 2

    # The lock now holds it: the next look confirms and the timer stands down.
    poll_lock.codes[1] = "9999"
    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=2 * CONFIRM_READ_INTERVAL + 2)
    )
    await hass.async_block_till_done()
    assert poll_coordinator.is_verified(pin_address(1)) is True
    assert poll_coordinator._confirm_unsub is None


async def test_shutdown_cancels_the_armed_timer_and_forgets_pending(
    hass: HomeAssistant,
    poll_lock: MockLCMLock,
    poll_coordinator: LockUsercodeUpdateCoordinator,
) -> None:
    """No read fires after the coordinator is gone, and nothing stays pending."""
    poll_lock.codes.pop(1, None)
    reads = poll_lock.service_calls["get_usercodes"]
    poll_coordinator.record_write(pin_address(1), "9999", believed=False)
    await hass.async_block_till_done()  # first look ran, found it absent, armed
    assert poll_coordinator._confirm_unsub is not None
    before = len(reads)

    await poll_coordinator.async_shutdown()
    assert poll_coordinator._confirm_unsub is None
    assert poll_coordinator.has_pending_write(pin_address(1)) is False

    async_fire_time_changed(
        hass, dt_util.utcnow() + timedelta(seconds=CONFIRM_READ_INTERVAL + 1)
    )
    await hass.async_block_till_done()
    assert len(reads) == before


async def test_shutdown_cancels_a_timer_fired_read_in_flight(
    hass: HomeAssistant,
    poll_lock: MockLCMLock,
    poll_coordinator: LockUsercodeUpdateCoordinator,
) -> None:
    """A read the timer started is the tracked look: shutdown cancels it mid-flight."""
    poll_lock.codes.pop(1, None)
    poll_coordinator.record_write(pin_address(1), "9999", believed=False)
    await hass.async_block_till_done()  # timer armed

    gate = asyncio.Event()
    completed: list[int] = []

    async def slow_read(*_args, **_kwargs):
        await gate.wait()
        completed.append(1)
        return {1: SlotCredential.empty()}

    with patch.object(poll_lock, "async_get_usercodes", slow_read):
        async_fire_time_changed(
            hass, dt_util.utcnow() + timedelta(seconds=CONFIRM_READ_INTERVAL + 1)
        )
        for _ in range(3):
            await asyncio.sleep(0)  # let the timer start the look and enter the read
        assert poll_coordinator._confirm_task is not None

        await poll_coordinator.async_shutdown()
        gate.set()
        await hass.async_block_till_done()

    assert completed == []
    assert poll_coordinator._confirm_unsub is None


async def test_record_write_while_the_timer_is_armed_pulls_the_look_forward(
    hass: HomeAssistant,
    poll_lock: MockLCMLock,
    poll_coordinator: LockUsercodeUpdateCoordinator,
) -> None:
    """A write landing while the timer waits gets its look now, on the one chain."""
    poll_lock.codes.pop(1, None)
    poll_coordinator.record_write(pin_address(1), "1111", believed=False)
    await hass.async_block_till_done()
    assert poll_coordinator._confirm_unsub is not None
    reads = poll_lock.service_calls["get_usercodes"]
    before = len(reads)

    poll_coordinator.record_write(pin_address(2), "2222", believed=False)
    assert poll_coordinator._confirm_task is not None
    assert poll_coordinator._confirm_unsub is None  # the waiting timer was cancelled

    await hass.async_block_till_done()
    assert len(reads) == before + 1
    assert poll_coordinator._confirm_task is None
    assert poll_coordinator._confirm_unsub is not None  # one chain, re-armed once


async def test_pending_slot_a_completed_read_never_names_is_given_up_at_the_deadline(
    push_lock: MockLCMPushLock,
    push_coordinator: LockUsercodeUpdateCoordinator,
    freezer,
) -> None:
    """A whole-device read that omits a pending slot is the lock not holding it.

    Waited for until the deadline like an absent slot, then failed -- never
    left pending to hard-refresh the device every interval for good.
    """
    push_coordinator.record_write(pin_address(9), "4321", believed=True)
    with patch.object(
        push_lock,
        "async_hard_refresh_codes",
        AsyncMock(return_value={1: SlotCredential.known("1234")}),
    ):
        await push_coordinator.async_confirm_pending_writes()
        assert push_coordinator.has_pending_write(pin_address(9)) is True
        assert push_coordinator.take_failed_write(pin_address(9)) is False

        freezer.tick(timedelta(seconds=PENDING_WRITE_TTL + 1))
        await push_coordinator.async_confirm_pending_writes()
    assert push_coordinator.has_pending_write(pin_address(9)) is False
    assert push_coordinator.take_failed_write(pin_address(9)) is True


async def test_a_read_with_an_unusable_key_does_not_strand_the_look(
    hass: HomeAssistant,
    push_lock: MockLCMPushLock,
    push_coordinator: LockUsercodeUpdateCoordinator,
) -> None:
    """Whatever the read returns, the look either settles the write or re-arms."""
    push_coordinator.record_write(pin_address(1), "9999", believed=True)
    with patch.object(
        push_lock,
        "async_hard_refresh_codes",
        AsyncMock(return_value={"bogus": SlotCredential.empty()}),
    ):
        await hass.async_block_till_done()
    assert push_coordinator.has_pending_write(pin_address(1)) is True
    assert push_coordinator._confirm_task is None
    assert push_coordinator._confirm_unsub is not None


async def test_observe_push_with_nothing_pending_is_the_locks_word(
    push_coordinator: LockUsercodeUpdateCoordinator,
) -> None:
    """An external change or deletion pushed with nothing pending lands as read.

    Every push provider's event path ends here, so this is the path by which a
    code changed or deleted at the keypad reaches the data at all.
    """
    push_coordinator.push_update({1: SlotCredential.known("1234")})
    push_coordinator.observe_push(pin_address(1), SlotCredential.known("4321"))
    assert push_coordinator.data[pin_address(1)] == SlotCredential.known("4321")
    assert push_coordinator.is_verified(pin_address(1)) is True

    push_coordinator.observe_push(pin_address(1), SlotCredential.empty())
    assert push_coordinator.data[pin_address(1)] == SlotCredential.empty()
    assert push_coordinator.is_verified(pin_address(1)) is True
    assert push_coordinator.take_failed_write(pin_address(1)) is False


async def test_newer_record_write_replaces_the_pending_pin(
    push_coordinator: LockUsercodeUpdateCoordinator,
) -> None:
    """Two quick PIN changes: the read is judged against the PIN sent last."""
    push_coordinator.record_write(pin_address(1), "1111", believed=True)
    push_coordinator.record_write(pin_address(1), "2222", believed=True)
    assert push_coordinator.pending_write(pin_address(1)).pin == "2222"
    assert push_coordinator.data[pin_address(1)] == SlotCredential.known("2222")

    out = push_coordinator._apply_read({pin_address(1): SlotCredential.known("2222")})
    assert out[pin_address(1)] == SlotCredential.known("2222")
    assert push_coordinator.is_verified(pin_address(1)) is True
    assert push_coordinator.take_failed_write(pin_address(1)) is False


async def test_record_write_forgets_an_earlier_failed_write(
    poll_coordinator: LockUsercodeUpdateCoordinator, freezer
) -> None:
    """A new write to the address supersedes an uncharged failure of the old one.

    Otherwise a direct set landing between the failure and the tick would be
    charged for a write the lock then kept.
    """
    poll_coordinator.record_write(pin_address(1), "1111", believed=False)
    freezer.tick(timedelta(seconds=PENDING_WRITE_TTL + 1))
    poll_coordinator._apply_read({pin_address(1): SlotCredential.empty()})

    poll_coordinator.record_write(pin_address(1), "2222", believed=False)
    poll_coordinator._apply_read({pin_address(1): SlotCredential.known("2222")})
    assert poll_coordinator.is_verified(pin_address(1)) is True
    assert poll_coordinator.take_failed_write(pin_address(1)) is False


async def test_push_provider_confirmed_set_is_present_and_settled_through_the_seam(
    push_lock: MockLCMPushLock, push_coordinator: LockUsercodeUpdateCoordinator
) -> None:
    """The push-provider contract, end to end on a real coordinator.

    A push provider pushes the value it wrote before returning CONFIRMED, so
    after the seam returns the value is on the coordinator, nothing is
    pending, and no confirmation look was started for it.
    """
    push_lock.coordinator = push_coordinator
    push_lock._min_operation_delay = 0.0
    await push_lock.async_internal_set_usercode(2, "8642", "dana")

    assert push_coordinator.data[pin_address(2)] == SlotCredential.known("8642")
    assert push_coordinator.is_verified(pin_address(2)) is True
    assert push_coordinator._confirm_task is None
    assert push_coordinator._confirm_unsub is None


async def test_record_write_during_a_timer_fired_read_joins_that_look(
    hass: HomeAssistant,
    poll_lock: MockLCMLock,
    poll_coordinator: LockUsercodeUpdateCoordinator,
) -> None:
    """A write landing while the timer's read is in flight starts no second chain.

    The timer's read is the tracked look, so the write waits for it and the
    re-arm after it covers both slots.
    """
    poll_lock.codes.pop(1, None)
    poll_coordinator.record_write(pin_address(1), "1111", believed=False)
    await hass.async_block_till_done()  # timer armed

    gate = asyncio.Event()

    async def slow_read(*_args, **_kwargs):
        await gate.wait()
        return {1: SlotCredential.empty(), 2: SlotCredential.empty()}

    with patch.object(poll_lock, "async_get_usercodes", slow_read):
        async_fire_time_changed(
            hass, dt_util.utcnow() + timedelta(seconds=CONFIRM_READ_INTERVAL + 1)
        )
        for _ in range(3):
            await asyncio.sleep(0)
        in_flight = poll_coordinator._confirm_task
        assert in_flight is not None

        poll_coordinator.record_write(pin_address(2), "2222", believed=False)
        assert poll_coordinator._confirm_task is in_flight
        assert poll_coordinator._confirm_unsub is None

        gate.set()
        await hass.async_block_till_done()

    assert poll_coordinator._confirm_task is None
    assert poll_coordinator._confirm_unsub is not None  # one chain, re-armed once


async def test_confirmation_read_drops_a_slot_the_lock_no_longer_reports(
    hass: HomeAssistant,
    poll_lock: MockLCMLock,
    poll_coordinator: LockUsercodeUpdateCoordinator,
) -> None:
    """An unmanaged code deleted out of band disappears with the confirmation read.

    The scoped read has the same shape as a poll -- every managed and pending
    slot plus whatever else the lock holds -- so it replaces the data as a
    poll would, rather than keeping a slot the lock has stopped reporting.
    """
    poll_lock.codes[9] = "4321"
    await poll_coordinator.async_refresh()
    assert poll_coordinator.data[pin_address(9)] == SlotCredential.known("4321")

    del poll_lock.codes[9]
    poll_coordinator.record_write(pin_address(1), "1234", believed=False)
    await hass.async_block_till_done()

    assert pin_address(9) not in poll_coordinator.data
    assert poll_coordinator.is_verified(pin_address(1)) is True


async def test_record_write_after_shutdown_records_nothing(
    poll_coordinator: LockUsercodeUpdateCoordinator,
) -> None:
    """A write returning after unload starts no look against a torn-down lock."""
    await poll_coordinator.async_shutdown()
    poll_coordinator.record_write(pin_address(1), "9999", believed=False)
    assert poll_coordinator.has_pending_write(pin_address(1)) is False
    assert poll_coordinator._confirm_task is None
    assert poll_coordinator._confirm_unsub is None


async def test_quiet_read_with_an_old_failed_write_does_not_wake_listeners(
    poll_lock: MockLCMLock, poll_coordinator: LockUsercodeUpdateCoordinator, freezer
) -> None:
    """Listeners are woken for a write that just failed, not for every read after.

    A failed direct write to a slot nothing manages has no one to consume
    it; it must not turn every later unchanged read into a full notify.
    """
    await poll_coordinator.async_refresh()
    wakes: list[int] = []
    poll_coordinator.async_add_listener(lambda: wakes.append(1))

    poll_lock.codes.pop(9, None)
    poll_coordinator.record_write(pin_address(9), "4321", believed=False)
    freezer.tick(timedelta(seconds=PENDING_WRITE_TTL + 1))
    await poll_coordinator.async_confirm_pending_writes()  # fails slot 9's write
    assert poll_coordinator.is_verified(pin_address(9)) is True
    await poll_coordinator.async_refresh()  # settle: slot 9 leaves the read scope
    woken = len(wakes)

    # A later write the lock does keep; the read changes nothing in the data.
    poll_coordinator.record_write(pin_address(1), poll_lock.codes[1], believed=False)
    await poll_coordinator.async_confirm_pending_writes()
    assert poll_coordinator.is_verified(pin_address(1)) is True
    assert len(wakes) == woken


async def test_unchanged_push_on_a_slot_with_an_old_failed_write_does_not_wake_listeners(
    push_coordinator: LockUsercodeUpdateCoordinator,
) -> None:
    """Only the push that fails a write wakes listeners on unchanged data."""
    wakes: list[int] = []
    push_coordinator.async_add_listener(lambda: wakes.append(1))

    push_coordinator.record_write(pin_address(9), "4321", believed=True)
    push_coordinator.observe_push(pin_address(9), SlotCredential.empty())  # fails it
    assert push_coordinator.data[pin_address(9)] == SlotCredential.empty()
    woken = len(wakes)

    push_coordinator.observe_push(pin_address(9), SlotCredential.empty())  # restated
    assert len(wakes) == woken


async def test_apply_read_keeps_showing_a_believed_write_while_waiting(
    push_coordinator: LockUsercodeUpdateCoordinator,
) -> None:
    """A believed write absent before the deadline stays shown, still pending.

    Flipping to the read and back on confirmation would be flicker for the
    code sensor; the address is unverified either way.
    """
    push_coordinator.record_write(pin_address(1), "9999", believed=True)
    out = push_coordinator._apply_read({pin_address(1): SlotCredential.empty()})
    assert out[pin_address(1)] == SlotCredential.known("9999")
    assert push_coordinator.is_verified(pin_address(1)) is False


async def test_poll_finding_the_lock_busy_keeps_its_data_without_backoff(
    poll_lock: MockLCMLock, poll_coordinator: LockUsercodeUpdateCoordinator
) -> None:
    """A poll that never got its turn is not a failed poll (issue #1535)."""
    await poll_coordinator.async_refresh()
    before = dict(poll_coordinator.data)
    with patch.object(
        poll_lock, "async_get_usercodes", AsyncMock(side_effect=LockBusy("busy"))
    ):
        data = await poll_coordinator.async_get_usercodes()
    assert data == before
    assert poll_coordinator._lock_breaker.failure_count == 0


async def test_first_poll_finding_the_lock_busy_fails_without_backoff(
    poll_lock: MockLCMLock, poll_coordinator: LockUsercodeUpdateCoordinator
) -> None:
    """Before anything was ever read there is nothing to keep, and no false success."""
    with (
        patch.object(
            poll_lock, "async_get_usercodes", AsyncMock(side_effect=LockBusy("busy"))
        ),
        pytest.raises(UpdateFailed),
    ):
        await poll_coordinator.async_get_usercodes()
    assert poll_coordinator._lock_breaker.failure_count == 0


async def test_confirmation_look_finding_the_lock_busy_keeps_waiting(
    push_lock: MockLCMPushLock,
    push_coordinator: LockUsercodeUpdateCoordinator,
    freezer,
) -> None:
    """A look that never reached the lock gives nothing up, even past the deadline."""
    push_coordinator.record_write(pin_address(1), "9999", believed=True)
    freezer.tick(timedelta(seconds=PENDING_WRITE_TTL + 1))
    with patch.object(
        push_lock, "async_hard_refresh_codes", AsyncMock(side_effect=LockBusy("busy"))
    ):
        await push_coordinator.async_confirm_pending_writes()
    assert push_coordinator.has_pending_write(pin_address(1)) is True
    assert push_coordinator.take_failed_write(pin_address(1)) is False


async def test_drift_check_finding_the_lock_busy_applies_no_backoff(
    push_lock: MockLCMPushLock, push_coordinator: LockUsercodeUpdateCoordinator
) -> None:
    """Drift detection that never got its turn is not a failed refresh."""
    await push_coordinator.async_refresh()
    with patch.object(
        push_lock, "async_hard_refresh_codes", AsyncMock(side_effect=LockBusy("busy"))
    ):
        await push_coordinator._async_drift_check(dt_util.utcnow())
    assert push_coordinator._lock_breaker.failure_count == 0
    assert push_coordinator.unreachable is False


async def test_poll_finding_the_lock_busy_during_backoff_is_not_a_recovery(
    poll_lock: MockLCMLock, poll_coordinator: LockUsercodeUpdateCoordinator
) -> None:
    """While the breaker says the lock is in backoff, a busy poll changes nothing.

    Returning data would flip the coordinator to "recovered" with the
    breaker still tripped; failing without backoff keeps both honest.
    """
    await poll_coordinator.async_refresh()
    for _ in range(BACKOFF_FAILURE_THRESHOLD):
        poll_coordinator._apply_backoff()
    assert poll_coordinator._lock_breaker.tripped
    failures = poll_coordinator._lock_breaker.failure_count

    with (
        patch.object(
            poll_lock, "async_get_usercodes", AsyncMock(side_effect=LockBusy("busy"))
        ),
        pytest.raises(UpdateFailed),
    ):
        await poll_coordinator.async_get_usercodes()
    assert poll_coordinator._lock_breaker.failure_count == failures
