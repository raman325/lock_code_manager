# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

Lock Code Manager is a Home Assistant custom integration that allows users to manage lock usercodes across multiple locks. It reduces entity/automation sprawl compared to alternatives by handling logic internally rather than generating numerous Home Assistant entities.

## Architecture

### Core Components

**Providers System** (`custom_components/lock_code_manager/providers/`)
- Plugin-based architecture for supporting different lock integrations
- `_base.py`: `BaseLock` abstract class defining the provider interface
- `zwave_js.py`: Z-Wave JS lock implementation
- `virtual.py`: Virtual lock implementation for testing
- Each provider implements: `get_usercodes()`, `set_usercode()`, `clear_usercode()`, `is_connection_up()`, `hard_refresh_codes()`
- Providers listen for lock-specific events and translate them to LCM events via `async_fire_code_slot_event()`

**Coordinator** (`coordinator.py`)
- `LockUsercodeUpdateCoordinator`: Home Assistant DataUpdateCoordinator that polls lock providers for usercode state
- Each lock instance has its own coordinator
- Default scan interval: 1 minute (configurable per provider)

**Main Module** (`__init__.py`)
- Entry point manages config entry lifecycle
- `async_update_listener()`: Core function handling dynamic entity creation/removal when config changes
- Uses dispatcher signals to notify entities of changes (e.g., `{DOMAIN}_{entry_id}_add_lock_slot`)
- Registers Lovelace strategy resource for dashboard UI

**Config Flow** (`config_flow.py`)
- Multi-step flow: select locks → configure slots → configure individual slot properties
- Validates slots aren't already configured across other config entries
- Supports YAML object mode for advanced slot configuration

**Entities**
- `binary_sensor.py`: PIN enabled/in-sync status
- `sensor.py`: Per-lock slot PIN sensors showing current codes on each lock
- `text.py`: Name and PIN configuration entities
- `number.py`: Number of uses configuration
- `switch.py`: Slot enabled/disabled toggle
- `event.py`: PIN usage events

### Data Flow

1. User configures slots and locks via config flow
2. `async_update_listener()` creates coordinator and provider instances for each lock
3. Lock provider sets up listeners for lock-specific events (e.g., Z-Wave JS notification events)
4. Coordinator polls provider's `get_usercodes()` to keep state in sync
5. When slot config changes (name, PIN, enabled), entities call provider's `set_usercode()` or `clear_usercode()`
6. Provider fires `EVENT_LOCK_STATE_CHANGED` events when locks are operated with PINs

### Key Design Patterns

- **Dual storage**: Config entries use both `data` and `options` fields; active config in `data`, options flow updates go to `options`, then merged back to `data`
- **Internal methods**: Providers expose `async_internal_*` methods that wrap public methods with asyncio locks to prevent race conditions
- **Dispatcher signals**: Heavily used for dynamic entity management without tight coupling
- **Reauth flow**: Automatically triggered if configured lock entities are removed

## Development Commands

### Setup
```bash
scripts/setup
```
Creates Python 3.12 venv, installs dependencies with uv, sets up pre-commit hooks, and installs Node dependencies.

### Testing
```bash
pytest                          # Run all tests
pytest tests/test_init.py      # Run specific test file
pytest -k test_name            # Run tests matching pattern
pytest --cov                   # Run with coverage
```

### Linting
```bash
pre-commit run --all-files     # Run all linters
ruff check                     # Python linting
ruff format                    # Python formatting
yarn lint                      # TypeScript/JavaScript linting
yarn lint:fix                  # Auto-fix JS/TS issues
```

### Building
```bash
yarn build                     # Build frontend strategy module
yarn watch                     # Watch mode for development
```

## Code Style

- Python: Ruff for linting/formatting (line length: 88)
- Import order: future → stdlib → third-party → homeassistant → first-party → local
- Type annotations: Keep older style (`dict[str, Any]` not `dict[str, Any] | None` where possible)
- Docstrings: Google style, required for all public functions
- Async: Prefer async/await; use `hass.async_add_executor_job()` to wrap sync code

## Testing Notes

- Uses `pytest-homeassistant-custom-component` for HA test utilities
- `tests/conftest.py`: Shared fixtures
- `tests/common.py`: Helper functions for setting up test config entries and coordinators
- Mock Z-Wave JS nodes and events for testing lock providers
- Tests verify entity creation/removal through config changes

## Adding Lock Provider Support

1. Create new file in `providers/` (e.g., `zigbee.py`)
2. Subclass `BaseLock` from `providers/_base.py`
3. Implement required abstract methods:
   - `domain` property: return integration domain string
   - `is_connection_up()`: check if lock is reachable
   - `get_usercodes()`: return dict of slot→code mappings
   - `set_usercode()`: program a code to a slot
   - `clear_usercode()`: remove code from slot
4. Override `setup()` to register event listeners
5. Call `async_fire_code_slot_event()` when lock events indicate PIN usage
6. Add tests in `tests/<provider>/test_provider.py`

## Important Constraints

- Slots are numeric (usually 1-100) but can be strings for some lock types
- Multiple config entries can share the same lock but not the same slot
- Entity unique IDs: `{entry_id}|{slot}|{entity_type}`
- Lock device is linked to config entry via `dev_reg.async_update_device()`

## Home Assistant Compatibility Notes

### Recent Breaking Changes (2024.11+)

