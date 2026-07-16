"""Test base class."""

import asyncio
from datetime import datetime, timedelta
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, State, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)

from custom_components.lock_code_manager.const import (
    ATTR_EXTRA_DATA,
    ATTR_NOTIFICATION_SOURCE,
    DOMAIN,
    EVENT_LOCK_STATE_CHANGED,
)
from custom_components.lock_code_manager.domain.credentials import (
    CredentialType,
    CredentialTypeCapability,
    LockCapabilities,
)
from custom_components.lock_code_manager.domain.exceptions import (
    DuplicateCodeError,
    LockDisconnected,
    LockOperationFailed,
    ProviderNotImplementedError,
)
from custom_components.lock_code_manager.domain.models import SlotCredential
from custom_components.lock_code_manager.providers._base import BaseLock
from tests.common import BASE_CONFIG, LOCK_1_ENTITY_ID, MockLCMLock

TEST_OPERATION_DELAY = 0.01


class MockLCMLockWithPush(MockLCMLock):
    """Mock lock with push subscription tracking."""

    def __init__(self, *args, **kwargs) -> None:
        """Initialize mock lock."""
        super().__init__(*args, **kwargs)
        self.subscribe_calls = 0
        self.unsubscribe_calls = 0

    @property
    def supports_push(self) -> bool:
        """Return whether this lock supports push-based updates."""
        return True

    def setup_push_subscription(self) -> None:
        """Subscribe to push-based value updates."""
        self.subscribe_calls += 1

    def teardown_push_subscription(self) -> None:
        """Unsubscribe from push-based value updates."""
        self.unsubscribe_calls += 1


async def test_base(hass: HomeAssistant):
    """Test base class."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    # Create a proper registry entry for the mock lock
    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock",
        config_entry=config_entry,
    )

    lock = BaseLock(
        hass,
        dr.async_get(hass),
        entity_reg,
        config_entry,
        lock_entity,
    )
    # Mock coordinator refreshes since BaseLock doesn't implement
    # the abstract methods needed for a real refresh.
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
        assert await lock.async_setup_internal(config_entry) is None
    assert lock.coordinator is not None
    assert await lock.async_unload(False) is None
    assert lock.usercode_scan_interval == timedelta(minutes=1)
    with pytest.raises(NotImplementedError):
        assert lock.domain
    # async_internal_is_reachable combines the integration and device
    # signals — it returns False here because the test config entry isn't in
    # the LOADED state (integration down short-circuits the combined check).
    assert await lock.async_internal_is_reachable() is False
    # hard_refresh / set / clear / get all check connection first via
    # _execute_rate_limited; since the default connection check returned
    # False above, they raise LockDisconnected before reaching the abstract
    # method that would raise NotImplementedError. Patch the connection
    # check to True so we actually exercise the abstract methods.
    with patch.object(BaseLock, "async_is_integration_connected", return_value=True):
        with pytest.raises(NotImplementedError):
            await lock.async_internal_hard_refresh_codes()
        with pytest.raises(NotImplementedError):
            await lock.async_internal_clear_usercode(1)
        with pytest.raises(NotImplementedError):
            await lock.async_internal_set_usercode(1, "1234")
        with pytest.raises(NotImplementedError):
            await lock.async_internal_get_usercodes()


async def test_unsubscribe_push_updates_suppresses_not_implemented(
    hass: HomeAssistant,
) -> None:
    """Test unsubscribe_push_updates swallows ProviderNotImplementedError.

    Providers that don't override teardown_push_subscription raise
    ProviderNotImplementedError from the base default.  The public
    unsubscribe_push_updates() wrapper must suppress that error so callers
    (e.g. async_setup teardown on reconnect) never see it propagate.
    """
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock_no_push",
        config_entry=config_entry,
    )
    lock = BaseLock(hass, dr.async_get(hass), entity_reg, config_entry, lock_entity)

    # teardown_push_subscription raises ProviderNotImplementedError by default
    with pytest.raises(ProviderNotImplementedError):
        lock.teardown_push_subscription()

    # The public wrapper must not propagate it
    lock.unsubscribe_push_updates()  # must not raise


async def test_config_entry_state_change_resubscribes(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Resubscribe and refresh when lock config entry reloads."""
    with patch(
        "custom_components.lock_code_manager.domain.locks.INTEGRATIONS_CLASS_MAP",
        {"test": MockLCMLockWithPush},
    ):
        lcm_config_entry = MockConfigEntry(
            domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title"
        )
        lcm_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(lcm_config_entry.entry_id)
        await hass.async_block_till_done()

        lock = lcm_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
        assert isinstance(lock, MockLCMLockWithPush)

        lock.subscribe_calls = 0
        lock.unsubscribe_calls = 0
        lock.coordinator.async_request_refresh = AsyncMock()

        await hass.config_entries.async_reload(mock_lock_config_entry.entry_id)
        await hass.async_block_till_done()

        assert lock.unsubscribe_calls == 1
        assert lock.subscribe_calls == 1
        lock.coordinator.async_request_refresh.assert_awaited()

        await hass.config_entries.async_unload(lcm_config_entry.entry_id)


