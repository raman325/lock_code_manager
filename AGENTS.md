# AGENTS.md

This file provides guidance to all coding agents (e.g., GPT-4/4o/5 via Codex
CLI, Opus via Claude Code CLI, etc.) when working with code in this repository.
`CLAUDE.md` points here so that Claude shares a consistent view of the project
with other coding agents.

## Overview

Lock Code Manager is a Home Assistant custom integration that allows users to manage lock
usercodes across multiple locks. It reduces entity/automation sprawl compared to
alternatives by handling logic internally rather than generating numerous Home Assistant
entities.

## Codex Agent Context (2025-02 Session)

- `BaseLock` now performs its own rate limiting + connection checks without Tenacity.
  Tenacity was removed from all dependency lists (manifest + requirements_*), and
  operations fail fast with `LockDisconnected` if the lock isn't connected—don't
  reintroduce Tenacity unless absolutely necessary.
- We added a lightweight retry scheduler in `LockCodeManagerCodeSlotInSyncEntity`: when a
  sync fails because the lock is offline, the entity schedules its own retry instead of
  blocking HA. Expect to see `_retry_unsub` state on those entities during tests.
- The in-sync entity now waits for dependent entity states/availability before acting,
  ignores irrelevant/unavailable events, and schedules a 10s retry on `LockDisconnected`.
  New coverage: `test_in_sync_waits_for_missing_pin_state`, `test_entities_track_availability`,
  the reconnect paths in `test_handles_disconnected_lock_on_set/clear`, and
  `test_startup_out_of_sync_slots_sync_once` (verifies we sync each slot once without
  extra calls).
- Provider connection failures no longer advance rate-limit timing; see
  `test_connection_failure_does_not_rate_limit_next_operation` for regression coverage.
- Lovelace strategy resource: only needs to be registered once globally; if HA is in YAML
  mode we skip removal on unload (mirrors ha_scrypted handling). New test
  `test_resource_unload_skips_yaml_mode` covers the YAML guard.
- Claude and Codex collaborated on the startup flapping fix (see "Startup Code Flapping
  Issue" below). Connection handling is no longer regressed after this session, but keep
  an eye on `tests/_base/test_provider.py::test_*disconnected` and
  `tests/test_binary_sensor.py::test_handles_disconnected_lock_on_*` whenever touching
  provider logic.
- A focused test command that confirms the connection/retry behaviour is
  `source venv/bin/activate && pytest tests/_base/test_provider.py -k disconnected -q`.
- This session also added the `AGENTS.md` mirror of `CLAUDE.md` so future coding agents
  (e.g., GPT-based) can track their own context along with Claude's notes. Keep both
  files updated when architecture/process guidance changes.

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

### Entities

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

- **Dual storage**: Config entries use both `data` and `options` fields; active config in
  `data`, options flow updates go to `options`, then merged back to `data`
- **Internal methods**: Providers expose `async_internal_*` methods that wrap public
  methods with asyncio locks to prevent race conditions
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

**Problem:** Integration flapped between clearing/setting codes on startup when
codes were already synced, causing battery drain.

**Root Cause:** Race condition - in-sync entity checked status before coordinator
had stable lock state data.

**Solution:** Added `_initial_state_loaded` flag to prevent sync operations on
first load. On initial load, entity reads all states and sets in-sync status
WITHOUT triggering operations. Normal sync executes only after initial state is
stable.

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

**Problem:** Test failures with outdated dependencies (Python 3.12,
pytest-homeassistant 85 versions behind).

**Solution:** Upgraded to Python 3.13, pytest-homeassistant 0.13.286, zeroconf
0.147.2, zwave-js-server-python 0.67.1.

**API Compatibility Fixes:**

| API Change | Before | After | Files |
|------------|--------|-------|-------|
| pytest-asyncio 1.2.0+ | `def aiohttp_client(event_loop, ...)` | `def aiohttp_client(...)` | `tests/conftest.py` |
| Config Entry Setup | `async_forward_entry_setup(entry, platform)` | `async_forward_entry_setups(entry, [platform])` | `__init__.py`, `tests/conftest.py` |
| Entity Registry | `er.RegistryEntry("lock.test", ...)` | `entity_reg.async_get_or_create(...)` | `tests/*_provider.py` |
| Lovelace Data | `hass.data[LL_DOMAIN].get("resources")` | `hass.data[LL_DOMAIN].resources` | `__init__.py`, `tests/test_init.py` |

**Result:** All 37 tests passing, full compatibility with Python 3.13 and HA 2025.10+.

### Entity Creation Blocking Issue (2025-11-04)

**Problem:** No entities created - integration blocked waiting for Z-Wave JS
connection during startup.

**Root Cause:** Infinite `while` loop blocked `async_update_listener()` waiting
for lock connection, preventing dispatcher signals for entity creation.

**Solution:**

1. Removed blocking wait loop - create entities regardless of connection state
2. Added `LockDisconnected` exception and connection checks in `async_internal_*` methods
3. Added error handling in sync logic to catch `LockDisconnected` during startup
4. Reverted defensive UI changes (PR #534, #594) that made entities optional -
   entities should always exist

**Key Lessons:** Fix root causes not symptoms. Making entities optional masked
the bug. Entities show "unavailable" until lock connects. UI fails fast if
entities truly missing.

**Files:** `__init__.py`, `providers/_base.py`, `binary_sensor.py`, `ts/types.ts`, `ts/generate-view.ts`

### Rate Limiting and Network Flooding Prevention (2025-11-15)

**Problem:** Integration flooded Z-Wave network with rapid operations during
startup (10 slots = 20 operations in <5 seconds), causing communication failures
and battery drain.

**Root Cause:** No serialization, no rate limiting, excessive coordinator refreshes after each operation.

**Solution:** Decorator-based rate limiting system at `BaseLock` level using
`@rate_limited_operation`:

- Enforces 2-second minimum delay between ANY operations (`time.monotonic()`)
- Single `_aio_lock` serializes all operations (get, set, clear, refresh)
- Connection checking before write operations (raises `LockDisconnected`)
- Type-safe with `Concatenate` and `ParamSpec` (passes mypy)

**Impact:** 10 out-of-sync slots: Before 20 ops in ~5s → After 20 ops in ~40s.
Network flooding prevented ✅, battery drain minimized ✅.

**Files:** `providers/_base.py` (decorator + fields), `binary_sensor.py` (kept refresh, changed logs to DEBUG)
**Tests:** 5 new tests in `tests/_base/test_provider.py` - all 37 tests passing, ~95% coverage