**2024.11:**
- Reauth and reconfigure flows must be linked to a config entry
- Extended deprecation period for `hass.helpers`

**2024.12:**
- Z-Wave JS requires zwave-js-server 1.39.0+ (schema 39)
- Z-Wave JS UI add-on requires v3.17.0+
- Removed deprecated Z-Wave lock service descriptions
- Python 3.13 upgrade
- Update entity `in_progress` attribute now always boolean
- New `update_percentage` attribute (0-100 or None) for update entities

### Recommended API Updates

**Config Entry Management:**
- Prefer `ConfigEntry.runtime_data` over `hass.data` for storing runtime information
- Can explicitly set `config_entry` in DataUpdateCoordinators
- New helper properties available for config entries

**DataUpdateCoordinator:**
- `_async_setup()` method available (since 2024.8) for one-time initialization
- Automatically called during `async_config_entry_first_refresh()`
- Provides same error handling as `_async_update_data` (ConfigEntryError, ConfigEntryAuthFailed)

**Z-Wave JS Updates:**
- New `node_capabilities` and `invoke_cc_api` websocket commands
- Can get/set custom config parameters for nodes
- Better support for non-dimmable color lights

**Deprecations to Watch:**
- `OptionsFlowWithConfigEntry` deprecated for core integrations
- Camera `async_handle_web_rtc_offer` and `frontend_stream_type` deprecated
- `homeassistant.util.dt.utc_to_timestamp` deprecated

## Bug Fixes and Investigations

### Startup Code Flapping Issue (2025-09-29)

**Problem:** Integration would flap between clearing and setting user codes on Home Assistant startup, even when codes were already correctly synced. This happened when the integration loaded before or during Z-Wave JS initialization, causing unnecessary battery drain and inefficient operations.

**Root Cause:** Race condition in `binary_sensor.py` where `LockCodeManagerCodeSlotInSyncEntity` would check sync status before the coordinator had stable lock state data. The sequence was:
1. In-sync entity loads and calls `_async_update_state()` immediately
2. PIN configuration entities load with their stored values
3. Lock slot sensor (coordinator data) is still empty or has stale data
4. Entity sees mismatch and triggers `set_usercode` or `clear_usercode`
5. Coordinator finally loads actual lock state
6. Now there's another mismatch (because we just changed it), triggering another operation

**Investigation:** Traced through startup flow in `__init__.py` → `coordinator.py` → `binary_sensor.py`. Found that `async_added_to_hass()` (line 361) immediately calls `_async_update_state()`, which would compare states and trigger sync operations even during initial load when data wasn't stable.

**Solution:** Added `_initial_state_loaded` flag to prevent sync operations on first load:
- Modified `__init__()` to initialize flag (line 222)
- Added check in `_async_update_state()` to verify coordinator has valid slot data before initial load (lines 293-302)
- On first load, entity now reads all states and sets in-sync status WITHOUT triggering clear/set operations (lines 321-343)
- Normal sync operations only execute after initial state is confirmed stable (lines 345-381)

**Files Changed:**
- `custom_components/lock_code_manager/binary_sensor.py`: Added startup state detection to `LockCodeManagerCodeSlotInSyncEntity`
- `tests/test_binary_sensor.py`: Added two new tests:
  - `test_startup_no_code_flapping_when_synced`: Validates that no set_usercode/clear_usercode calls are made during startup when codes are already in sync
  - `test_startup_detects_out_of_sync_code`: Validates that out-of-sync codes are correctly detected on startup (initial load marks them as out-of-sync without triggering operations), then automatically corrected on the next update cycle (via polling)

**Result:** Integration now correctly detects sync state on startup without performing unnecessary operations, preventing battery drain and code flapping during Home Assistant startup. Out-of-sync codes are still properly detected and corrected through the normal polling mechanism. Tests ensure this behavior is maintained in future updates.

### Issue #527: Dashboard UI TypeError (2025-10-02)

**Problem:** Users encountered `TypeError: Cannot read properties of undefined (reading 'entity_id')` when trying to add the Lock Code Manager custom view strategy to their Home Assistant dashboard.

**Root Cause:** The TypeScript code in `generate-view.ts` assumed that `pinActiveEntity` and `codeEventEntity` would always exist for every slot. However, these entities might not be available during initial setup or if they're disabled. Specifically:
- Line 230-232: `pinActiveEntity` was assigned using `.find()` which can return `undefined`
- Line 214: `codeEventEntity` was declared but might never be assigned if no matching entity existed
- Lines 165 and 169: Both entities had `.entity_id` accessed without null/undefined checks

**Solution:** Updated types and code to handle optional entities:
1. **Type Changes (`ts/types.ts`)**:
   - Changed `codeEventEntity: LockCodeManagerEntityEntry` to `codeEventEntity?: LockCodeManagerEntityEntry`
   - Changed `pinActiveEntity: LockCodeManagerEntityEntry` to `pinActiveEntity?: LockCodeManagerEntityEntry`

2. **Code Changes (`ts/generate-view.ts`)**:
   - Updated `codeEventEntity` declaration to `let codeEventEntity: LockCodeManagerEntityEntry | undefined`
   - Added conditional rendering in `generateSlotCard()` to only include entities if they exist:
     ```typescript
     ...(slotMapping.pinActiveEntity
         ? [{ entity: slotMapping.pinActiveEntity.entity_id, name: 'PIN active' }]
         : []),
     ...(slotMapping.codeEventEntity
         ? [{ entity: slotMapping.codeEventEntity.entity_id, name: 'PIN last used' }]
         : []),
     ```

