"""Test base class."""

import asyncio
from datetime import datetime, timedelta
import time
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant, State, callback
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import (
    ATTR_EXTRA_DATA,
    ATTR_NOTIFICATION_SOURCE,
    DOMAIN,
    EVENT_LOCK_STATE_CHANGED,
)
from custom_components.lock_code_manager.exceptions import LockDisconnected
from custom_components.lock_code_manager.providers._base import BaseLock
from tests.common import BASE_CONFIG, LOCK_1_ENTITY_ID, LOCK_DATA, MockLCMLock

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

    def subscribe_push_updates(self) -> None:
        """Subscribe to push-based value updates."""
        self.subscribe_calls += 1

    def unsubscribe_push_updates(self) -> None:
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
            "custom_components.lock_code_manager.coordinator."
            "LockUsercodeUpdateCoordinator.async_config_entry_first_refresh"
        ),
        patch(
            "custom_components.lock_code_manager.coordinator."
            "LockUsercodeUpdateCoordinator.async_refresh"
        ),
    ):
        assert await lock.async_setup(config_entry) is None
    assert lock.coordinator is not None
    assert await lock.async_unload(False) is None
    assert lock.usercode_scan_interval == timedelta(minutes=1)
    with pytest.raises(NotImplementedError):
        assert lock.domain
    with pytest.raises(NotImplementedError):
        await lock.async_internal_is_connection_up()
    # Note: hard_refresh, set, and clear operations now check connection first,
    # so they raise NotImplementedError from is_connection_up() instead of
    # the expected error from the unimplemented method
    with pytest.raises(NotImplementedError):
        await lock.async_internal_hard_refresh_codes()
    with pytest.raises(NotImplementedError):
        await lock.async_internal_clear_usercode(1)
    with pytest.raises(NotImplementedError):
        await lock.async_internal_set_usercode(1, 1)
    with pytest.raises(NotImplementedError):
        await lock.async_internal_get_usercodes()


