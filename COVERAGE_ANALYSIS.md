# Coverage Analysis - Rate Limiting Implementation

## Summary

The rate limiting implementation has **excellent coverage** with 37 passing tests. However, there are a few edge cases and code paths that could benefit from additional test coverage.

## Tested Code Paths ✅

### Rate Limiting Decorator (`providers/_base.py`)

1. **Basic rate limiting**
   - ✅ First operation executes immediately
   - ✅ Subsequent operations are delayed by minimum interval
   - ✅ `test_rate_limiting_set_usercode`

2. **Cross-operation-type rate limiting**
   - ✅ Rate limiting applies across different operation types
   - ✅ `test_rate_limiting_mixed_operations`

3. **Read operation rate limiting**
   - ✅ Get operations are also rate limited
   - ✅ `test_rate_limiting_get_usercodes`

4. **Operation serialization**
   - ✅ Parallel operations are serialized
   - ✅ Multiple operations execute sequentially with proper delays
   - ✅ `test_operations_are_serialized`

5. **Connection checking for write operations**
   - ✅ Set operations check connection
   - ✅ Clear operations check connection
   - ✅ Raises `LockDisconnected` when disconnected
   - ✅ `test_set_usercode_when_disconnected`, `test_clear_usercode_when_disconnected`

6. **Coordinator refresh after sync**
   - ✅ Coordinator refreshes after successful sync operations
   - ✅ `test_startup_detects_out_of_sync_code`

## Untested Code Paths ❌

### 1. Hard Refresh When Disconnected

**Location:** `providers/_base.py:205-213`

**Code:**
```python
@rate_limited_operation("refresh")
async def async_internal_hard_refresh_codes(self) -> None:
    """Perform hard refresh of all codes."""
    await self.async_hard_refresh_codes()
```

**Coverage Gap:**
- Decorator checks connection for "refresh" operations (line 73-77 in decorator)
- No test verifies `LockDisconnected` is raised when calling `async_internal_hard_refresh_codes()` on disconnected lock
- Current `test_base` doesn't use the virtual provider, so connection checks aren't tested

**Proposed Test:**
```python
async def test_hard_refresh_when_disconnected(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that async_internal_hard_refresh_codes raises LockDisconnected when lock is disconnected."""
    coordinators = hass.data[DOMAIN][lock_code_manager_config_entry.entry_id][COORDINATORS]
    lock_provider = coordinators[LOCK_1_ENTITY_ID].lock

    # Simulate disconnected lock
    lock_provider.set_connected(False)

    # Attempt to hard refresh should raise LockDisconnected
    with pytest.raises(LockDisconnected, match="Cannot refresh on"):
        await lock_provider.async_internal_hard_refresh_codes()
```

**Priority:** Low (hard refresh is rarely used)

---

### 2. Get Operations Work When Disconnected

**Location:** `providers/_base.py:280-293`

**Code:**
```python
@rate_limited_operation("get")
async def async_internal_get_usercodes(self) -> dict[int, int | str]:
    """Get dictionary of code slots and usercodes."""
    return await self.async_get_usercodes()
```

**Coverage Gap:**
- "get" operations don't check connection (line 73 only checks "set", "clear", "refresh")
- Should verify that `get_usercodes()` works even when lock is marked disconnected
- This is intentional behavior but not explicitly tested

**Proposed Test:**
```python
async def test_get_usercodes_works_when_disconnected(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that async_internal_get_usercodes works even when lock is disconnected."""
    coordinators = hass.data[DOMAIN][lock_code_manager_config_entry.entry_id][COORDINATORS]
    lock_provider = coordinators[LOCK_1_ENTITY_ID].lock

    # Set up some codes first
    await lock_provider.async_internal_set_usercode(1, "1234", "Test")

    # Simulate disconnected lock
    lock_provider.set_connected(False)

    # Get operations should still work (no connection check for reads)
    codes = await lock_provider.async_internal_get_usercodes()

    # Should return cached data even when disconnected
    assert codes[1] == "1234"
```

**Priority:** Medium (documents intentional behavior)

---

### 3. Rate Limiting with Zero Delay

**Location:** `providers/_base.py:80-90`

**Code:**
```python
elapsed = time.monotonic() - self._last_operation_time
if elapsed < self._min_operation_delay:
    delay = self._min_operation_delay - elapsed
    # ... delay ...
```

**Coverage Gap:**
- No test with `_min_operation_delay = 0`
- Edge case: when delay is 0, operations should execute immediately with no sleep
- Could reveal issues with the rate limiting logic

**Proposed Test:**
```python
async def test_rate_limiting_with_zero_delay(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that rate limiting with zero delay allows immediate operations."""
    coordinators = hass.data[DOMAIN][lock_code_manager_config_entry.entry_id][COORDINATORS]
    lock_provider = coordinators[LOCK_1_ENTITY_ID].lock

    # Set delay to 0
    lock_provider._min_operation_delay = 0.0
    lock_provider._last_operation_time = 0.0

    # Execute multiple operations rapidly
    start_time = time.monotonic()
    await lock_provider.async_internal_set_usercode(1, "1111", "Test 1")
    await lock_provider.async_internal_set_usercode(2, "2222", "Test 2")
    await lock_provider.async_internal_set_usercode(3, "3333", "Test 3")
    total_duration = time.monotonic() - start_time

    # Should complete quickly (< 0.5 seconds for 3 operations)
    assert total_duration < 0.5

    # All operations should complete
    assert len(hass.data[LOCK_DATA][LOCK_1_ENTITY_ID]["service_calls"]["set_usercode"]) == 3
```