**Files Changed:**
- `ts/types.ts`: Made `pinActiveEntity` and `codeEventEntity` optional in `SlotMapping` interface
- `ts/generate-view.ts`: Added null checks before accessing `entity_id` property
- `custom_components/lock_code_manager/www/lock-code-manager-strategy.js`: Rebuilt from TypeScript source

**Commit:** `39ff8cf`

**Result:** Dashboard UI now gracefully handles missing entities without throwing errors, allowing the view strategy to load correctly even during initial setup or with disabled entities.

### Home Assistant Compatibility Fixes (2025-10-02)

Three critical compatibility issues were identified and fixed to ensure the integration works with Home Assistant Core 2025.7, 2025.8, and 2025.11+.

#### Issue #531: Deprecated Config Import (HA Core 2025.11)

**Problem:** Integration was using deprecated `Config` import from `homeassistant.core`, which will be removed in HA Core 2025.11. Users were seeing deprecation warnings asking them to report the issue.

**Root Cause:** The core config class was moved from `homeassistant/core.py` to `homeassistant/core_config.py` to improve code organization. The old import path was deprecated with a grace period until 2025.11.

**Solution:** Updated import statement:
- Changed from: `from homeassistant.core import Config`
- Changed to: `from homeassistant.core_config import Config`

**Files Changed:**
- `custom_components/lock_code_manager/__init__.py`: Updated Config import to use new location

**Commit:** `658d5d2`

#### Issue #530/#528: Deprecated register_static_path (HA Core 2025.7+)

**Problem:** Integration was using synchronous `hass.http.register_static_path()` which performs blocking I/O in the event loop. This method was deprecated and removed in HA Core 2025.7.

**Root Cause:** The synchronous static path registration blocks the event loop, which can cause performance issues. Home Assistant moved to an async-only API for static path registration.

**Solution:** Replaced with async API:
- Changed from: `hass.http.register_static_path(url, path)`
- Changed to: `await hass.http.async_register_static_paths([StaticPathConfig(url, path, cache_headers)])`
- Added import: `from homeassistant.components.http import StaticPathConfig`

**Files Changed:**
- `custom_components/lock_code_manager/__init__.py`: Updated static path registration to async API

**Commit:** `e9eb1cd`

#### Issue #530: Z-Wave JS DATA_CLIENT Deprecation (HA Core 2025.8+)

**Problem:** Integration was using deprecated `DATA_CLIENT` constant to access Z-Wave JS client objects. The internal data structure for Z-Wave JS integration changed in HA Core 2025.8, causing AttributeError when using the old access pattern.

**Root Cause:** Home Assistant 2025.8 changed how Z-Wave JS stores client objects internally. The old dictionary-based access via `DATA_CLIENT` key was replaced with a new `_client_driver_map` attribute structure.

**Investigation:** Reviewed PR #530 which showed the exact changes needed. The new pattern uses `getattr()` to access `_client_driver_map` and retrieves client from a `client_entry` object.

**Solution:** Updated client access in `async_is_connection_up()`:
- Removed import: `DATA_CLIENT` from `homeassistant.components.zwave_js.const`
- Changed from: Dictionary access using `hass.data[ZWAVE_JS_DOMAIN][entry_id][DATA_CLIENT]`
- Changed to: Attribute access using `getattr(zwave_data, "_client_driver_map", {}).get(entry_id)`
- Updated client reference from `client` to `client_entry.client`

**Bonus Fix:** Modernized imports by moving `Iterable` from `typing` to `collections.abc` (Python 3.9+ best practice).

**Files Changed:**
- `custom_components/lock_code_manager/providers/zwave_js.py`: Updated Z-Wave JS client access pattern and modernized imports

**Commit:** `3023fc4`

**Result:** Integration is now fully compatible with Home Assistant Core 2025.7, 2025.8, and 2025.11+. All deprecation warnings are eliminated, and the integration uses current APIs that won't break in future releases.

### Python 3.13 Upgrade and Home Assistant 2025.10 Compatibility (2025-10-05)

**Problem:** The `test_get_slot_calendar_data` test was failing in CI with thread cleanup errors. Initially thought to be a test teardown issue, investigation revealed the actual root cause was outdated dependencies.

**Root Cause:** The test was failing due to outdated dependencies:
- Python 3.12 (current: 3.13)
- `pytest-homeassistant-custom-component` 0.13.201 (current: 0.13.286, 85 versions behind)
- `zeroconf` 0.137.2 (required by HA 2025.10.1: 0.147.2)
- `zwave-js-server-python` 0.58.1 (required by HA 2025.10.1: 0.67.1)

**Solution:** Updated all dependencies to their latest versions. The test passes as-is with no test logic changes required.

**Dependency Upgrades:**
- Python 3.12 → **3.13** (`.github/workflows/pytest.yaml`)
- `pytest-homeassistant-custom-component` 0.13.201 → **0.13.286** (required Python 3.13)
- `zeroconf` 0.137.2 → **0.147.2**
- `zwave-js-server-python` 0.58.1 → **0.67.1**
- GitHub Actions: Updated `actions/checkout`, `actions/setup-python`, and `codecov/codecov-action` to latest versions

**API Compatibility Fixes Required:**

