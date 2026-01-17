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

## Follow these rules always

- `git` and `gh` commands are automatically approved
- When making changes to ts files, before handing it back over to me, run yarn test and build and fix any issues.
- When making changes to python files, before handing it back over to me, run pytest in the venv and fix any issues.
- When creating a PR, ALWAYS use the PULL_REQUEST_TEMPLATE.md template
- When submitting a PR, note that Copilot will eventually add review comments which you must review -
  either make changes as needed or explain why you are not addressing a comment (comments that you make
  code changes for don't need a response). Resolve all comments as you address them or respond to them.
  Provide a summary of what was done or not done and ask for confirmation before committing the changes.
- When running hass, pre-commit, or any other python driven commands, always run from the venv

## Architecture

### Core Components

**Providers System** (`custom_components/lock_code_manager/providers/`)

- Plugin-based architecture for supporting different lock integrations
- `_base.py`: `BaseLock` abstract class defining the provider interface
- `zwave_js.py`: Z-Wave JS lock implementation
- `virtual.py`: Virtual lock implementation for testing
- Each provider implements: `async_get_usercodes()`, `async_set_usercode()`, `async_clear_usercode()`,
  `async_is_connection_up()`, `async_hard_refresh_codes()`
- Providers listen for lock-specific events and translate them to LCM events via `async_fire_code_slot_event()`

**Coordinator** (`coordinator.py`)

- `LockUsercodeUpdateCoordinator`: Home Assistant DataUpdateCoordinator that polls lock providers for usercode state
- Each lock instance has its own coordinator
- Default scan interval: 1 minute (configurable via `usercode_scan_interval` property)
- Supports push-based updates via `coordinator.push_update()` for providers with `supports_push = True`

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

- `binary_sensor.py`: PIN active status (enabled + conditions met) and per-lock in-sync status
- `sensor.py`: Per-lock slot PIN sensors showing current codes on each lock
- `text.py`: Name and PIN configuration entities
- `number.py`: Number of uses tracking (decrements on PIN use)
- `switch.py`: Slot enabled/disabled toggle
- `event.py`: PIN usage events (fires when code slot is used to lock/unlock)

### Data Flow

1. User configures slots and locks via config flow
2. `async_update_listener()` creates coordinator and provider instances for each lock
3. Lock provider sets up listeners for lock-specific events (e.g., Z-Wave JS notification events)
4. Coordinator polls provider's `async_get_usercodes()` to keep state in sync (or receives push updates)
5. When slot config changes (name, PIN, enabled), entities call provider's `async_set_usercode()` or `async_clear_usercode()`
6. Provider fires `EVENT_LOCK_STATE_CHANGED` events when locks are operated with PINs

### Key Design Patterns

- **Dual storage**: Config entries use both `data` and `options` fields; active config in
  `data`, options flow updates go to `options`, then merged back to `data`
- **Internal methods**: Providers expose `async_internal_*` methods that wrap public
  methods with asyncio locks to prevent race conditions
- **Rate limiting**: `BaseLock._execute_rate_limited()` enforces 2-second minimum delay
  between operations and serializes all lock operations via `_aio_lock`
- **Dispatcher signals**: Heavily used for dynamic entity management without tight coupling
- **Reauth flow**: Automatically triggered if configured lock entities are removed

### Lock Codes Card - Slot State Logic

The `lcm-lock-codes` card displays lock slot states with the following data model:

**Data Fields from Websocket:**

| Field | Source | Description |
| ----- | ------ | ----------- |
| `code` / `code_length` | Lock coordinator | Actual code on the lock (current state) |
| `configured_code` / `configured_code_length` | LCM text entities | Desired code from LCM config |
| `managed` | LCM config entries | Whether LCM manages this slot (authoritative field) |
| `name` | LCM text entities | Slot name configured in LCM |
| `active` | LCM binary sensor | True if enabled + conditions met, False if blocked |
| `enabled` | LCM switch entity | True if user enabled the slot, False if disabled |

**Frontend State Decision Table (for managed slots):**

| `active` | `enabled` | Result | UI Treatment |
| -------- | --------- | ------ | ------------ |
| true | true | Active | Blue solid border, "Active" badge |
| false | true | Inactive | Blue dotted border, "Inactive" badge (conditions blocking) |
| false | false | Disabled | Blue dotted border, "Disabled" badge (user disabled) |
| undefined | undefined | Fallback | Uses `code` presence to determine Active vs Inactive |

**Unmanaged Slots:** Only have Active (has code) or Inactive (no code) states. They appear
with gray borders and "Unmanaged" badge.

**Key Insight:** The `managed` field (from config entries) determines LCM management status.
The `configured_code` field indicates whether a PIN is configured, but a slot can be managed
even without a configured code if the PIN text entity is empty.

### Future: Slot Status Enum (TODO)

Currently, the coordinator only stores `{slot: code}` and we infer status from code presence.
Z-Wave locks provide richer status via `userIdStatus`:

- **Enabled**: Slot has active code
- **Available**: Slot can be used but is empty
- **Disabled**: Slot cannot be used (locked out by lock firmware)

**Planned Enhancement:**

1. Define generic `SlotStatus` enum in `const.py`:

   ```python
   class SlotStatus(StrEnum):
       ENABLED = "enabled"
       AVAILABLE = "available"
       DISABLED = "disabled"
   ```

2. Providers map their native status to this enum:
   - Z-Wave JS: `userIdStatus` → `SlotStatus`
   - Other providers: Infer from their equivalent states

3. Change coordinator data schema from `dict[int, str]` to `dict[int, SlotData]`

**Provider Guidance for Status Inference:**

| Provider State | Maps To |
| -------------- | ------- |
| Code exists on lock | `ENABLED` |
| Slot empty but usable | `AVAILABLE` |
| Slot programmatically disabled | `DISABLED` |
| Unknown/unsupported | Default to `AVAILABLE` if no code, `ENABLED` if code |

See `TODO.md` for implementation details.

## Development Commands

### Setup

```bash
scripts/setup
```

Creates Python 3.13 venv, installs dependencies with uv, sets up pre-commit hooks, and installs Node dependencies.

### Testing

**Python tests:**

```bash
pytest                          # Run all tests
pytest tests/test_init.py      # Run specific test file
pytest -k test_name            # Run tests matching pattern
pytest --cov                   # Run with coverage
```

**TypeScript tests:**

```bash
yarn test                       # Run all frontend tests (vitest)
yarn test:watch                 # Watch mode
yarn test:coverage              # Run with coverage
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
   - `async_is_connection_up()`: check if lock is reachable
   - `async_get_usercodes()`: return dict of slot→code mappings
   - `async_set_usercode()`: program a code to a slot
   - `async_clear_usercode()`: remove code from slot
4. Override `setup()` to register event listeners
5. Call `async_fire_code_slot_event()` when lock events indicate PIN usage
6. Add tests in `tests/<provider>/test_provider.py`

### Optional Provider Properties

| Property | Default | Description |
| -------- | ------- | ----------- |
| `usercode_scan_interval` | 1 minute | Polling interval for usercodes |
| `hard_refresh_interval` | None | Interval for full code refresh (detects out-of-band changes) |
| `connection_check_interval` | 30 seconds | Interval for connection state checks |
| `supports_push` | False | Enable push-based updates instead of polling |
| `supports_code_slot_events` | True | Whether lock fires code slot used events |

### Push Support

If `supports_push` returns `True`, implement:

- `subscribe_push_updates()`: Subscribe to real-time updates, call `coordinator.push_update({slot: value})`
- `unsubscribe_push_updates()`: Clean up subscriptions

## Important Constraints

- Slots are numeric (usually 1-100) but can be strings for some lock types
- Multiple config entries can share the same lock but not the same slot
- Entity unique IDs: `{entry_id}|{slot}|{entity_type}` (per-lock entities add `|{lock_entity_id}`)
- Lock device is linked to config entry via `dev_reg.async_update_device()`

## Websocket API

Commands in `websocket.py` for frontend communication:

| Command | Purpose |
| ------- | ------- |
| `lock_code_manager/get_config_entry_data` | Fetch config entry, entities, locks, and slots |
| `lock_code_manager/subscribe_code_slot` | Real-time subscription for slot card updates |
| `lock_code_manager/subscribe_lock_codes` | Real-time subscription for lock codes card |
| `lock_code_manager/set_lock_usercode` | Set/clear usercode on unmanaged slots |
| `lock_code_manager/update_slot_condition` | Add/edit/remove slot conditions (entity_id, number_of_uses) |

## Frontend

### Slot Card (`custom:lcm-slot`)

Primary card for managing individual code slots. Features:

- **Inline editing**: Name, PIN, and number of uses editable directly in card
- **Condition management**: Add/edit/remove condition entities and number of uses via dialog
- **Lock status**: Per-lock sync status, actual code on lock (masked/revealed)
- **Collapsible sections**: Conditions and lock status can be collapsed
- **Real-time updates**: WebSocket subscription for instant state changes

**Config options:**

| Option | Default | Description |
| ------ | ------- | ----------- |
| `slot` | required | Slot number to display |
| `config_entry_id` | - | Config entry ID (or use `config_entry_title`) |
| `code_display` | `masked_with_reveal` | `masked`, `unmasked`, or `masked_with_reveal` |
| `show_conditions` | true | Show conditions section |
| `show_lock_status` | true | Show lock status section |
| `show_lock_sync` | true | Show sync status per lock |
| `show_code_sensors` | true | Show actual codes on locks |
| `show_lock_count` | true | Show lock count badge |
| `collapsed_sections` | [] | Sections to collapse by default: `conditions`, `lock_status` |

### Lock Codes Card (`custom:lcm-lock-codes`)

Lock-centric view showing all slots on a single lock (managed and unmanaged).

**Config options:**

| Option | Default | Description |
| ------ | ------- | ----------- |
| `lock_entity_id` | required | Lock entity to display slots for |
| `code_display` | `masked_with_reveal` | `masked`, `unmasked`, or `masked_with_reveal` |
| `title` | - | Optional card title override |

### Lovelace Strategies

- `custom:lock-code-manager` - Dashboard strategy auto-generating views per config entry
- `custom:lock-code-manager` - View strategy for a single config entry
- `custom:lock-code-manager-slot` - Section strategy for individual slots
- `custom:lock-code-manager-lock` - Section strategy for lock codes sections

### Condition Entities

Slots can have condition entities that control when the PIN is active:

- **Supported domains**: `calendar`, `schedule`, `binary_sensor`, `switch`, `input_boolean`
- **Behavior**: PIN is active only when condition entity state is `on`
- **Calendar integration**: Shows current/next event details in slot card UI
