# Lock Code Manager Development Guide

This guide covers development topics for Lock Code Manager, including how to add support for new lock integrations.

## Architecture Overview

Lock Code Manager uses a **provider pattern** to abstract lock-specific implementations.
Each supported lock integration (Z-Wave JS, Virtual, etc.) has a corresponding provider
class that inherits from `BaseLock`.

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│                           Lock Code Manager                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│  Config Entry                                                                │
│  ├── Entities (switches, sensors, text inputs)                              │
│  └── Provider instances (one per configured lock)                           │
│       └── LockUsercodeUpdateCoordinator                                     │
│            └── Manages usercode state and entity updates                    │
└─────────────────────────────────────────────────────────────────────────────┘
                              │
                              │ inherits from
                              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                              BaseLock                                        │
├─────────────────────────────────────────────────────────────────────────────┤
│  Abstract base class defining the provider interface                        │
│  • Data fetching (poll, push, drift detection)                              │
│  • Usercode operations (get, set, clear)                                    │
│  • Connection management                                                     │
│  • Event handling                                                            │
└─────────────────────────────────────────────────────────────────────────────┘
                              │
            ┌─────────────────┼─────────────────┐
            ▼                 ▼                 ▼
     ┌───────────┐     ┌───────────┐     ┌───────────┐
     │ ZWaveJS   │     │ Virtual   │     │ YourLock  │
     │ Lock      │     │ Lock      │     │ Provider  │
     └───────────┘     └───────────┘     └───────────┘
```

## Key Components

### Provider (`BaseLock`)

The provider is responsible for:

- **Reading usercodes** from the lock device
- **Writing usercodes** to the lock device
- **Monitoring connection state** to the lock
- **Subscribing to events** for real-time updates (optional)
- **Firing events** when codes are used

See [Provider State Management](provider-state-management.md) for details on update modes.

### Coordinator (`LockUsercodeUpdateCoordinator`)

The coordinator manages the state of usercodes and notifies entities when data changes. It:

- Calls the provider's `get_usercodes()` method to fetch current state
- Receives push updates from providers that support real-time events
- Runs periodic drift detection to catch out-of-band changes
- Handles connection state and retry logic

### Entities

Lock Code Manager creates entities for each configured slot:

| Entity Type | Purpose |
| ----------- | ------- |
| `switch` | Enable/disable the code slot |
| `text` | Name for the code slot |
| `text` | PIN value for the code slot |
| `binary_sensor` | Whether the code is active (enabled + conditions met) |
| `binary_sensor` | Per-lock sync status |
| `sensor` | Current code on the lock |
| `number` | Number of uses remaining (if configured) |
| `event` | PIN usage events |

## Development Guides

### Adding a New Provider

See [Adding a Provider](adding-a-provider.md) for a step-by-step tutorial on adding support for a new lock integration.

### Provider State Management

See [Provider State Management](provider-state-management.md) for details on poll vs push
modes, drift detection, and the update lifecycle.

## File Structure

```text
custom_components/lock_code_manager/
├── providers/
│   ├── __init__.py       # Provider registry
│   ├── _base.py          # BaseLock abstract class
│   ├── const.py          # Provider constants
│   ├── virtual.py        # Virtual lock provider
│   └── zwave_js.py       # Z-Wave JS provider
├── coordinator.py        # LockUsercodeUpdateCoordinator
├── exceptions.py         # LCM-specific exceptions
└── ...
```

## Testing

Providers should include tests for:

1. **Usercode operations**: `set_usercode`, `clear_usercode`, `get_usercodes`
2. **Connection handling**: `is_connection_up`, disconnection recovery
3. **Push updates** (if supported): Event subscription and unsubscription
4. **Error handling**: Proper exception types raised

See the [Adding a Provider](adding-a-provider.md) guide for testing examples.