Upgrading to Python 3.13 and Home Assistant 2025.10 required fixing several deprecated APIs:

1. **pytest-asyncio 1.2.0+** (`tests/conftest.py`):
   - Removed deprecated `event_loop` parameter from `aiohttp_client` fixture
   ```python
   # Before: def aiohttp_client(event_loop, aiohttp_client, socket_enabled)
   # After:  def aiohttp_client(aiohttp_client, socket_enabled)
   ```

2. **HA 2025.10+ Config Entry Setup** (`tests/conftest.py`, `custom_components/lock_code_manager/__init__.py`):
   - `async_forward_entry_setup()` (singular) → `async_forward_entry_setups()` (plural)
   ```python
   # Before: await hass.config_entries.async_forward_entry_setup(config_entry, platform)
   # After:  await hass.config_entries.async_forward_entry_setups(config_entry, [platform])
   ```

3. **HA 2025.10+ Entity Registry** (`tests/_base/test_provider.py`, `tests/virtual/test_provider.py`):
   - `RegistryEntry` constructor no longer accepts positional arguments
   - Must use `async_get_or_create()` instead
   ```python
   # Before: er.RegistryEntry("lock.test", "blah", "blah")
   # After:  entity_reg.async_get_or_create("lock", "test", "test_lock", config_entry=config_entry)
   ```

4. **HA 2025.10+ Lovelace Data Access** (`tests/test_init.py`, `custom_components/lock_code_manager/__init__.py`):
   - Lovelace data structure changed from dict-like to object with attributes
   ```python
   # Before: resources = hass.data[LL_DOMAIN].get("resources")
   # After:  resources = hass.data[LL_DOMAIN].resources
   ```

**Other Improvements:**
- `tests/common.py`: Added `@callback` decorators to `setup()` and `unload()` methods in `MockLCMLock`
- `tests/test_websocket.py`: Added clarifying comment for manual config entry unload

**Files Changed:**
- `.github/workflows/pytest.yaml`: Python 3.13, updated action versions
- `.github/workflows/*.yaml`: Updated action versions across all workflows
- `requirements_test.txt`: pytest-homeassistant 0.13.286
- `requirements_dev.txt`: zeroconf 0.147.2, zwave-js-server-python 0.67.1
- `tests/conftest.py`: Removed event_loop parameter, updated async_forward_entry_setups API
- `tests/common.py`: Added @callback decorators
- `tests/test_websocket.py`: Added clarifying comment
- `tests/test_init.py`: Updated lovelace data access pattern
- `tests/_base/test_provider.py`: Fixed RegistryEntry usage
- `tests/virtual/test_provider.py`: Fixed RegistryEntry usage
- `custom_components/lock_code_manager/__init__.py`: Updated async_forward_entry_setups and lovelace APIs

**Test Results:**
- ✅ **All 27 tests passing** (100% pass rate)
- ✅ All websocket tests pass (including the originally failing test)
- ✅ No thread teardown errors
- ✅ Full compatibility with Python 3.13 and Home Assistant 2025.10.1

**Key Takeaway:** What appeared to be a complex test teardown issue was actually just outdated dependencies. Updating to the latest versions fixed the test immediately, though it required addressing several API deprecations to maintain compatibility.

**PR:** #552
**Commits:** fd01fec (main fix) + multiple API compatibility updates

### Entity Creation Blocking Issue (2025-11-04)

**Problem:** No entities were being generated when creating a config entry with locks and code slots. Users would configure the integration, but no entities would appear in Home Assistant.

**Root Cause Analysis:** The integration was blocking during `async_update_listener()` while waiting for Z-Wave JS locks to be connected before creating entities. The blocking wait loop in `__init__.py` (lines 448-462) would infinitely retry connection checks during startup:

```python
while not await lock.async_internal_is_connection_up():
    await asyncio.sleep(timeout)
    timeout = min(timeout * 2, 180)
```

This created a startup race condition:
1. Lock Code Manager loads and forwards entity platform setups
2. `async_update_listener()` is called to create entities
3. Lock provider checks if Z-Wave JS is connected - it's not (still loading)
4. Integration waits indefinitely for connection
5. **Entity creation completely blocked** - dispatcher signals never sent
6. Z-Wave JS eventually loads, but entities are never created because the flow never progressed

**Why Previous "Fixes" Were Wrong:**

**Issue #527 / PR #534 (2025-10-03):**
- **Wrong Solution:** Made dashboard UI entities optional (`pinActiveEntity?: LockCodeManagerEntityEntry`, `codeEventEntity?: LockCodeManagerEntityEntry`) and added conditional rendering with optional chaining
- **Why Wrong:** This was treating the symptom (missing entities) as expected behavior rather than fixing the root cause (entities not being created at all)
- **Result:** Masked the real problem - the UI would silently fail to show entities instead of throwing an error that would reveal the bug

**PR #594 (2025-11-03):**
- **Wrong Solution:** Added more optional chaining (`resource.url?.includes()`) to handle potentially undefined values
- **Why Wrong:** Further defensive programming around missing entities, continuing to mask the underlying issue
- **Result:** Made it even harder to detect that entities weren't being created, as the UI gracefully degraded

**The Correct Solution:**

The fix addresses the root cause by removing the blocking wait and handling Z-Wave JS initialization gracefully:

