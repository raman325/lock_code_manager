# Provider State Management

This guide explains how Lock Code Manager providers manage usercode state, including update
modes, the coordinator lifecycle, and best practices.

## Overview

The `LockUsercodeUpdateCoordinator` manages usercode state through three update modes:

| Mode | Mechanism | When to Use |
| ---- | --------- | ----------- |
| **Poll for updates** | Periodic `get_usercodes()` | Default for most integrations |
| **Push for updates** | Real-time subscription | Integrations with event support |
| **Poll for drift** | Periodic `hard_refresh_codes()` | Detect out-of-band changes |

All modes include an initial poll to populate coordinator data.

> **Important:** Even with push mode enabled, you must implement `get_usercodes()`.
> The coordinator always calls it for the initial data load and any manual refresh requests.

## Architecture

```text
┌─────────────────────────────────────────────────────────────────┐
│                    LockUsercodeUpdateCoordinator                │
├─────────────────────────────────────────────────────────────────┤
│  data: dict[int, int | str]          # slot -> usercode        │
├─────────────────────────────────────────────────────────────────┤
│  async_get_usercodes()               # poll method             │
│  push_update(updates)                # push entry point        │
│  _async_drift_check()                # hard refresh timer      │
│  _async_connection_check()           # connection poll timer   │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ calls
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                         BaseLock                                │
├─────────────────────────────────────────────────────────────────┤
│  Properties:                                                    │
│    supports_push: bool              # opt-in for push mode      │
│    usercode_scan_interval: timedelta   # polling interval       │
│    hard_refresh_interval: timedelta    # drift detection        │
│    connection_check_interval: timedelta  # connection polling   │
├─────────────────────────────────────────────────────────────────┤
│  Required Methods:                                              │
│    get_usercodes()                  # return current codes      │
│    set_usercode()                   # set a code on lock        │
│    clear_usercode()                 # clear a code from lock    │
│    is_connection_up()               # check lock connectivity   │
├─────────────────────────────────────────────────────────────────┤
│  Optional Methods:                                              │
│    hard_refresh_codes()             # re-fetch from device      │
│    subscribe_push_updates()         # set up event listeners    │
│    unsubscribe_push_updates()       # clean up listeners        │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ implements
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      YourLockProvider                           │
└─────────────────────────────────────────────────────────────────┘
```

## Update Modes

### Poll Mode (Default)

The coordinator periodically calls `get_usercodes()` at the interval specified by `usercode_scan_interval`.

**Flow:**

1. Timer fires at `usercode_scan_interval`
2. Coordinator calls `async_internal_get_usercodes()`
3. Base class applies rate limiting, then calls `async_get_usercodes()`
4. Provider returns current usercode state
5. Coordinator updates `data` and notifies listening entities

**When to use:**

- Integrations without real-time event support
- Simple implementations where polling is sufficient

**Example configuration:**

```python
class MyLock(BaseLock):
    @property
    def usercode_scan_interval(self) -> timedelta:
        return timedelta(minutes=1)  # Poll every minute

    @property
    def hard_refresh_interval(self) -> timedelta | None:
        return None  # No drift detection needed
```

### Push Mode

For integrations that support real-time events (e.g., Z-Wave JS value updates), push mode
provides immediate updates without polling overhead.

**Flow:**

1. Provider subscribes to device events in `subscribe_push_updates()`
2. Device event fires (e.g., code changed)
3. Provider's event handler calls `coordinator.push_update({slot: value})`
4. Coordinator merges update into `data`
5. Coordinator notifies listening entities immediately

**When to use:**

- Integrations with real-time event support
- When you want immediate UI updates after code changes

**Example configuration:**

```python
class MyLock(BaseLock):
    @property
    def supports_push(self) -> bool:
        return True  # Disable polling, use push instead

    @property
    def hard_refresh_interval(self) -> timedelta | None:
        return timedelta(hours=1)  # Still check for drift
```

### Drift Detection

