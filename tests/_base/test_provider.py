"""Test base class."""

import asyncio
from datetime import timedelta
import time

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.lock_code_manager.const import COORDINATORS, DOMAIN
from custom_components.lock_code_manager.exceptions import LockDisconnected
from custom_components.lock_code_manager.providers._base import BaseLock

from ..common import LOCK_1_ENTITY_ID, LOCK_DATA


async def test_base(hass: HomeAssistant):
    """Test base class."""
    entity_reg = er.async_get(hass)
    config_entry = MockConfigEntry()
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
    assert await lock.async_setup() is None
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


async def test_set_usercode_when_disconnected(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that async_internal_set_usercode raises LockDisconnected when lock is disconnected."""
    # Arrange: get the provider and force it offline
    coordinators = hass.data[DOMAIN][lock_code_manager_config_entry.entry_id][
        COORDINATORS
    ]
    lock_provider = coordinators[LOCK_1_ENTITY_ID].lock

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
    coordinators = hass.data[DOMAIN][lock_code_manager_config_entry.entry_id][
        COORDINATORS
    ]
    lock_provider = coordinators[LOCK_1_ENTITY_ID].lock

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
    coordinators = hass.data[DOMAIN][lock_code_manager_config_entry.entry_id][
        COORDINATORS
    ]
    lock_provider = coordinators[LOCK_1_ENTITY_ID].lock

    # Set a smaller delay for testing (0.5 seconds instead of 2)
    lock_provider._min_operation_delay = 0.5

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

    # Second operation should take at least the rate limit time (0.5 seconds)
    assert second_duration >= 0.5

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
    coordinators = hass.data[DOMAIN][lock_code_manager_config_entry.entry_id][
        COORDINATORS
    ]
    lock_provider = coordinators[LOCK_1_ENTITY_ID].lock

    # Set a smaller delay for testing
    lock_provider._min_operation_delay = 0.5

    # First operation: set usercode
    await lock_provider.async_internal_set_usercode(1, "1111", "Test")

    # Second operation: clear usercode (different operation type, should still be rate limited)
    start_time = time.monotonic()
    await lock_provider.async_internal_clear_usercode(2)
    duration = time.monotonic() - start_time

    # Should be rate limited even though it's a different operation type
    assert duration >= 0.5


async def test_rate_limiting_get_usercodes(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that get operations are also rate limited."""
    # Arrange: shorter delay for faster assertions
    coordinators = hass.data[DOMAIN][lock_code_manager_config_entry.entry_id][
        COORDINATORS
    ]
    lock_provider = coordinators[LOCK_1_ENTITY_ID].lock

    # Set a smaller delay for testing
    lock_provider._min_operation_delay = 0.5

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
    assert second_duration >= 0.5


async def test_operations_are_serialized(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that multiple parallel operations are serialized by the lock."""
    # Arrange: shorter delay for faster assertions
    coordinators = hass.data[DOMAIN][lock_code_manager_config_entry.entry_id][
        COORDINATORS
    ]
    lock_provider = coordinators[LOCK_1_ENTITY_ID].lock

    # Set a smaller delay for testing
    lock_provider._min_operation_delay = 0.2

    # Start multiple operations in parallel
    start_time = time.monotonic()
    await asyncio.gather(
        lock_provider.async_internal_set_usercode(1, "1111", "Test 1"),
        lock_provider.async_internal_set_usercode(2, "2222", "Test 2"),
        lock_provider.async_internal_set_usercode(3, "3333", "Test 3"),
    )
    total_duration = time.monotonic() - start_time

    # With 3 operations and 0.2s delay between each:
    # - First operation: executes immediately
    # - Second operation: waits 0.2s
    # - Third operation: waits 0.2s
    # Total should be at least 0.4s (2 * 0.2s)
    assert total_duration >= 0.4

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
    coordinators = hass.data[DOMAIN][lock_code_manager_config_entry.entry_id][
        COORDINATORS
    ]
    lock_provider = coordinators[LOCK_1_ENTITY_ID].lock

    # Tighten delay to keep test quick
    lock_provider._min_operation_delay = 0.5
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
    coordinators = hass.data[DOMAIN][lock_code_manager_config_entry.entry_id][
        COORDINATORS
    ]
    lock_provider = coordinators[LOCK_1_ENTITY_ID].lock

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