1. **Removed Blocking Wait Loop** (`__init__.py` lines 447-455):
   - Changed from infinite `while` loop to single connection check
   - Log DEBUG message if not connected (not WARNING - this is expected during startup)
   - Continue with entity creation regardless of connection state
   - Wrapped coordinator first refresh in try/except to handle disconnected state

2. **Added Connection Checks in Provider** (`providers/_base.py`):
   - Import `LockDisconnected` exception at module level (line 40)
   - Check connection in `async_internal_set_usercode()` before attempting to set codes (lines 168-171)
   - Check connection in `async_internal_clear_usercode()` before attempting to clear codes (lines 187-190)
   - Raise `LockDisconnected` exception if lock not ready

3. **Added Error Handling in Sync Logic** (`binary_sensor.py`):
   - Added connection check in `async_update()` to prevent sync attempts when disconnected (line 237)
   - Wrapped `async_internal_set_usercode()` call in try/except (lines 309-329)
   - Wrapped `async_internal_clear_usercode()` call in try/except (lines 338-356)
   - Log failures as DEBUG (not ERROR) since this is expected during startup

4. **Reverted Defensive UI Changes** (`ts/types.ts`, `ts/generate-view.ts`):
   - Changed `pinActiveEntity` and `codeEventEntity` back to required (non-optional)
   - Removed optional chaining and conditional rendering
   - Removed defensive logging about missing entities
   - Added non-null assertion (`!`) to `.find()` call since entities will always exist
   - UI now fails fast if entities truly missing, making problems immediately obvious

**Behavior After Fix:**

**During Startup (Z-Wave JS not ready):**
- ✅ Entities created immediately
- ✅ Entities show as "unavailable" until locks come online
- ✅ No blocking or infinite waits
- ✅ Clean DEBUG logs (not WARNING or ERROR)
- ✅ No failed attempts to program codes

**Once Z-Wave JS Ready:**
- ✅ Entities automatically become available
- ✅ Coordinator polls and syncs codes properly
- ✅ Dashboard UI works correctly with all entities present

**Files Changed:**
- `custom_components/lock_code_manager/__init__.py`: Removed blocking wait loop, added graceful connection check
- `custom_components/lock_code_manager/providers/_base.py`: Added connection checks with `LockDisconnected` exception
- `custom_components/lock_code_manager/binary_sensor.py`: Added connection check and error handling for sync operations
- `ts/types.ts`: Reverted optional entity types from PR #534
- `ts/generate-view.ts`: Reverted optional chaining and conditional rendering from PRs #534 & #594
- `custom_components/lock_code_manager/www/lock-code-manager-strategy.js`: Rebuilt from TypeScript

**Key Lessons:**
1. **Fix root causes, not symptoms** - The entities should always exist; making them optional was hiding the real problem
2. **Race conditions during startup are common** - Integrations must handle dependencies loading at different times
3. **Blocking operations during setup prevent entity creation** - Entity creation should be non-blocking
4. **Fail fast in UI** - Better to get an error that reveals a bug than silently handle missing data
5. **Log levels matter** - Expected startup behavior should be DEBUG, not WARNING

**Result:** Entities are now created immediately and reliably, regardless of Z-Wave JS load timing. The integration handles startup race conditions gracefully without blocking, and the UI correctly expects all entities to be present.

## Future Improvements & TODOs

This section tracks potential improvements, refactoring opportunities, and feature requests. Claude will periodically ask if you're ready to address these items.

### 1. Fix Locks Out of Sync Issue

**Problem:** Users report that locks go out of sync and the integration doesn't automatically re-sync them. The in-sync binary sensor shows "off" but the integration doesn't trigger automatic sync operations to fix the issue.

**Initial Analysis:** Based on PR review comments, the current auto-sync logic has issues:

**Current Behavior (in `binary_sensor.py`):**
- `LockCodeManagerCodeSlotInSyncEntity.async_update()` (lines 230-246) attempts to auto-sync when:
  - Lock is not locked (`self._lock.locked()` is False)
  - Entity shows out of sync (`self.is_on` is False)
  - Lock state is available
  - Coordinator successfully updated
- If conditions met, calls `_async_update_state()` to perform sync

**Issues Identified:**
1. **Insufficient sync triggers**: Only runs during polling interval (default 30 seconds from `SCAN_INTERVAL`)
2. **Lock condition problematic**: `self._lock.locked()` check may prevent syncing when it should happen
3. **No user-initiated sync**: No manual way to force re-sync
4. **Polling dependency**: Relies entirely on periodic polling, doesn't respond to state changes

**Root Cause Areas to Investigate:**
1. **When does `async_update()` actually get called?**
   - Currently only during coordinator updates (polling)
   - State change events don't trigger sync attempts

2. **What does `self._lock.locked()` check?**
   - This is an `asyncio.Lock()` (line 216), not lock state
   - Prevents concurrent sync operations
   - May block legitimate sync attempts if lock held

3. **Why doesn't state change listener trigger sync?**
   - `async_track_state_change_filtered()` (line 378) calls `_async_update_state()`
   - But `_async_update_state()` has guards that may prevent action (lines 264-282)
   - May need to trigger sync more aggressively on state changes

**Proposed Solutions:**

**Option 1: More Aggressive Auto-Sync**
- Remove or adjust `self._lock.locked()` check in `async_update()`
- Add retry logic with exponential backoff
- Trigger sync on any PIN/name/active state change, not just during polling
- Add sync on coordinator refresh success