Drift detection catches out-of-band changes (e.g., codes changed at the lock's keypad, or via another integration).

**Flow:**

1. Timer fires at `hard_refresh_interval`
2. Coordinator calls `async_internal_hard_refresh_codes()`
3. Provider queries the device directly, bypassing cache
4. If data differs from current state, coordinator updates and notifies entities

**When to use:**

- Codes can be changed outside Home Assistant
- Integration caches data that may become stale
- You want to catch sync issues even with push mode

## Interval Properties

| Property | Default | Purpose |
| -------- | ------- | ------- |
| `usercode_scan_interval` | 1 minute | How often to poll for usercode updates (ignored if `supports_push=True`) |
| `hard_refresh_interval` | `None` | How often to hard refresh for drift detection (`None` = disabled) |
| `connection_check_interval` | 30 seconds | How often to check connection state (`None` = disabled) |

## Configuration Examples

### Poll-Only (Simple Implementation)

```python
class MyLock(BaseLock):
    @property
    def usercode_scan_interval(self) -> timedelta:
        return timedelta(minutes=1)

    @property
    def hard_refresh_interval(self) -> timedelta | None:
        return None  # No drift detection
```

### Push with Drift Detection (Recommended for Z-Wave)

```python
class MyLock(BaseLock):
    @property
    def supports_push(self) -> bool:
        return True

    @property
    def hard_refresh_interval(self) -> timedelta | None:
        return timedelta(hours=1)  # Periodic drift check

    @property
    def connection_check_interval(self) -> timedelta | None:
        return None  # Z-Wave JS provides config entry state changes
```

### Poll with Drift Detection

```python
class MyLock(BaseLock):
    @property
    def usercode_scan_interval(self) -> timedelta:
        return timedelta(minutes=1)

    @property
    def hard_refresh_interval(self) -> timedelta | None:
        return timedelta(hours=1)
```

## Implementing Required Methods

### get_usercodes()

Return current usercode state. This may read from a cache or query the device.

```python
def get_usercodes(self) -> dict[int, int | str]:
    """Return dictionary mapping slot numbers to usercodes.

    Returns:
        Dict with slot number as key, usercode as value.
        Use empty string "" for cleared/unused slots.

    Raises:
        LockDisconnected: If lock cannot be communicated with.
    """
    return {
        1: "1234",   # Slot 1 has code
        2: "",       # Slot 2 is empty
        3: "5678",   # Slot 3 has code
    }
```

**Important:** The return value format is `dict[int, int | str]`:

- **Keys** are slot numbers (integers)
- **Values** are usercodes (strings or integers), or empty string `""` for cleared slots

### set_usercode()

Set a usercode on a specific slot.

```python
def set_usercode(
    self, code_slot: int, usercode: int | str, name: str | None = None
) -> bool:
    """Set a usercode on a code slot.

    Args:
        code_slot: The slot number to set.
        usercode: The PIN code to set.
        name: Optional name for the slot (some locks support this).

    Returns:
        True if the value was changed, False if already set to this value.
        If you can't determine whether a change occurred, return True.

    Raises:
        LockDisconnected: If the lock cannot be communicated with.
    """
    # Check if already set to this value (optional optimization)
    if self._cache.get(code_slot) == str(usercode):
        return False

    # Set the code on the device
    self._device.set_code(code_slot, usercode)
    return True
```

### clear_usercode()

Clear a usercode from a specific slot.

```python
def clear_usercode(self, code_slot: int) -> bool:
    """Clear a usercode from a code slot.

    Args:
        code_slot: The slot number to clear.

    Returns:
        True if the value was changed, False if already cleared.
        If you can't determine whether a change occurred, return True.

    Raises:
        LockDisconnected: If the lock cannot be communicated with.
    """
    if code_slot not in self._cache or self._cache[code_slot] == "":
        return False

    self._device.clear_code(code_slot)
    return True
```

### is_connection_up()

Check if the lock is reachable.

```python
def is_connection_up(self) -> bool:
    """Return whether connection to lock is up.

    This is called periodically (at connection_check_interval) and
    before each operation. Return False if the lock is unreachable.
    """
    return self._device.is_connected()
```

### hard_refresh_codes()

Re-fetch codes directly from the device, bypassing any cache. Required if `hard_refresh_interval` is set.

```python
def hard_refresh_codes(self) -> dict[int, int | str]:
    """Force refresh from device and return all codes.

    This should bypass any caching layer and query the
    physical device directly.

    Returns:
        Dict with slot number as key, usercode as value.

    Raises:
        LockDisconnected: If lock cannot be communicated with.
    """
    self._cache = self._device.fetch_all_codes()
    return self.get_usercodes()
```

## Implementing Push Updates

### 1. Enable Push Mode

```python
@property
def supports_push(self) -> bool:
    return True
```

### 2. Subscribe to Events

```python
@callback
def subscribe_push_updates(self) -> None:
    """Subscribe to real-time value updates.

    Must be idempotent - no-op if already subscribed.
    """
    # Skip if already subscribed
    if self._unsub is not None:
        return

    @callback
    def on_code_changed(slot: int, usercode: str | None) -> None:
        # Convert to coordinator format (empty string for cleared)
        value = usercode if usercode else ""

        # Push update to coordinator
        if self.coordinator:
            self.coordinator.push_update({slot: value})

    # Store unsubscribe function for cleanup
    self._unsub = self.device.subscribe_to_code_events(on_code_changed)
```

### 3. Clean Up on Unload

```python
@callback
def unsubscribe_push_updates(self) -> None:
    """Unsubscribe from value updates.

    Must be idempotent - no-op if already unsubscribed.
    """
    if self._unsub:
        self._unsub()
        self._unsub = None
```

## Exception Handling

Providers must raise `LockCodeManagerError` subclasses for lock communication failures:

```python
from ..exceptions import LockDisconnected

def get_usercodes(self) -> dict[int, int | str]:
    try:
        return self._fetch_codes()
    except SomeDeviceError as err:
        raise LockDisconnected("Cannot communicate with lock") from err
```

**Available exceptions:**

| Exception              | When to Use                                 |
| ---------------------- | ------------------------------------------- |
| `LockDisconnected`     | Lock is unreachable or communication failed |
| `LockCodeManagerError` | Base class for other LCM errors             |

The coordinator catches `LockCodeManagerError` and handles retry logic. Do NOT raise generic
exceptions or `HomeAssistantError` directly.

## Rate Limiting

The base class provides automatic rate limiting through `_execute_rate_limited()`. Use the
`async_internal_*` methods which apply rate limiting:

- `async_internal_get_usercodes()` - rate-limited get
- `async_internal_set_usercode()` - rate-limited set + refresh
- `async_internal_clear_usercode()` - rate-limited clear + refresh
- `async_internal_hard_refresh_codes()` - rate-limited hard refresh

The default delay between operations is 2 seconds (`MIN_OPERATION_DELAY`).

## Connection State Management

The base class handles connection state transitions:

1. **Reconnection detection**: When `is_connection_up()` transitions from `False` to `True`:
   - Coordinator refresh is triggered
   - Push subscriptions are re-established (if `supports_push=True`)

2. **Disconnection handling**: When `is_connection_up()` transitions from `True` to `False`:
   - Push subscriptions are cleaned up (if `supports_push=True`)

3. **Config entry state changes**: For integrations that expose config entry state (like Z-Wave JS):
   - The base class listens for state changes
   - Automatically resubscribes when the integration reloads

## Best Practices

1. **Prefer push mode** when your integration supports events - it's more responsive and
   reduces device traffic.

2. **Use drift detection** if codes can be changed outside Home Assistant (e.g., at the lock's keypad).

3. **Cache appropriately** - `get_usercodes()` can return cached data, but
   `hard_refresh_codes()` should always query the device.

4. **Handle disconnections gracefully** - raise `LockDisconnected` rather than letting exceptions bubble up.

5. **Clean up subscriptions** - always implement `unsubscribe_push_updates()` to prevent
   memory leaks.

6. **Make subscriptions idempotent** - `subscribe_push_updates()` and
   `unsubscribe_push_updates()` may be called multiple times.

7. **Return change indicators** - `set_usercode()` and `clear_usercode()` should return
   `False` if no change was made to avoid unnecessary refreshes.

8. **Use the internal methods** - Call `async_internal_*` methods which provide rate
   limiting, connection checks, and automatic coordinator refresh.