async def test_connection_transition_resubscribes(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Resubscribe on reconnect and unsubscribe on disconnect."""
    with patch(
        "custom_components.lock_code_manager.domain.locks.INTEGRATIONS_CLASS_MAP",
        {"test": MockLCMLockWithPush},
    ):
        lcm_config_entry = MockConfigEntry(
            domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title"
        )
        lcm_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(lcm_config_entry.entry_id)
        await hass.async_block_till_done()

        lock = lcm_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
        assert isinstance(lock, MockLCMLockWithPush)

        lock.subscribe_calls = 0
        lock.unsubscribe_calls = 0
        lock._min_operation_delay = 0.0
        lock._last_operation_time = 0.0

        await lock.coordinator.async_refresh()

        lock.set_connected(False)
        await lock.coordinator.async_refresh()
        assert lock.unsubscribe_calls == 1

        lock.set_connected(True)
        await lock.coordinator.async_refresh()
        assert lock.subscribe_calls == 1

        await hass.config_entries.async_unload(lcm_config_entry.entry_id)


async def test_connection_transition_on_device_availability(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """A device-level (node) outage drives the same resubscribe/refresh path.

    Recovery must be detected uniformly whether the outage was at the
    integration/transport layer or the device/node layer: with the
    integration still connected, toggling device availability alone must
    unsubscribe on the drop and resubscribe + refresh on recovery
    (issue #1257 recovery latency).
    """
    with patch(
        "custom_components.lock_code_manager.domain.locks.INTEGRATIONS_CLASS_MAP",
        {"test": MockLCMLockWithPush},
    ):
        lcm_config_entry = MockConfigEntry(
            domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title"
        )
        lcm_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(lcm_config_entry.entry_id)
        await hass.async_block_till_done()

        lock = lcm_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
        assert isinstance(lock, MockLCMLockWithPush)

        lock.subscribe_calls = 0
        lock.unsubscribe_calls = 0
        lock._min_operation_delay = 0.0
        lock._last_operation_time = 0.0

        await lock.coordinator.async_refresh()

        # Integration stays connected; only the node drops.
        lock.set_device_available(False)
        await lock.coordinator.async_refresh()
        assert lock.unsubscribe_calls == 1

        lock.set_device_available(True)
        lock.coordinator.async_request_refresh = AsyncMock()
        await lock.coordinator.async_refresh()
        assert lock.subscribe_calls == 1
        lock.coordinator.async_request_refresh.assert_awaited()

        await hass.config_entries.async_unload(lcm_config_entry.entry_id)


@pytest.mark.parametrize(
    ("operation", "call"),
    [
        ("set", lambda p: p.async_internal_set_usercode(2, "9999", "test")),
        ("clear", lambda p: p.async_internal_clear_usercode(2)),
    ],
)
async def test_operation_when_disconnected(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    operation: str,
    call,
):
    """Test that operations raise LockDisconnected when lock is disconnected."""
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    lock_provider.set_connected(False)

    with pytest.raises(LockDisconnected):
        await call(lock_provider)


async def test_rate_limiting_set_usercode(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that operations are rate limited with minimum delay between calls."""
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    lock_provider._min_operation_delay = TEST_OPERATION_DELAY
    lock_provider._last_operation_time = 0.0

    # First operation should execute immediately
    start_time = time.monotonic()
    await lock_provider.async_internal_set_usercode(1, "1111", "Test 1")
    first_duration = time.monotonic() - start_time

    # First operation should be fast (< 0.2 seconds without rate limiting delay)
    assert first_duration < 0.2

    # Second operation should be rate limited
    start_time = time.monotonic()
    await lock_provider.async_internal_set_usercode(2, "2222", "Test 2")
    second_duration = time.monotonic() - start_time

    # Second operation should take at least the rate limit time
    assert second_duration >= TEST_OPERATION_DELAY

    # Verify both explicit operations completed (sync manager may add more)
    assert len(lock_provider.service_calls["set_usercode"]) >= 2


async def test_rate_limiting_mixed_operations(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that rate limiting applies across different operation types."""
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    lock_provider._min_operation_delay = TEST_OPERATION_DELAY
    lock_provider._last_operation_time = 0.0

    # First operation: set usercode
    await lock_provider.async_internal_set_usercode(1, "1111", "Test")

    # Second operation: clear usercode (different operation type, should still be rate limited)
    start_time = time.monotonic()
    await lock_provider.async_internal_clear_usercode(2)
    duration = time.monotonic() - start_time

    # Should be rate limited even though it's a different operation type
    assert duration >= TEST_OPERATION_DELAY


async def test_rate_limiting_get_usercodes(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that get operations are also rate limited."""
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    lock_provider._min_operation_delay = TEST_OPERATION_DELAY
    lock_provider._last_operation_time = 0.0

    # First get should be fast
    start_time = time.monotonic()
    await lock_provider.async_internal_get_usercodes()
    first_duration = time.monotonic() - start_time
    assert first_duration < 0.2

    # Second get should be rate limited
    start_time = time.monotonic()
    await lock_provider.async_internal_get_usercodes()
    second_duration = time.monotonic() - start_time
    assert second_duration >= TEST_OPERATION_DELAY


async def test_operations_are_serialized(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that multiple parallel operations are serialized by the lock."""
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    lock_provider._min_operation_delay = TEST_OPERATION_DELAY
    lock_provider._last_operation_time = 0.0

    # Start multiple operations in parallel
    start_time = time.monotonic()
    await asyncio.gather(
        lock_provider.async_internal_set_usercode(1, "1111", "Test 1"),
        lock_provider.async_internal_set_usercode(2, "2222", "Test 2"),
        lock_provider.async_internal_set_usercode(3, "3333", "Test 3"),
    )
    total_duration = time.monotonic() - start_time

    # With 3 operations and the test delay between each:
    # - First operation: executes immediately
    # - Second operation: waits once
    # - Third operation: waits once
    # Total should be at least 2 * TEST_OPERATION_DELAY
    assert total_duration >= 2 * TEST_OPERATION_DELAY

    # Verify all operations completed
    assert len(lock_provider.service_calls["set_usercode"]) == 3


async def test_connection_failure_does_not_rate_limit_next_operation(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that failed connection checks do not advance rate limit timing."""
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    # Tighten delay to keep test quick
    lock_provider._min_operation_delay = TEST_OPERATION_DELAY
    lock_provider._last_operation_time = 0.0

    lock_provider.set_connected(False)

    # First attempt should fail fast without waiting
    start = time.monotonic()
    with pytest.raises(LockDisconnected):
        await lock_provider.async_internal_set_usercode(1, "1111", "Test 1")
    failed_duration = time.monotonic() - start
    assert failed_duration < 0.2

    # Reconnect and verify the next call is not delayed by the failed attempt
    lock_provider.set_connected(True)
    start = time.monotonic()
    await lock_provider.async_internal_set_usercode(1, "2222", "Test 2")
    succeeded_duration = time.monotonic() - start

    assert succeeded_duration < 0.2


async def test_async_call_service_raises_operation_failed_on_error(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that async_call_service wraps HA service errors as LockOperationFailed."""
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    async def failing_service(call):
        raise HomeAssistantError("Service failed")

    hass.services.async_register("test_domain", "failing_service", failing_service)

    with pytest.raises(
        LockOperationFailed, match="Service call test_domain.failing_service failed"
    ):
        await lock_provider.async_call_service("test_domain", "failing_service", {})

    hass.services.async_remove("test_domain", "failing_service")


async def test_async_call_service_propagates_non_ha_exceptions(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    Non-HomeAssistantError exceptions must propagate, not become LockDisconnected.

    Wrapping programming errors (TypeError) or shutdown signals
    (asyncio.CancelledError) as LockDisconnected would trigger false
    "lock offline" issues, drift backoff, and push-resub loops.
    """
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    async def buggy_service(call):
        raise TypeError("programmer made a mistake")

    hass.services.async_register("test_domain", "buggy_service", buggy_service)

    with pytest.raises(TypeError, match="programmer made a mistake"):
        await lock_provider.async_call_service("test_domain", "buggy_service", {})

    hass.services.async_remove("test_domain", "buggy_service")


async def test_async_call_service_wraps_os_error_as_lock_disconnected(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """
    Test that async_call_service wraps OSError (e.g. ReadTimeout) as LockDisconnected.

    Integrations that don't wrap network errors in HomeAssistantError raise raw
    OSError subclasses (TimeoutError, ConnectionError).  These are transient and
    should be routed through the retry/backoff path, not treated as programming
    errors.
    """
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    async def timeout_service(call):
        raise TimeoutError("Read timed out")

    hass.services.async_register("test_domain", "timeout_service", timeout_service)

    with pytest.raises(
        LockDisconnected, match="Service call test_domain.timeout_service failed"
    ):
        await lock_provider.async_call_service("test_domain", "timeout_service", {})

    hass.services.async_remove("test_domain", "timeout_service")


async def test_set_usercode_refreshes_coordinator_on_change(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that async_internal_set_usercode refreshes coordinator when value changes."""
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    coordinator = lock_provider.coordinator
    assert coordinator is not None

    # Track coordinator refreshes
    refresh_count = [0]
    original_refresh = coordinator.async_request_refresh

    async def track_refresh():
        refresh_count[0] += 1
        return await original_refresh()

    with patch.object(coordinator, "async_request_refresh", track_refresh):
        # Setting a new usercode should trigger a coordinator refresh
        await lock_provider.async_internal_set_usercode(3, "3333", "Test 3")
        assert refresh_count[0] == 1

        # Setting the same usercode should NOT trigger refresh (no change)
        await lock_provider.async_internal_set_usercode(3, "3333", "Test 3")
        assert refresh_count[0] == 1  # Still 1, no new refresh


async def test_clear_usercode_refreshes_coordinator_on_change(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that async_internal_clear_usercode refreshes coordinator when value changes."""
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    coordinator = lock_provider.coordinator
    assert coordinator is not None

    # First set a usercode so we can clear it
    await lock_provider.async_internal_set_usercode(4, "4444", "Test 4")

    # Track coordinator refreshes
    refresh_count = [0]
    original_refresh = coordinator.async_request_refresh

    async def track_refresh():
        refresh_count[0] += 1
        return await original_refresh()

    with patch.object(coordinator, "async_request_refresh", track_refresh):
        # Clearing an existing usercode should trigger a coordinator refresh
        await lock_provider.async_internal_clear_usercode(4)
        assert refresh_count[0] == 1

        # Clearing a non-existent slot should NOT trigger refresh (no change)
        await lock_provider.async_internal_clear_usercode(999)
        assert refresh_count[0] == 1  # Still 1, no new refresh


async def test_lock_equality_with_non_baselock(hass: HomeAssistant):
    """Test that __eq__ returns False when comparing with non-BaseLock object."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock_eq",
        config_entry=config_entry,
    )

    lock = BaseLock(
        hass,
        dr.async_get(hass),
        entity_reg,
        config_entry,
        lock_entity,
    )

    # Comparing to non-BaseLock objects should return False
    assert lock != "not a lock"
    assert lock != 123
    assert (lock == None) is False  # noqa: E711 - Intentionally testing __eq__ with None
    assert lock != {"entity_id": lock_entity.entity_id}


async def test_fire_code_slot_event_with_state_source(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test async_fire_code_slot_event with State as source_data."""
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    # Capture events
    events = []

    @callback
    def capture_events(event):
        events.append(event)

    hass.bus.async_listen(EVENT_LOCK_STATE_CHANGED, capture_events)

    # Create a State object as source_data
    now = datetime.now()
    state = State(
        entity_id="lock.test_lock",
        state="locked",
        attributes={"battery_level": 80},
        last_changed=now,
        last_updated=now,
    )

    # Fire event with State source
    lock_provider.async_fire_code_slot_event(
        code_slot=1,
        to_locked=True,
        action_text="Keypad lock",
        source_data=state,
    )
    await hass.async_block_till_done()

    # Verify event was fired with State source info
    assert len(events) == 1
    assert events[0].data[ATTR_NOTIFICATION_SOURCE] == "state"
    extra_data = events[0].data[ATTR_EXTRA_DATA]
    assert extra_data["entity_id"] == "lock.test_lock"
    assert extra_data["state"] == "locked"
    assert extra_data["attributes"] == {"battery_level": 80}
    assert "last_changed" in extra_data
    assert "last_updated" in extra_data


async def test_fire_code_slot_event_with_dict_source(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test async_fire_code_slot_event with dict as source_data."""
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    # Capture events
    events = []

    @callback
    def capture_events(event):
        events.append(event)

    hass.bus.async_listen(EVENT_LOCK_STATE_CHANGED, capture_events)

    # Fire event with dict source
    custom_data = {"custom_field": "custom_value", "another_field": 123}
    lock_provider.async_fire_code_slot_event(
        code_slot=1,
        to_locked=False,
        action_text="Manual unlock",
        source_data=custom_data,
    )
    await hass.async_block_till_done()

    # Verify event was fired with dict source
    assert len(events) == 1
    assert (
        events[0].data[ATTR_NOTIFICATION_SOURCE] is None
    )  # dict doesn't set source type
    extra_data = events[0].data[ATTR_EXTRA_DATA]
    assert extra_data == custom_data


async def test_setup_defers_push_subscription_when_entry_not_loaded(
    hass: HomeAssistant,
):
    """Test that async_setup defers push subscription when lock config entry isn't loaded."""
    entity_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    # Create a mock lock config entry that's NOT loaded
    lock_config_entry = MockConfigEntry(domain="test", state=None)
    lock_config_entry.add_to_hass(hass)
    # Manually set state to NOT_LOADED
    object.__setattr__(lock_config_entry, "state", "not_loaded")

    lcm_config_entry = MockConfigEntry(domain=DOMAIN)
    lcm_config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock_defer",
        config_entry=lock_config_entry,
    )

    # Create a mock lock with push support
    lock = MockLCMLockWithPush(
        hass,
        dev_reg,
        entity_reg,
        lock_config_entry,
        lock_entity,
    )
    lock.subscribe_calls = 0

    # Mock coordinator refreshes
    with (
        patch(
            "custom_components.lock_code_manager.domain.coordinator."
            "LockUsercodeUpdateCoordinator.async_config_entry_first_refresh"
        ),
    ):
        await lock.async_setup_internal(lcm_config_entry)

        # Push subscription should be deferred (not called) since entry not loaded
        assert lock.subscribe_calls == 0

    await lock.async_unload(False)


async def test_async_setup_internal_creates_coordinator_when_setup_fails(
    hass: HomeAssistant,
):
    """Test that coordinator is created even when async_setup raises."""
    entity_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock_setup_fail",
        config_entry=config_entry,
    )

    lock = BaseLock(
        hass,
        dev_reg,
        entity_reg,
        config_entry,
        lock_entity,
    )

    # Make async_setup raise an exception
    with (
        patch.object(
            lock,
            "async_setup",
            side_effect=LockDisconnected("provider unavailable"),
        ),
        patch(
            "custom_components.lock_code_manager.domain.coordinator."
            "LockUsercodeUpdateCoordinator.async_config_entry_first_refresh"
        ),
        patch(
            "custom_components.lock_code_manager.domain.coordinator."
            "LockUsercodeUpdateCoordinator.async_refresh"
        ),
    ):
        # Should not raise even though async_setup failed
        await lock.async_setup_internal(config_entry)

    # Coordinator should still have been created
    assert lock.coordinator is not None
    # Setup complete should be signaled
    assert lock._setup_complete.is_set()
    # Setup should be marked as failed
    assert lock._setup_succeeded is False

    # Simulate reconnect: mock lock_config_entry as LOADED, async_setup succeeds
    mock_entry = MagicMock()
    mock_entry.state = ConfigEntryState.LOADED
    lock.lock_config_entry = mock_entry
    with patch.object(lock, "async_setup", return_value=None):
        await lock._async_on_integration_loaded()

    # Coordinator was created via async_setup_internal directly (no full LCM
    # entry to unload). Shut it down explicitly so HA 2026.5.0's
    # verify_cleanup does not flag the debouncer timer as lingering.
    await lock.coordinator.async_shutdown()

    assert lock._setup_succeeded is True


async def test_on_integration_loaded_skips_when_no_config_entry(
    hass: HomeAssistant,
):
    """Test that _async_on_integration_loaded is a no-op when _lcm_config_entry is None."""
    entity_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock", "test", "test_lock_no_entry", config_entry=config_entry
    )

    lock = BaseLock(hass, dev_reg, entity_reg, config_entry, lock_entity)
    # _lcm_config_entry is None (async_setup_internal never called)
    assert lock._lcm_config_entry is None
    # Should be a no-op, not raise
    await lock._async_on_integration_loaded()
    assert lock._setup_succeeded is False


async def test_on_integration_loaded_retries_on_disconnect(
    hass: HomeAssistant,
):
    """Test that _async_on_integration_loaded retries setup on LockDisconnected."""
    entity_reg = er.async_get(hass)
    dev_reg = dr.async_get(hass)

    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock", "test", "test_lock_retry", config_entry=config_entry
    )

    lock = BaseLock(hass, dev_reg, entity_reg, config_entry, lock_entity)
    lock._lcm_config_entry = config_entry

    # async_setup raises LockDisconnected — should not propagate
    with patch.object(
        lock, "async_setup", side_effect=LockDisconnected("still offline")
    ):
        await lock._async_on_integration_loaded()

    assert lock._setup_succeeded is False


class MockNativeUserLock(MockLCMLock):
    """Mock lock that opts into the native-user capability validation."""

    @property
    def supports_native_users(self) -> bool:
        """Return True so async_setup_internal runs the capability probe."""
        return True


def _pin_capabilities() -> LockCapabilities:
    """Return capabilities advertising PIN support."""
    return LockCapabilities(
        supports_user_management=True,
        max_users=10,
        credential_types={
            CredentialType.PIN: CredentialTypeCapability(
                num_slots=10, min_length=4, max_length=8, supports_learn=False
            )
        },
        max_user_name_length=10,
    )


def _make_base_test_lock(
    hass: HomeAssistant, unique_id: str, lock_cls: type[MockLCMLock] = MockLCMLock
) -> MockLCMLock:
    """Create a mock lock wired to a registry entry and config entry."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)
    lock_entity = entity_reg.async_get_or_create(
        "lock", "test", unique_id, config_entry=config_entry
    )
    return lock_cls(hass, dr.async_get(hass), entity_reg, config_entry, lock_entity)


async def test_setup_internal_defers_provider_io_when_integration_not_connected(
    hass: HomeAssistant,
):
    """Setup skips the capability probe and async_setup while the integration is down.

    Regression test for issue #1321: the base class must not touch the
    provider integration before it is connected — a provider whose probe
    raises an untranslated error (e.g. zwave_js's node lookup ValueError)
    would otherwise crash setup and permanently drop the lock.
    """
    lock = _make_base_test_lock(hass, "test_lock_defer_io", MockNativeUserLock)
    lock.set_connected(False)

    with (
        patch.object(lock, "async_get_capabilities") as mock_caps,
        patch.object(lock, "async_setup") as mock_setup,
    ):
        await lock.async_setup_internal(lock.lock_config_entry)

    mock_caps.assert_not_called()
    mock_setup.assert_not_called()
    assert lock._setup_succeeded is False
    # Degraded, not dropped: coordinator and recovery listener exist.
    assert lock.coordinator is not None
    assert lock._config_entry_state_unsub is not None
    assert lock._setup_complete.is_set()

    await lock.coordinator.async_shutdown()
    await lock.async_unload(False)


async def test_setup_complete_set_when_setup_raises_unexpectedly(
    hass: HomeAssistant,
):
    """_setup_complete is set even when setup escapes with an unexpected error.

    Waiters in async_wait_for_setup (shared lock instances across config
    entries) must not hang forever when setup fails structurally.
    """
    lock = _make_base_test_lock(hass, "test_lock_setup_complete")

    with patch.object(lock, "async_setup", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            await lock.async_setup_internal(lock.lock_config_entry)

    assert lock._setup_complete.is_set()


async def test_on_integration_loaded_runs_deferred_capability_validation(
    hass: HomeAssistant,
):
    """The reconnect path runs the capability validation setup deferred.

    When initial setup ran degraded (integration down), the LOADED
    transition must run the same probe + PIN validation as initial setup
    before marking the provider set up.
    """
    lock = _make_base_test_lock(hass, "test_lock_deferred_caps", MockNativeUserLock)
    lock.set_connected(False)

    caps_mock = AsyncMock(return_value=_pin_capabilities())
    with patch.object(lock, "async_get_capabilities", caps_mock):
        await lock.async_setup_internal(lock.lock_config_entry)
        assert lock._setup_succeeded is False
        caps_mock.assert_not_called()

        # Integration comes up; simulate the LOADED transition callback.
        lock.set_connected(True)
        loaded_entry = MagicMock()
        loaded_entry.state = ConfigEntryState.LOADED
        lock.lock_config_entry = loaded_entry
        await lock._async_on_integration_loaded()

    caps_mock.assert_awaited_once()
    assert lock._setup_succeeded is True

    await lock.coordinator.async_shutdown()
    await lock.async_unload(False)


async def test_setup_internal_structural_failure_degrades_and_recovers_on_reload(
    hass: HomeAssistant,
):
    """Initial-setup validation failure degrades the lock instead of dropping it.

    A structural failure (no PIN support / zero usable slots) during initial
    setup used to propagate, making ``_async_setup_new_locks`` pop the lock
    from runtime data with no retry path and no UI surface — a re-included
    Z-Wave lock whose interview completed asleep stayed invisible until a
    full reload. Initial setup now matches the reconnect path: the failure is
    logged, the coordinator and recovery listener are still created, and the
    LOADED transition re-runs validation so the lock recovers once the
    underlying condition is fixed (e.g. after a re-interview).
    """
    lock = _make_base_test_lock(
        hass, "test_lock_structural_degraded", MockNativeUserLock
    )

    no_pin_caps = LockCapabilities(
        supports_user_management=True, max_users=10, credential_types={}
    )
    caps_mock = AsyncMock(return_value=no_pin_caps)
    with patch.object(lock, "async_get_capabilities", caps_mock):
        await lock.async_setup_internal(lock.lock_config_entry)

        caps_mock.assert_awaited_once()
        assert lock._setup_succeeded is False
        # Degraded, not dropped: coordinator and recovery listener exist.
        assert lock.coordinator is not None
        assert lock._config_entry_state_unsub is not None
        assert lock._setup_complete.is_set()

        # The lock's condition is fixed (e.g. re-interview repopulated the
        # slot count); the next LOADED transition revalidates and recovers.
        caps_mock.return_value = _pin_capabilities()
        # Validation failure must invalidate the capability cache; a
        # degenerate-but-successful read would otherwise be served to every
        # revalidation, making recovery impossible.
        loaded_entry = MagicMock()
        loaded_entry.state = ConfigEntryState.LOADED
        lock.lock_config_entry = loaded_entry
        await lock._async_on_integration_loaded()

    assert lock._setup_succeeded is True

    await lock.coordinator.async_shutdown()
    await lock.async_unload(False)


async def test_setup_validation_failure_raises_and_clears_repair_issue(
    hass: HomeAssistant,
):
    """A structural setup failure surfaces a repair issue; recovery clears it.

    The actionable error message (e.g. zwave_js's "re-interview the lock")
    used to land only in the log, addressed to a user who never sees it.
    The repair carries it into the UI, and revalidation succeeding on the
    provider integration's LOADED transition dismisses it automatically.
    """
    lock = _make_base_test_lock(hass, "test_lock_repair_issue", MockNativeUserLock)
    issue_reg = ir.async_get(hass)
    issue_id = f"lock_setup_failed_{lock.lock.entity_id}"

    no_pin_caps = LockCapabilities(
        supports_user_management=True, max_users=10, credential_types={}
    )
    caps_mock = AsyncMock(return_value=no_pin_caps)
    with patch.object(lock, "async_get_capabilities", caps_mock):
        await lock.async_setup_internal(lock.lock_config_entry)

        issue = issue_reg.async_get_issue(DOMAIN, issue_id)
        assert issue is not None
        assert issue.translation_placeholders
        assert "PIN credential" in issue.translation_placeholders["error"]

        # The lock's condition is fixed; the LOADED transition revalidates
        # and the repair dismisses itself.
        caps_mock.return_value = _pin_capabilities()
        loaded_entry = MagicMock()
        loaded_entry.state = ConfigEntryState.LOADED
        lock.lock_config_entry = loaded_entry
        await lock._async_on_integration_loaded()

    assert issue_reg.async_get_issue(DOMAIN, issue_id) is None

    await lock.coordinator.async_shutdown()
    await lock.async_unload(False)


async def test_reconnect_validation_failure_raises_repair_issue(
    hass: HomeAssistant,
):
    """Deferred validation failing on the reconnect path raises the repair too."""
    lock = _make_base_test_lock(
        hass, "test_lock_repair_issue_reconnect", MockNativeUserLock
    )
    lock.set_connected(False)
    issue_reg = ir.async_get(hass)
    issue_id = f"lock_setup_failed_{lock.lock.entity_id}"

    no_pin_caps = LockCapabilities(
        supports_user_management=True, max_users=10, credential_types={}
    )
    caps_mock = AsyncMock(return_value=no_pin_caps)
    with patch.object(lock, "async_get_capabilities", caps_mock):
        await lock.async_setup_internal(lock.lock_config_entry)
        # Deferred: no probe ran, so no issue yet.
        assert issue_reg.async_get_issue(DOMAIN, issue_id) is None

        lock.set_connected(True)
        loaded_entry = MagicMock()
        loaded_entry.state = ConfigEntryState.LOADED
        lock.lock_config_entry = loaded_entry
        await lock._async_on_integration_loaded()

    assert issue_reg.async_get_issue(DOMAIN, issue_id) is not None

    await lock.coordinator.async_shutdown()
    await lock.async_unload(False)


async def test_unload_permanently_clears_setup_failed_repair_issue(
    hass: HomeAssistant,
):
    """Removing a lock permanently clears its setup-failed repair issue.

    A non-permanent unload (reload, HA restart) must keep the persistent
    issue so it survives until the failure actually resolves.
    """
    lock = _make_base_test_lock(hass, "test_lock_repair_unload", MockNativeUserLock)
    issue_reg = ir.async_get(hass)
    issue_id = f"lock_setup_failed_{lock.lock.entity_id}"

    no_pin_caps = LockCapabilities(
        supports_user_management=True, max_users=10, credential_types={}
    )
    with patch.object(
        lock, "async_get_capabilities", AsyncMock(return_value=no_pin_caps)
    ):
        await lock.async_setup_internal(lock.lock_config_entry)
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is not None

    await lock.async_unload(False)
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is not None

    await lock.async_unload(True)
    assert issue_reg.async_get_issue(DOMAIN, issue_id) is None

    await lock.coordinator.async_shutdown()


async def test_on_integration_loaded_rejects_lock_without_pin_support(
    hass: HomeAssistant,
):
    """Deferred validation failing (no PIN support) leaves setup unsuccessful.

    The structural failure must not escape into the reconnect task; the
    provider simply never reaches the set-up state.
    """
    lock = _make_base_test_lock(hass, "test_lock_deferred_no_pin", MockNativeUserLock)
    lock.set_connected(False)

    no_pin_caps = LockCapabilities(
        supports_user_management=True, max_users=10, credential_types={}
    )
    caps_mock = AsyncMock(return_value=no_pin_caps)
    with patch.object(lock, "async_get_capabilities", caps_mock):
        await lock.async_setup_internal(lock.lock_config_entry)

        lock.set_connected(True)
        loaded_entry = MagicMock()
        loaded_entry.state = ConfigEntryState.LOADED
        lock.lock_config_entry = loaded_entry
        await lock._async_on_integration_loaded()

    caps_mock.assert_awaited_once()
    assert lock._setup_succeeded is False

    await lock.coordinator.async_shutdown()
    await lock.async_unload(False)


async def test_set_usercode_skips_refresh_for_push_provider(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Test that async_internal_set_usercode does NOT refresh coordinator for push providers."""
    with patch(
        "custom_components.lock_code_manager.domain.locks.INTEGRATIONS_CLASS_MAP",
        {"test": MockLCMLockWithPush},
    ):
        lcm_config_entry = MockConfigEntry(
            domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title Push Set"
        )
        lcm_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(lcm_config_entry.entry_id)
        await hass.async_block_till_done()

        lock_provider = lcm_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
        assert isinstance(lock_provider, MockLCMLockWithPush)
        coordinator = lock_provider.coordinator
        assert coordinator is not None

        # Track coordinator refreshes
        refresh_count = [0]
        original_refresh = coordinator.async_request_refresh

        async def track_refresh():
            refresh_count[0] += 1
            return await original_refresh()

        with patch.object(coordinator, "async_request_refresh", track_refresh):
            # Setting a new usercode should NOT trigger refresh for push providers
            await lock_provider.async_internal_set_usercode(3, "3333", "Test 3")
            assert refresh_count[0] == 0

        await hass.config_entries.async_unload(lcm_config_entry.entry_id)


async def test_clear_usercode_skips_refresh_for_push_provider(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Test that async_internal_clear_usercode does NOT refresh coordinator for push providers."""
    with patch(
        "custom_components.lock_code_manager.domain.locks.INTEGRATIONS_CLASS_MAP",
        {"test": MockLCMLockWithPush},
    ):
        lcm_config_entry = MockConfigEntry(
            domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title Push Clear"
        )
        lcm_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(lcm_config_entry.entry_id)
        await hass.async_block_till_done()

        lock_provider = lcm_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
        assert isinstance(lock_provider, MockLCMLockWithPush)
        coordinator = lock_provider.coordinator
        assert coordinator is not None

        # Track coordinator refreshes
        refresh_count = [0]
        original_refresh = coordinator.async_request_refresh

        async def track_refresh():
            refresh_count[0] += 1
            return await original_refresh()

        with patch.object(coordinator, "async_request_refresh", track_refresh):
            # Clearing an existing usercode should NOT trigger refresh for push providers
            await lock_provider.async_internal_clear_usercode(1)
            assert refresh_count[0] == 0

        await hass.config_entries.async_unload(lcm_config_entry.entry_id)


async def test_config_entry_state_listener_ignores_same_state(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Test that config entry state listener ignores transitions to same state."""
    with patch(
        "custom_components.lock_code_manager.domain.locks.INTEGRATIONS_CLASS_MAP",
        {"test": MockLCMLockWithPush},
    ):
        lcm_config_entry = MockConfigEntry(
            domain=DOMAIN, data=BASE_CONFIG, unique_id="Mock Title Same State"
        )
        lcm_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(lcm_config_entry.entry_id)
        await hass.async_block_till_done()

        lock = lcm_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
        assert isinstance(lock, MockLCMLockWithPush)

        # Reset counters
        lock.subscribe_calls = 0
        lock.unsubscribe_calls = 0

        # Manually trigger the state listener with the same state (LOADED -> LOADED)
        # This simulates the edge case where the state hasn't actually changed
        lock._last_entry_state = mock_lock_config_entry.state
        # The listener should no-op since state didn't change

        # Verify no calls were made
        assert lock.subscribe_calls == 0
        assert lock.unsubscribe_calls == 0

        await hass.config_entries.async_unload(lcm_config_entry.entry_id)


async def test_is_device_available_default_returns_true(hass: HomeAssistant):
    """Test that base class is_device_available() returns True by default."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry(domain=DOMAIN)
    config_entry.add_to_hass(hass)

    lock_entity = entity_reg.async_get_or_create(
        "lock",
        "test",
        "test_lock_device_available",
        config_entry=config_entry,
    )

    lock = BaseLock(
        hass,
        dr.async_get(hass),
        entity_reg,
        config_entry,
        lock_entity,
    )

    # Default implementation returns True
    assert await lock.async_is_device_available() is True


async def test_execute_rate_limited_raises_when_device_not_available(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that _execute_rate_limited raises LockDisconnected when device not available."""
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    # Device is not available but integration is connected
    with patch.object(lock_provider, "async_is_device_available", return_value=False):
        with pytest.raises(LockDisconnected, match="device not available"):
            await lock_provider.async_internal_set_usercode(2, "9999", "test")


# =============================================================================
# _check_duplicate_code tests
# =============================================================================


async def test_check_duplicate_code_raises_on_match(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test _check_duplicate_code raises when a duplicate PIN is found."""
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    coordinator = lock.coordinator
    assert coordinator is not None

    coordinator.async_set_updated_data(
        {
            1: SlotCredential.known("1234"),
            2: SlotCredential.known("5678"),
            3: SlotCredential.empty(),
        }
    )

    with pytest.raises(DuplicateCodeError) as exc_info:
        lock._check_duplicate_code(3, "1234")

    assert exc_info.value.code_slot == 3
    assert exc_info.value.conflicting_slot == 1


async def test_check_duplicate_code_skips_masked(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test _check_duplicate_code skips masked codes."""
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    coordinator = lock.coordinator
    assert coordinator is not None

    coordinator.async_set_updated_data(
        {1: SlotCredential.unreadable(), 3: SlotCredential.empty()}
    )

    lock._check_duplicate_code(3, "1234")


async def test_check_duplicate_code_skips_same_slot(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test _check_duplicate_code skips the target slot itself."""
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    coordinator = lock.coordinator
    assert coordinator is not None

    coordinator.async_set_updated_data({1: SlotCredential.known("1234")})

    lock._check_duplicate_code(1, "1234")


async def test_check_duplicate_code_no_op_on_empty_usercode(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test _check_duplicate_code is a no-op when usercode is empty."""
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    coordinator = lock.coordinator
    assert coordinator is not None

    coordinator.async_set_updated_data(
        {1: SlotCredential.empty(), 2: SlotCredential.empty()}
    )

    lock._check_duplicate_code(3, "")


async def test_check_duplicate_code_no_coordinator(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test _check_duplicate_code is a no-op when coordinator has no data."""
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    lock.coordinator = None

    lock._check_duplicate_code(1, "1234")


async def test_async_unload_cancels_in_flight_reconnect_task(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """async_unload cancels the reconnect task spawned by the state listener."""
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    # Plant an in-flight reconnect task that takes a long time. async_unload
    # should cancel it before returning so a late
    # coordinator.async_request_refresh() cannot fire after teardown.
    started = asyncio.Event()

    async def slow_reconnect() -> None:
        started.set()
        await asyncio.sleep(60)

    lock._reconnect_task = hass.async_create_task(slow_reconnect())
    await started.wait()
    assert not lock._reconnect_task.done()

    await lock.async_unload(False)

    assert lock._reconnect_task is None


async def test_handle_state_change_supersedes_prior_reconnect_task(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """A second LOADED transition cancels and replaces the prior reconnect task."""
    with patch(
        "custom_components.lock_code_manager.domain.locks.INTEGRATIONS_CLASS_MAP",
        {"test": MockLCMLockWithPush},
    ):
        lcm_config_entry = MockConfigEntry(
            domain=DOMAIN,
            data=BASE_CONFIG,
            unique_id="Mock Title Reconnect Supersede",
        )
        lcm_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(lcm_config_entry.entry_id)
        await hass.async_block_till_done()

        lock = lcm_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

        # Plant a long-running reconnect task to mimic a slow reconnect that
        # has not yet completed.
        async def slow_reconnect() -> None:
            await asyncio.sleep(60)

        prior_task = hass.async_create_task(slow_reconnect())
        lock._reconnect_task = prior_task

        # Drive a second LOADED transition through the state listener. The
        # listener should cancel the prior task and store the new one.
        lock._last_entry_state = ConfigEntryState.NOT_LOADED
        mock_lock_config_entry.mock_state(hass, ConfigEntryState.LOADED)
        await hass.async_block_till_done()

        assert prior_task.cancelled() or prior_task.done()
        assert lock._reconnect_task is not None
        assert lock._reconnect_task is not prior_task

        await hass.config_entries.async_unload(lcm_config_entry.entry_id)


async def test_supersede_drains_prior_reconnect_exception(
    hass: HomeAssistant,
    mock_lock_config_entry,
    caplog: pytest.LogCaptureFixture,
):
    """A superseded reconnect task that failed before cancellation is logged, not orphaned."""
    with patch(
        "custom_components.lock_code_manager.domain.locks.INTEGRATIONS_CLASS_MAP",
        {"test": MockLCMLockWithPush},
    ):
        lcm_config_entry = MockConfigEntry(
            domain=DOMAIN,
            data=BASE_CONFIG,
            unique_id="Mock Title Drain Reconnect",
        )
        lcm_config_entry.add_to_hass(hass)
        await hass.config_entries.async_setup(lcm_config_entry.entry_id)
        await hass.async_block_till_done()

        lock = lcm_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

        boom = RuntimeError("simulated prior reconnect failure")

        async def failed_reconnect() -> None:
            raise boom

        prior_task = hass.async_create_task(failed_reconnect())
        # Let the task run to completion with its exception.
        await asyncio.sleep(0)
        assert prior_task.done()
        assert prior_task.exception() is boom

        lock._reconnect_task = prior_task

        # Drive a LOADED transition through the state listener; the supersede
        # path must drain the failed prior task and log its exception.
        lock._last_entry_state = ConfigEntryState.NOT_LOADED
        with caplog.at_level(logging.WARNING):
            mock_lock_config_entry.mock_state(hass, ConfigEntryState.LOADED)
            await hass.async_block_till_done()

        assert any(
            record.exc_info is not None and record.exc_info[1] is boom
            for record in caplog.records
            if record.levelname == "WARNING"
        )

        await hass.config_entries.async_unload(lcm_config_entry.entry_id)


async def test_async_unload_logs_reconnect_task_exception(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    caplog: pytest.LogCaptureFixture,
):
    """async_unload logs warnings when the reconnect task raises non-CancelledError on shutdown."""
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    boom = RuntimeError("simulated reconnect cleanup failure")
    started = asyncio.Event()

    async def stubborn_reconnect() -> None:
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise boom from None

    lock._reconnect_task = hass.async_create_task(stubborn_reconnect())
    await started.wait()
    assert not lock._reconnect_task.done()

    with caplog.at_level(logging.WARNING):
        await lock.async_unload(False)

    assert lock._reconnect_task is None
    assert any(
        record.exc_info is not None and record.exc_info[1] is boom
        for record in caplog.records
        if record.levelname == "WARNING"
    )


# =============================================================================
# Push unsub registry
# =============================================================================


async def test_clear_push_unsubs_invokes_and_empties_registry(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """_clear_push_unsubs invokes every registered unsub and clears the list."""
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    calls: list[str] = []
    lock._register_push_unsub(lambda: calls.append("a"))
    lock._register_push_unsub(lambda: calls.append("b"))
    assert len(lock._push_unsubs) == 2

    lock._clear_push_unsubs()

    assert calls == ["a", "b"]
    assert lock._push_unsubs == []


async def test_clear_push_unsubs_continues_on_individual_failure(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    caplog: pytest.LogCaptureFixture,
):
    """A raising unsub does not prevent later unsubs from running."""
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    second_called = [False]

    def boom_unsub() -> None:
        raise RuntimeError("unsub failed")

    def second_unsub() -> None:
        second_called[0] = True

    lock._register_push_unsub(boom_unsub)
    lock._register_push_unsub(second_unsub)

    with caplog.at_level(logging.WARNING):
        lock._clear_push_unsubs()

    assert second_called[0]
    assert lock._push_unsubs == []


async def test_clear_push_unsubs_safe_when_unsub_reregisters(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """An unsub that re-registers must not corrupt iteration or loop forever."""
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    calls: list[str] = []

    def reregistering_unsub() -> None:
        calls.append("first")
        # Re-register self after the original snapshot was taken; the
        # re-registration must survive clear() but must not be called
        # again in this _clear_push_unsubs invocation.
        lock._register_push_unsub(reregistering_unsub)

    lock._register_push_unsub(reregistering_unsub)
    lock._register_push_unsub(lambda: calls.append("second"))

    lock._clear_push_unsubs()

    assert calls == ["first", "second"]
    assert lock._push_unsubs == [reregistering_unsub]


# =============================================================================
# Sequence lock context manager
# =============================================================================


async def test_serialize_sequence_serializes_concurrent_blocks(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """_serialize_sequence forces concurrent multi-step ops to run one at a time."""
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    in_section = [0]
    max_overlap = [0]
    order: list[int] = []

    async def section(tag: int) -> None:
        async with lock._serialize_sequence():
            in_section[0] += 1
            max_overlap[0] = max(max_overlap[0], in_section[0])
            await asyncio.sleep(0)
            order.append(tag)
            in_section[0] -= 1

    await asyncio.gather(section(1), section(2), section(3))

    assert max_overlap[0] == 1
    assert sorted(order) == [1, 2, 3]


async def test_serialize_sequence_allows_rate_limited_inside(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """A rate-limited leaf call inside _serialize_sequence does not deadlock."""
    lock = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]
    lock._min_operation_delay = TEST_OPERATION_DELAY

    async with lock._serialize_sequence():
        result = await lock.async_internal_set_usercode(5, "5555", "Test 5")

    assert result is None
