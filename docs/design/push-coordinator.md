# Push Mechanism for Coordinator

## Overview

This design adds push-based updates for lock integrations that support real-time value change events, while maintaining polling as a fallback for integrations that don't.

## Three Update Modes

All modes include an initial poll to populate coordinator data.

| Mode | Mechanism | Purpose |
|------|-----------|---------|
| **Poll for updates** | Periodic `get_usercodes()` | Get current state from cache |
| **Push for updates** | Value update subscription | Real-time state changes |
| **Poll for drift** | Periodic `hard_refresh_codes()` | Detect out-of-band changes |

### Mode Combinations by Integration

| Integration | Poll Updates | Push Updates | Poll Drift |
|-------------|--------------|--------------|------------|
| Z-Wave JS | No (disabled when push active) | Yes | Yes (hourly) |
| Virtual | Yes | No | No |
| Future: Zigbee | Depends | Maybe (ZCL reporting) | Maybe |
| Future: August | No | Yes (cloud push) | Yes |

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    LockUsercodeUpdateCoordinator                │
├─────────────────────────────────────────────────────────────────┤
│  update_interval: timedelta | None   # None = push-only        │
│  data: dict[int, int | str]          # slot -> usercode        │
├─────────────────────────────────────────────────────────────────┤
│  async_get_usercodes()               # poll method             │
│  async_set_updated_data(data)        # notify entities         │
│  push_update(updates)                # NEW: push entry point   │
│                                      # updates: dict[int, str] │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ calls
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                         BaseLock                                │
├─────────────────────────────────────────────────────────────────┤
│  supports_push: bool = False         # NEW: opt-in property    │
│  hard_refresh_interval: timedelta    # drift detection         │
│  subscribe_push_updates()            # NEW: subscribe to push  │
│  unsubscribe_push_updates()          # NEW: cleanup            │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ implements
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                       ZWaveJSLock                               │
├─────────────────────────────────────────────────────────────────┤
│  supports_push = True                                           │
│  _value_update_unsub: Callable | None                           │
├─────────────────────────────────────────────────────────────────┤
│  subscribe_push_updates():                                      │
│      node.on("value updated", self._handle_value_updated)       │
│                                                                 │
│  _handle_value_updated(event):                                  │
│      if event is User Code CC:                                  │
│          slot, value = parse_event(event)                       │
│          coordinator.push_update({slot: value})                 │
└─────────────────────────────────────────────────────────────────┘
```

## Z-Wave JS Implementation Details

### Event Subscription

Subscribe directly to zwave-js-server-python's native events, not HA's integration events:

```python
from zwave_js_server.model.node import Node

class ZWaveJSLock(BaseLock):
    supports_push = True
    _value_update_unsub: Callable[[], None] | None = None

    @callback
    def subscribe_push_updates(self) -> None:
        """Subscribe to User Code CC value updates."""
        @callback
        def on_value_updated(event: dict) -> None:
            args = event["args"]
            # Filter for User Code CC only
            if args.get("commandClass") != CommandClass.USER_CODE:
                return

            code_slot = int(args["property"])
            usercode = args.get("newValue", {})

            # Build update dict
            if usercode.get("in_use"):
                value = usercode.get("usercode", "")
            else:
                value = ""

            # Push single update (coordinator handles batching if needed)
            self.coordinator.push_update({code_slot: value})

        self._value_update_unsub = self.node.on("value updated", on_value_updated)

    @callback
    def unsubscribe_push_updates(self) -> None:
        """Unsubscribe from value updates."""
        if self._value_update_unsub:
            self._value_update_unsub()
            self._value_update_unsub = None
