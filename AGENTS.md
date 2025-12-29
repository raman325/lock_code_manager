# AGENTS.md

This file provides guidance to non-Claude coding agents (e.g., GPT-4/4o/5 via Codex CLI) when working with code in this repository. It mirrors `CLAUDE.md` so both assistants share a consistent view of the project.

## Overview

Lock Code Manager is a Home Assistant custom integration that allows users to manage lock usercodes across multiple locks. It reduces entity/automation sprawl compared to alternatives by handling logic internally rather than generating numerous Home Assistant entities.

## Codex Agent Context (2025-02 Session)

- `BaseLock` now performs its own rate limiting + connection checks without Tenacity. Tenacity was removed from all dependency lists (manifest + requirements_*), and operations fail fast with `LockDisconnected` if the lock isn’t connected—don’t reintroduce Tenacity unless absolutely necessary.
- We added a lightweight retry scheduler in `LockCodeManagerCodeSlotInSyncEntity`: when a sync fails because the lock is offline, the entity schedules its own retry instead of blocking HA. Expect to see `_retry_unsub` state on those entities during tests.
- The in-sync entity now waits for dependent entity states/availability before acting, ignores irrelevant/unavailable events, and schedules a 10s retry on `LockDisconnected`. New coverage: `test_in_sync_waits_for_missing_pin_state`, `test_entities_track_availability`, the reconnect paths in `test_handles_disconnected_lock_on_set/clear`, and `test_startup_out_of_sync_slots_sync_once` (verifies we sync each slot once without extra calls).
- Provider connection failures no longer advance rate-limit timing; see `test_connection_failure_does_not_rate_limit_next_operation` for regression coverage.
- Lovelace strategy resource: only needs to be registered once globally; if HA is in YAML mode we skip removal on unload (mirrors ha_scrypted handling). New test `test_resource_unload_skips_yaml_mode` covers the YAML guard.
- Claude and Codex collaborated on the startup flapping fix (see “Startup Code Flapping Issue” below). Connection handling is no longer regressed after this session, but keep an eye on `tests/_base/test_provider.py::test_*disconnected` and `tests/test_binary_sensor.py::test_handles_disconnected_lock_on_*` whenever touching provider logic.
- A focused test command that confirms the connection/retry behaviour is `source venv/bin/activate && pytest tests/_base/test_provider.py -k disconnected -q`.
- This session also added the `AGENTS.md` mirror of `CLAUDE.md` so future coding agents (e.g., GPT-based) can track their own context along with Claude’s notes. Keep both files updated when architecture/process guidance changes.

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
Creates Python 3.13 venv, installs dependencies with uv, sets up pre-commit hooks, and installs Node dependencies.

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

**Problem:** Integration flapped between clearing/setting codes on startup when codes were already synced, causing battery drain.

**Root Cause:** Race condition - in-sync entity checked status before coordinator had stable lock state data.

**Solution:** Added `_initial_state_loaded` flag to prevent sync operations on first load. On initial load, entity reads all states and sets in-sync status WITHOUT triggering operations. Normal sync executes only after initial state is stable.

**Files:** `binary_sensor.py`, `tests/test_binary_sensor.py`
**Tests:** `test_startup_no_code_flapping_when_synced`, `test_startup_detects_out_of_sync_code`

### Home Assistant Compatibility Fixes (2025-10-02)

Fixed three critical compatibility issues for HA Core 2025.7-2025.11+:

| Issue | Problem | Solution | File |
|-------|---------|----------|------|
| **Config Import** (2025.11) | Deprecated `Config` import from `homeassistant.core` | Changed to `homeassistant.core_config.Config` | `__init__.py` |
| **register_static_path** (2025.7+) | Synchronous API blocks event loop | Changed to `async_register_static_paths()` with `StaticPathConfig` | `__init__.py` |
| **Z-Wave JS DATA_CLIENT** (2025.8+) | Deprecated dictionary access pattern | Changed to `getattr(zwave_data, "_client_driver_map", {})` and `client_entry.client` | `providers/zwave_js.py` |

**Result:** Full compatibility with HA Core 2025.7+, all deprecation warnings eliminated.

### Python 3.13 Upgrade and Home Assistant 2025.10 Compatibility (2025-10-05)

**Problem:** Test failures with outdated dependencies (Python 3.12, pytest-homeassistant 85 versions behind).

**Solution:** Upgraded to Python 3.13, pytest-homeassistant 0.13.286, zeroconf 0.147.2, zwave-js-server-python 0.67.1.

**API Compatibility Fixes:**

| API Change | Before | After | Files |
|------------|--------|-------|-------|
| pytest-asyncio 1.2.0+ | `def aiohttp_client(event_loop, ...)` | `def aiohttp_client(...)` | `tests/conftest.py` |
| Config Entry Setup | `async_forward_entry_setup(entry, platform)` | `async_forward_entry_setups(entry, [platform])` | `__init__.py`, `tests/conftest.py` |
| Entity Registry | `er.RegistryEntry("lock.test", ...)` | `entity_reg.async_get_or_create(...)` | `tests/*_provider.py` |
| Lovelace Data | `hass.data[LL_DOMAIN].get("resources")` | `hass.data[LL_DOMAIN].resources` | `__init__.py`, `tests/test_init.py` |

**Result:** All 37 tests passing, full compatibility with Python 3.13 and HA 2025.10+.

### Entity Creation Blocking Issue (2025-11-04)

**Problem:** No entities created - integration blocked waiting for Z-Wave JS connection during startup.

**Root Cause:** Infinite `while` loop blocked `async_update_listener()` waiting for lock connection, preventing dispatcher signals for entity creation.

**Solution:**
1. Removed blocking wait loop - create entities regardless of connection state
2. Added `LockDisconnected` exception and connection checks in `async_internal_*` methods
3. Added error handling in sync logic to catch `LockDisconnected` during startup
4. Reverted defensive UI changes (PR #534, #594) that made entities optional - entities should always exist

**Key Lessons:** Fix root causes not symptoms. Making entities optional masked the bug. Entities show "unavailable" until lock connects. UI fails fast if entities truly missing.

**Files:** `__init__.py`, `providers/_base.py`, `binary_sensor.py`, `ts/types.ts`, `ts/generate-view.ts`

### Rate Limiting and Network Flooding Prevention (2025-11-15)

**Problem:** Integration flooded Z-Wave network with rapid operations during startup (10 slots = 20 operations in <5 seconds), causing communication failures and battery drain.

**Root Cause:** No serialization, no rate limiting, excessive coordinator refreshes after each operation.

**Solution:** Decorator-based rate limiting system at `BaseLock` level using `@rate_limited_operation`:
- Enforces 2-second minimum delay between ANY operations (`time.monotonic()`)
- Single `_aio_lock` serializes all operations (get, set, clear, refresh)
- Connection checking before write operations (raises `LockDisconnected`)
- Type-safe with `Concatenate` and `ParamSpec` (passes mypy)

**Impact:** 10 out-of-sync slots: Before 20 ops in ~5s → After 20 ops in ~40s. Network flooding prevented ✅, battery drain minimized ✅.

**Files:** `providers/_base.py` (decorator + fields), `binary_sensor.py` (kept refresh, changed logs to DEBUG)
**Tests:** 5 new tests in `tests/_base/test_provider.py` - all 37 tests passing, ~95% coverage

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

#### 4c. Move Sync Logic to Coordinator

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