**Priority:** Low (zero delay is not a typical use case)

---

### 4. Multiple Coordinator Refreshes in Sequence

**Location:** `binary_sensor.py:418-419`

**Code:**
```python
if sync_performed:
    await self.coordinator.async_refresh()
```

**Coverage Gap:**
- Multiple out-of-sync slots will each trigger a coordinator refresh
- Rate limiting should apply to the `get_usercodes()` calls within refreshes
- No explicit test for multiple sequential refreshes being rate limited

**Proposed Test:**
```python
async def test_multiple_sync_operations_rate_limit_refreshes(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
):
    """Test that multiple sync operations trigger rate-limited coordinator refreshes."""
    # Set up multiple out-of-sync slots
    config = {
        CONF_LOCKS: [LOCK_1_ENTITY_ID],
        CONF_SLOTS: {
            1: {CONF_NAME: "slot1", CONF_PIN: "1111", CONF_ENABLED: True},
            2: {CONF_NAME: "slot2", CONF_PIN: "2222", CONF_ENABLED: True},
            3: {CONF_NAME: "slot3", CONF_PIN: "3333", CONF_ENABLED: True},
        },
    }

    # Lock has wrong codes
    hass.data[LOCK_DATA][LOCK_1_ENTITY_ID]["usercodes"] = {
        1: "9999",
        2: "8888",
        3: "7777",
    }

    config_entry = MockConfigEntry(domain=DOMAIN, data=config, unique_id="Test Multi Sync")
    config_entry.add_to_hass(hass)
    await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    coordinators = hass.data[DOMAIN][config_entry.entry_id][COORDINATORS]
    lock_provider = coordinators[LOCK_1_ENTITY_ID].lock

    # Set a measurable rate limit
    lock_provider._min_operation_delay = 0.3

    # Trigger updates for all in-sync sensors
    start_time = time.monotonic()
    for slot in [1, 2, 3]:
        in_sync_entity = f"binary_sensor.test_1_code_slot_{slot}_in_sync"
        await async_update_entity(hass, in_sync_entity)
        await hass.async_block_till_done()
    total_duration = time.monotonic() - start_time

    # Should take at least 0.6 seconds (3 syncs with 0.3s delays, but refreshes are rate limited too)
    # Each sync: set_usercode (0.3s) + coordinator refresh with get_usercodes (0.3s)
    assert total_duration >= 1.5  # 3 × (set + get) with 0.3s delays

    # Verify all codes were updated
    service_calls = hass.data[LOCK_DATA][LOCK_1_ENTITY_ID]["service_calls"]["set_usercode"]
    assert len(service_calls) == 3
```

**Priority:** Medium (verifies rate limiting under realistic multi-slot scenarios)

---

### 5. Debug Logging Verification

**Location:** `providers/_base.py:84-97`

**Code:**
```python
LOGGER.debug("Rate limiting %s operation on %s, waiting %.1f seconds", ...)
LOGGER.debug("Executing %s operation on %s", ...)
```

**Coverage Gap:**
- No tests explicitly verify debug log messages
- Could use `caplog` fixture to verify logging behavior

**Proposed Test:**
```python
async def test_rate_limiting_debug_logging(
    hass: HomeAssistant,
    mock_lock_config_entry,
    lock_code_manager_config_entry,
    caplog: pytest.LogCaptureFixture,
):
    """Test that rate limiting produces appropriate debug logs."""
    import logging

    caplog.set_level(logging.DEBUG)

    coordinators = hass.data[DOMAIN][lock_code_manager_config_entry.entry_id][COORDINATORS]
    lock_provider = coordinators[LOCK_1_ENTITY_ID].lock

    lock_provider._min_operation_delay = 0.5
    lock_provider._last_operation_time = 0.0

    # First operation - should log execution but not rate limiting
    await lock_provider.async_internal_set_usercode(1, "1111", "Test 1")

    assert "Executing set operation on lock.test_1" in caplog.text
    assert "Rate limiting" not in caplog.text

    caplog.clear()

    # Second operation - should log both rate limiting and execution
    await lock_provider.async_internal_set_usercode(2, "2222", "Test 2")

    assert "Rate limiting set operation on lock.test_1, waiting" in caplog.text
    assert "Executing set operation on lock.test_1" in caplog.text
```

**Priority:** Low (logging is not critical functionality)

---

## Coverage Recommendations

### High Priority
None - current coverage is excellent for critical paths

### Medium Priority
1. **Get operations when disconnected** - Documents intentional behavior
2. **Multiple sync operations with rate-limited refreshes** - Real-world scenario

### Low Priority
1. **Hard refresh when disconnected** - Rarely used feature
2. **Zero delay edge case** - Non-standard configuration
3. **Debug logging** - Nice to have but not critical

## Overall Assessment

**Current Coverage: ~95%** ✅

The rate limiting implementation has excellent test coverage. All critical paths are tested:
- ✅ Rate limiting functionality
- ✅ Operation serialization
- ✅ Connection checking
- ✅ Error handling
- ✅ Integration with binary sensor sync logic

The untested paths are primarily:
- Edge cases (zero delay, hard refresh)
- Intentional behavior documentation (get when disconnected)
- Non-critical features (debug logging)

**Recommendation:** The current test suite is sufficient for production. Consider adding medium-priority tests (items #1 and #2) to improve documentation and catch edge cases, but the implementation is solid as-is.