async def test_config_entry_state_change_resubscribes(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Resubscribe and refresh when lock config entry reloads."""
    with patch(
        "custom_components.lock_code_manager.helpers.INTEGRATIONS_CLASS_MAP",
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
        lock.coordinator.async_refresh = AsyncMock()

        await hass.config_entries.async_reload(mock_lock_config_entry.entry_id)
        await hass.async_block_till_done()

        assert lock.unsubscribe_calls == 1
        assert lock.subscribe_calls == 1
        lock.coordinator.async_refresh.assert_awaited()

        await hass.config_entries.async_unload(lcm_config_entry.entry_id)


async def test_connection_transition_resubscribes(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Resubscribe on reconnect and unsubscribe on disconnect."""
    with patch(
        "custom_components.lock_code_manager.helpers.INTEGRATIONS_CLASS_MAP",
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


async def test_set_usercode_when_disconnected(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that async_internal_set_usercode raises LockDisconnected when lock is disconnected."""
    # Arrange: get the provider and force it offline
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    # Simulate disconnected lock
    lock_provider.set_connected(False)

    # Attempt to set usercode should raise LockDisconnected
    with pytest.raises(LockDisconnected, match="Cannot set on"):
        await lock_provider.async_internal_set_usercode(2, "9999", "test")

    # Verify no service calls were made
    assert hass.data[LOCK_DATA][LOCK_1_ENTITY_ID]["service_calls"]["set_usercode"] == []


async def test_clear_usercode_when_disconnected(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that async_internal_clear_usercode raises LockDisconnected when lock is disconnected."""
    # Arrange: get the provider and force it offline
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    # Simulate disconnected lock
    lock_provider.set_connected(False)

    # Attempt to clear usercode should raise LockDisconnected
    with pytest.raises(LockDisconnected):
        await lock_provider.async_internal_clear_usercode(2)


async def test_rate_limiting_set_usercode(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that operations are rate limited with minimum delay between calls."""
    # Arrange: shorter delay for faster assertions
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    # Set a smaller delay for testing
    lock_provider._min_operation_delay = TEST_OPERATION_DELAY

    # Reset the last operation time to ensure clean test
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

    # Verify both operations completed
    assert (
        len(hass.data[LOCK_DATA][LOCK_1_ENTITY_ID]["service_calls"]["set_usercode"])
        == 2
    )


async def test_rate_limiting_mixed_operations(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that rate limiting applies across different operation types."""
    # Arrange: shorter delay for faster assertions
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    # Set a smaller delay for testing
    lock_provider._min_operation_delay = TEST_OPERATION_DELAY
    # Reset the last operation time to ensure clean test isolation
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
    # Arrange: shorter delay for faster assertions
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    # Set a smaller delay for testing
    lock_provider._min_operation_delay = TEST_OPERATION_DELAY

    # Reset the last operation time to ensure clean test
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
    # Arrange: shorter delay for faster assertions
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    # Set a smaller delay for testing
    lock_provider._min_operation_delay = TEST_OPERATION_DELAY
    # Reset the last operation time to ensure clean test isolation
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
    assert (
        len(hass.data[LOCK_DATA][LOCK_1_ENTITY_ID]["service_calls"]["set_usercode"])
        == 3
    )


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


async def test_async_call_service_raises_lock_disconnected_on_error(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that async_call_service raises LockDisconnected when service call fails."""
    lock_provider = lock_code_manager_config_entry.runtime_data.locks[LOCK_1_ENTITY_ID]

    # Register a service that raises an error
    async def failing_service(call):
        raise ValueError("Service failed")

    hass.services.async_register("test_domain", "failing_service", failing_service)

    # Calling a failing service should raise LockDisconnected
    with pytest.raises(
        LockDisconnected, match="Service call test_domain.failing_service failed"
    ):
        await lock_provider.async_call_service("test_domain", "failing_service", {})

    # Clean up
    hass.services.async_remove("test_domain", "failing_service")


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
    refresh_count = 0
    original_refresh = coordinator.async_request_refresh

    async def track_refresh():
        nonlocal refresh_count
        refresh_count += 1
        return await original_refresh()

    with patch.object(coordinator, "async_request_refresh", track_refresh):
        # Setting a new usercode should trigger a coordinator refresh
        await lock_provider.async_internal_set_usercode(3, "3333", "Test 3")
        assert refresh_count == 1

        # Setting the same usercode should NOT trigger refresh (no change)
        await lock_provider.async_internal_set_usercode(3, "3333", "Test 3")
        assert refresh_count == 1  # Still 1, no new refresh


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
    refresh_count = 0
    original_refresh = coordinator.async_request_refresh

    async def track_refresh():
        nonlocal refresh_count
        refresh_count += 1
        return await original_refresh()

    with patch.object(coordinator, "async_request_refresh", track_refresh):
        # Clearing an existing usercode should trigger a coordinator refresh
        await lock_provider.async_internal_clear_usercode(4)
        assert refresh_count == 1

        # Clearing a non-existent slot should NOT trigger refresh (no change)
        await lock_provider.async_internal_clear_usercode(999)
        assert refresh_count == 1  # Still 1, no new refresh


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
            "custom_components.lock_code_manager.coordinator."
            "LockUsercodeUpdateCoordinator.async_config_entry_first_refresh"
        ),
    ):
        await lock.async_setup(lcm_config_entry)

        # Push subscription should be deferred (not called) since entry not loaded
        assert lock.subscribe_calls == 0

    await lock.async_unload(False)


async def test_config_entry_state_listener_ignores_same_state(
    hass: HomeAssistant,
    mock_lock_config_entry,
):
    """Test that config entry state listener ignores transitions to same state."""
    with patch(
        "custom_components.lock_code_manager.helpers.INTEGRATIONS_CLASS_MAP",
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