**Option 2: Manual Sync Service**
- Add `lock_code_manager.sync_slot` service action
- Add `lock_code_manager.sync_all_slots` service action
- Allow users to manually trigger sync when they notice issues
- Could be called from automations

**Option 3: Better State Change Detection**
- Enhance `_async_update_state()` to be more responsive
- Remove guards that prevent sync on legitimate state changes
- Add more detailed logging to understand why syncs don't happen
- Consider firing HA events when sync fails repeatedly

**Investigation Steps:**
1. Add detailed logging to understand when `async_update()` is called
2. Add logging to show why sync operations are skipped
3. Monitor `self._lock.locked()` state to see if it blocks syncs
4. Review state change event handling to ensure it triggers appropriately
5. Test with deliberately out-of-sync locks to reproduce issue

**Files to Modify:**
- `custom_components/lock_code_manager/binary_sensor.py`: Lines 230-246 (async_update), 254-370 (_async_update_state)
- Potentially add new service actions in `custom_components/lock_code_manager/__init__.py`
- Add tests in `tests/test_binary_sensor.py` for sync behavior

**Estimated Effort:** High (16-24 hours) - Requires investigation, testing, and careful changes to sync logic

**Priority:** **HIGH** - User-reported issue affecting core functionality

**Status:** Not started - Needs investigation phase first

**Related Issues:** Review comments on PR indicating locks don't auto-sync

---

### 2. Remove Commented Code

**Initial Analysis:** Search codebase for commented-out code blocks and remove dead code to improve maintainability.

**Scope:**
- Python files: Look for `# ` commented code blocks (not docstrings or inline comments)
- TypeScript files: Look for `//` or `/* */` commented code blocks
- Configuration files

**Estimated Effort:** Low (1-2 hours)

**Priority:** Low - Technical debt cleanup

**Status:** Not started

---

### 3. Simplify Tests

**Initial Analysis:** Review test suite for duplication, overly complex setup, and opportunities to use shared fixtures more effectively.

**Current Issues:**
- `tests/common.py` has some duplicated mock setup code
- Some tests may have redundant assertions
- Test fixtures could potentially be more reusable across test files

**Potential Improvements:**
- Consolidate duplicate test setup code into shared fixtures
- Review test naming conventions for clarity
- Consider parametrized tests where multiple similar tests exist
- Reduce test boilerplate with helper functions

**Estimated Effort:** Medium (4-8 hours)

**Priority:** Medium - Improves developer experience and test maintainability

**Status:** Not started

---

### 4. Simplify Code

#### 4a. Remove Dispatcher Complexity

**Initial Analysis:** The integration heavily uses Home Assistant's dispatcher system for dynamic entity management. Evaluate if this can be simplified or if there's a more modern approach.

**Current Usage:** (`__init__.py` lines 27-30, various entity files)
- Dispatcher signals used for: `add_lock_slot`, `update_lock_slot`, `remove_lock_slot`
- Each entity listens for dispatcher signals to handle dynamic config changes
- Pattern: `{DOMAIN}_{entry_id}_action_type`

**Considerations:**
- **Pros of current approach:** Decoupled, allows dynamic entity creation without tight coupling
- **Cons:** More complex to trace, multiple layers of indirection
- **Alternative:** Use `ConfigEntry.async_on_unload()` callbacks or `ConfigEntry.runtime_data` more extensively

**Questions to Answer:**
1. Can we use `ConfigEntry.add_update_listener()` instead of dispatchers for config changes?
2. Would storing entity references in `ConfigEntry.runtime_data` allow more direct updates?
3. Is the dispatcher pattern necessary for the dynamic slot management, or is there a simpler way?

**Estimated Effort:** High (16+ hours) - Major architectural change

**Priority:** Medium - Would improve code clarity but requires careful refactoring

**Status:** Not started - Needs design review first

#### 4b. Remove Other Unnecessary Complexity

**Initial Analysis:** General code review to identify and eliminate unnecessary complexity.

**Areas to Review:**
1. **Dual storage pattern** (`data` + `options` in config entries) - Can this be simplified?
2. **Entity unique ID format** - Is `{entry_id}|{slot}|{type}` optimal?
3. **Multiple coordinator instances** - One per lock - could this be unified?
4. **Internal method wrappers** - `async_internal_*` methods with locks - are all necessary?

**Specific Items:**
- Review if all `_get_entity_state()` calls can be simplified
- Evaluate if `_entity_id_map` dictionary caching is worth the complexity
- Consider if provider `async_internal_*` wrapper methods can be simplified
- Review entity base classes for potential consolidation

**Estimated Effort:** High (20+ hours) - Requires deep understanding and careful refactoring

**Priority:** Low-Medium - Would improve long-term maintainability

**Status:** Not started - Needs comprehensive audit first

#### 3c. Move Sync Logic to Coordinator

**Initial Analysis:** The startup flapping fix (see Bug Fixes section) added `_initial_state_loaded` flag to handle race conditions during startup. This complexity exists because the in-sync binary sensor reads state from other entities and compares with coordinator data, creating timing dependencies.

**Current Architecture Issues:**
- In-sync binary sensor reads PIN config from text entities via `_get_entity_state()`
- Reads lock state from coordinator data
- Compares them and triggers sync operations (set_usercode/clear_usercode)
- Cross-entity dependencies create race conditions during startup
- Requires `_initial_state_loaded` flag to prevent flapping

