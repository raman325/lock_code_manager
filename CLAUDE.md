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