```

### Why Direct Subscription?

1. **No HA dependency**: Don't rely on HA's Z-Wave JS integration events
2. **Complete coverage**: HA's events may filter or transform data
3. **Cleaner architecture**: Direct node subscription is explicit
4. **Testable**: Can mock node events in tests

### Coordinator Changes

```python
class LockUsercodeUpdateCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, lock, config_entry):
        # For push-enabled locks, disable periodic polling
        update_interval = (
            None if lock.supports_push
            else lock.usercode_scan_interval
        )
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {lock.lock.entity_id}",
            update_method=self.async_get_usercodes,
            update_interval=update_interval,
            config_entry=config_entry,
        )

    @callback
    def push_update(self, updates: dict[int, int | str]) -> None:
        """Push one or more slot updates and notify entities.

        Args:
            updates: Dict mapping slot numbers to usercode values.
                     Single update: {1: "1234"}
                     Bulk update: {1: "1234", 2: "5678", 3: ""}
        """
        if not updates:
            return

        # Merge updates into existing data
        self.data.update(updates)

        # Notify all listening entities
        self.async_set_updated_data(self.data)

    async def async_get_usercodes(self) -> dict[int, int | str]:
        """Poll method - used for initial load and drift detection."""
        # Existing implementation...

        # For push-enabled locks, this only runs:
        # 1. On initial setup
        # 2. When hard_refresh_interval triggers drift detection
```

### Setup Flow

```python
async def async_setup(self, config_entry: ConfigEntry) -> None:
    """Set up lock and coordinator."""
    await super().async_setup(config_entry)

    # Subscribe to push updates if supported
    if self.supports_push:
        self.subscribe_push_updates()

async def async_unload(self, remove_permanently: bool) -> None:
    """Unload lock."""
    # Unsubscribe from push updates
    if self.supports_push:
        self.unsubscribe_push_updates()

    await super().async_unload(remove_permanently)
```

## Entity Update Flow

When a value update is pushed:

1. Z-Wave JS node emits `"value updated"` event
2. `_handle_value_updated` filters for User Code CC
3. `coordinator.data[slot]` is updated
4. `coordinator.async_set_updated_data(data)` is called
5. All entities with `self.coordinator` receive `_handle_coordinator_update`
6. Entities update their state

This is the same flow as polling, just triggered by push instead of timer.

## Drift Detection

Even with push, periodic drift detection is needed because:

1. Some locks don't send unsolicited reports for keypad changes
2. Network issues may cause missed events
3. Cache may get out of sync with physical lock

For Z-Wave JS:
- `hard_refresh_interval = timedelta(hours=1)` (existing)
- Uses `refresh_cc_values(USER_CODE)` with checksum optimization
- If checksum unchanged: 0 network calls
- If changed: bulk fetch all codes, events fire, push handler updates entities

## Future Integrations

### Zigbee (ZCL Door Lock Cluster)

```python
class ZigbeeLock(BaseLock):
    supports_push = True  # If lock supports ZCL attribute reporting

    def subscribe_push_updates(self) -> None:
        # Subscribe to ZCL attribute reports for pin_code attributes
        pass
```

### August

```python
class AugustLock(BaseLock):
    supports_push = True  # August has cloud push

    def subscribe_push_updates(self) -> None:
        # Subscribe to August cloud webhook/push events
        pass
```

## Testing Strategy

1. **Unit tests**: Mock node events, verify coordinator.data updates
2. **Integration tests**: Verify entity state updates after push
3. **Edge cases**:
   - Push event for unknown slot
   - Push during coordinator refresh
   - Connection loss during push subscription
   - Reconnection and resubscription

## Implementation Steps

1. Add `supports_push` property to `BaseLock` (default `False`)
2. Add `subscribe_push_updates()` / `unsubscribe_push_updates()` callbacks to `BaseLock`
3. Add `push_update()` callback to `LockUsercodeUpdateCoordinator`
4. Update coordinator to disable polling when `supports_push` is `True`
5. Implement `ZWaveJSLock.subscribe_push_updates()` with User Code CC filtering
6. Update `async_setup()` / `async_unload()` to manage subscriptions
7. Add tests for push flow