**Proposed Solution:**
Move sync logic entirely into the coordinator:
- Coordinator already has access to both desired state (from config) and actual state (from lock)
- Coordinator performs sync operations during its `_async_update_data()` cycle
- Binary sensor becomes read-only, just displays coordinator's computed in-sync status
- Text/number/switch entities remain as config views

**Example Implementation:**
```python
# In coordinator._async_update_data()
actual_code = await self.provider.get_usercodes()
desired_code = self.config_entry.data[slot]["pin"]

if actual_code != desired_code and slot_enabled:
    await self.provider.set_usercode(slot, desired_code)

return {"in_sync": actual_code == desired_code, "actual_code": actual_code}
```

**Benefits:**
- Eliminates cross-entity state reading
- Removes `_initial_state_loaded` flag and startup detection logic
- No race conditions during startup
- Simpler, more centralized sync logic
- Coordinator is single source of truth

**Considerations:**
- Major architectural change
- Would need to update binary sensor to be read-only
- Config updates still flow through text/switch entities
- Need to ensure coordinator runs sync on config changes

**Estimated Effort:** High (16-24 hours) - Significant architectural refactoring

**Priority:** Medium - Would eliminate startup complexity and simplify architecture

**Status:** Not started - Consider after current PR is merged

---

### 5. Advanced Calendar Configuration

**Feature Request:** Allow slot number and PIN to be configured directly in calendar event metadata, eliminating need for separate slot configuration.

**Design Considerations:**

**Current Behavior:**
- Slot configured in config flow with fixed PIN
- Calendar event (when present) only controls whether slot is active/inactive
- PIN and slot number are static configuration

**Proposed Behavior:**
- Calendar event contains slot number and PIN in its metadata
- User configures regex/pattern to extract slot number and PIN from:
  - Event title (e.g., "Slot 3: 1234")
  - Event description
  - Event location
  - Custom calendar properties

**Implementation Requirements:**
1. **Config Flow Changes:**
   - Add "advanced calendar mode" toggle per slot
   - When enabled, show pattern configuration UI
   - Pattern fields: slot number regex, PIN regex, which field to parse (title/description/location)

2. **Entity Changes:**
   - Calendar event listener needs to parse event metadata
   - Extract slot number and PIN based on user-defined patterns
   - Validate extracted values (numeric slot, PIN format)

3. **Binary Sensor Changes:**
   - Check if calendar event contains valid extracted values
   - Use extracted PIN instead of configured PIN
   - Handle multiple calendar events with different slots

**Example Patterns:**
- Title: `"Guest Access: Slot 5, PIN 9876"` → Slot: `\d+`, PIN: `\d{4}$`
- Description: `"Code: 1234 for slot 3"` → Slot: `slot (\d+)`, PIN: `Code: (\d+)`
- Location: `"5:1234"` → Slot: `^(\d+):`, PIN: `:(\d+)$`

**Challenges:**
- Multiple calendar events with different slots
- Error handling for invalid patterns
- UI for testing patterns
- Backward compatibility with existing simple calendar mode

**Estimated Effort:** Very High (40+ hours) - New feature with UI, validation, and testing

**Priority:** Medium - Power user feature, not essential for basic functionality

**Status:** Not started - Needs detailed design document

---

### 6. Add Relevant New Home Assistant Core Features

**Analysis:** Review Home Assistant release notes from 2024.1 through 2025.10 and integrate relevant new features.

**Key Features to Evaluate (2024-2025):**

1. **Config Entry Runtime Data** (2024.8+)
   - `ConfigEntry.runtime_data` for type-safe runtime storage
   - Could replace some `hass.data[DOMAIN]` usage
   - **Action:** Audit `hass.data` usage and migrate to `runtime_data` where appropriate

2. **DataUpdateCoordinator `_async_setup()`** (2024.8+)
   - One-time initialization method
   - **Action:** Evaluate if any coordinator setup code should move to `_async_setup()`

3. **Entity Category Enhancements** (2024.x)
   - New entity categories available
   - **Action:** Review entity category assignments

4. **Selector Improvements** (2024.x)
   - New selector types for config flow
   - **Action:** Review config flow UI for better selectors

5. **LockState Enum** (2025.10)
   - Replace deprecated `STATE_LOCKED`/`STATE_UNLOCKED` constants
   - **Action:** Update to use `LockState.LOCKED` / `LockState.UNLOCKED`

6. **Repair Platform** (2024.x)
   - Notify users of configuration issues
   - **Action:** Consider adding repairs for common misconfigurations

7. **Config Entry Diagnostics** (2024.x)
   - Better debugging information
   - **Action:** Add diagnostics download capability

**Estimated Effort:** Medium-High (12-20 hours) - Depends on features adopted

**Priority:** Medium - Keeps integration modern and leverages platform improvements

**Status:** Not started - Needs systematic review of release notes

---

### 7. Add Support for Additional Lock Providers

**Current Providers:**
- Z-Wave JS (`zwave_js.py`)
- Virtual (`virtual.py`) - for testing only

**Available Lock Integrations in Home Assistant Core:**

Based on analysis of `home-assistant/core` repository, the following 62 integrations provide lock entities:

**Smart Home Platforms:**
- `deconz` - deCONZ (Zigbee/Z-Wave gateway)
- `esphome` - ESPHome devices
- `homematic` - Homematic (CCU)
- `homematicip_cloud` - Homematic IP Cloud
- `homekit_controller` - HomeKit accessories
- `matter` - Matter protocol
- `mqtt` - MQTT locks
- `smartthings` - SmartThings
- `zha` - Zigbee Home Automation
- `zwave_js` - Z-Wave JS (already supported ✅)
- `zwave_me` - Z-Wave.Me

**Brand-Specific Integrations:**
- `abode` - Abode Security
- `august` - August Smart Locks
- `bmw_connected_drive` - BMW Connected Drive
- `dormakaba_dkey` - Dormakaba dkey
- `igloohome` - igloohome
- `kiwi` - Kiwi (Eufy)
- `loqed` - Loqed Smart Lock
- `nuki` - Nuki Smart Lock
- `schlage` - Schlage Encode
- `sesame` - Sesame Smart Lock
- `switchbot` - SwitchBot Lock
- `switchbot_cloud` - SwitchBot Cloud
- `tedee` - Tedee Smart Lock
- `yale` - Yale Access (August partnership)
- `yalexs_ble` - Yale/August BLE
- `yolink` - YoLink

**Security Systems:**
- `simplisafe` - SimpliSafe
- `verisure` - Verisure
- `yale_smart_alarm` - Yale Smart Alarm

**Vehicle Integrations:**
- `subaru` - Subaru Starlink
- `tesla_fleet` - Tesla Fleet API
- `teslemetry` - Teslemetry
- `tessie` - Tessie (Tesla)
- `starline` - StarLine

**Other Integrations:**
- `fibaro` - Fibaro
- `freedompro` - Freedompro
- `insteon` - Insteon
- `isy994` - Universal Devices ISY
- `keba` - KEBA EV Charger
- `overkiz` - Overkiz (Somfy TaHoma)
- `surepetcare` - Sure Petcare
- `unifiprotect` - UniFi Protect
- `vera` - Vera
- `wallbox` - Wallbox EV Charger
- `xiaomi_aqara` - Xiaomi Aqara

**Utility/Helper Integrations:**
- `demo` - Demo platform
- `group` - Lock groups
- `kitchen_sink` - Testing platform
- `switch_as_x` - Convert switches to locks
- `template` - Template locks
- `homee` - Homee gateway

**Recommended Priorities for Support:**

**High Priority** (Popular, widely used):
1. **ZHA (Zigbee Home Automation)** - Very popular, supports many lock brands
2. **Matter** - Future-proof, industry standard
3. **ESPHome** - DIY community, custom locks
4. **MQTT** - Generic protocol, many custom implementations

**Medium Priority** (Brand-specific, popular):
5. **August/Yale** (`august`, `yale`, `yalexs_ble`) - Popular smart lock brand
6. **Nuki** - Popular in Europe
7. **Schlage** - Popular in North America
8. **SwitchBot** - Growing popularity

**Low Priority** (Niche or less common):
- Vehicle locks (Tesla, BMW, Subaru) - Different use case
- Security system locks - Usually managed by their own systems
- Utility integrations (template, group) - May work without specific provider

**Implementation Approach:**

For each new provider:
1. Create `providers/INTEGRATION_NAME.py`
2. Subclass `BaseLock`
3. Implement required methods
4. Add integration-specific event listeners
5. Add tests in `tests/INTEGRATION_NAME/test_provider.py`
6. Update documentation

**Estimated Effort per Provider:** Medium (6-12 hours each)

**Priority:** Medium-High - Expands integration usefulness

**Status:** Not started

**Recommended First Addition:** ZHA (Zigbee) - Most requested, widely used

---

### Additional Improvement Ideas

#### 8. Improve Dashboard UI/UX

**Ideas:**
- Add visual indicator when codes are out of sync
- Bulk operations (enable/disable multiple slots)
- Import/export slot configuration
- QR code generation for PIN sharing
- History view showing when codes were used
- Slot templates for quick configuration

**Estimated Effort:** High (20+ hours)

**Priority:** Medium

**Status:** Not started

---

#### 9. Add Service Actions

**Ideas:**
- `lock_code_manager.set_temporary_code` - Create time-limited PIN
- `lock_code_manager.generate_pin` - Auto-generate secure PIN
- `lock_code_manager.bulk_enable` - Enable multiple slots at once
- `lock_code_manager.bulk_disable` - Disable multiple slots at once
- `lock_code_manager.copy_slot` - Copy configuration from one slot to another

**Estimated Effort:** Medium (8-16 hours)

**Priority:** Medium-Low

**Status:** Not started

---

#### 10. Enhanced Notifications

**Ideas:**
- Notify when PIN is used (already have event entity, could add notification action)
- Notify when code goes out of sync
- Notify when calendar event starts (PIN becomes active)
- Notify when number of uses is depleted

**Estimated Effort:** Medium (6-10 hours)

**Priority:** Low - Can be done with automations currently

**Status:** Not started

---

#### 11. Configuration Validation

**Ideas:**
- Warn if PIN is too simple (e.g., "1234", "0000")
- Warn if multiple slots use same PIN
- Validate PIN format against lock requirements
- Check for slot conflicts across config entries

**Estimated Effort:** Low-Medium (4-8 hours)

**Priority:** Medium

**Status:** Not started

---

### Review Schedule

Claude will ask about addressing these TODOs:
- After completing current work
- When starting new features
- During refactoring sessions
- At your request with `/todos` command
