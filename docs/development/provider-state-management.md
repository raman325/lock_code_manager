# Provider State Management Guide

This guide explains how to implement state management for Lock Code Manager providers. Providers are responsible for communicating with lock devices and keeping usercode state synchronized.

## Overview

The coordinator manages usercode state through three update modes:

| Mode | Mechanism | When to Use |
|------|-----------|-------------|
| **Poll for updates** | Periodic `get_usercodes()` | Default for most integrations |
| **Push for updates** | Real-time subscription | Integrations with event support |
| **Poll for drift** | Periodic `hard_refresh_codes()` | Detect out-of-band changes |

All modes include an initial poll to populate coordinator data.

> **Note:** Even with push mode enabled, you must implement `get_usercodes()`. The coordinator always calls it for the initial data load and any manual refresh requests.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    LockUsercodeUpdateCoordinator                │
├─────────────────────────────────────────────────────────────────┤
│  data: dict[int, int | str]          # slot -> usercode        │
├─────────────────────────────────────────────────────────────────┤
│  async_get_usercodes()               # poll method             │
│  push_update(updates)                # push entry point        │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ calls
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                         BaseLock                                │
├─────────────────────────────────────────────────────────────────┤
│  supports_push: bool                 # opt-in for push mode    │
│  usercode_scan_interval: timedelta   # polling interval        │
│  hard_refresh_interval: timedelta    # drift detection         │
├─────────────────────────────────────────────────────────────────┤
│  get_usercodes()                     # return current codes    │
│  hard_refresh_codes()                # re-fetch from device    │
│  subscribe_push_updates()            # set up event listeners  │
│  unsubscribe_push_updates()          # clean up listeners      │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ implements
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      YourLockProvider                           │
└─────────────────────────────────────────────────────────────────┘
```

## Configuration Options

### Poll-Only (Default)

For integrations without real-time events. The coordinator polls at regular intervals.

```python
class MyLock(BaseLock):
    @property
    def usercode_scan_interval(self) -> timedelta:
        return timedelta(minutes=1)  # Poll every minute

    @property
    def hard_refresh_interval(self) -> timedelta | None:
        return None  # No drift detection needed
```

### Push with Drift Detection

For integrations with real-time events but potential sync issues (e.g., codes changed at the physical keypad).

```python
class MyLock(BaseLock):
    @property
    def supports_push(self) -> bool:
        return True  # Disable polling, use push instead

    @property
    def hard_refresh_interval(self) -> timedelta | None:
        return timedelta(hours=1)  # Check for drift hourly
```

> **Note:** You still need to implement `get_usercodes()` - it's called for initial load and manual refreshes even when push is enabled.

### Poll with Drift Detection

For integrations that cache data and need periodic verification.

```python
class MyLock(BaseLock):
    @property
    def usercode_scan_interval(self) -> timedelta:
        return timedelta(minutes=1)

    @property
    def hard_refresh_interval(self) -> timedelta | None:
        return timedelta(hours=1)  # Hard refresh hourly
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
    self._refresh_cache_from_device()
    return self.get_usercodes()
```

## Implementing Push Updates

If your integration supports real-time events, implement push updates:

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
    """Subscribe to real-time value updates."""

    @callback
    def on_code_changed(slot: int, usercode: str | None) -> None:
        # Convert to coordinator format
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
    """Unsubscribe from value updates."""
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
        raise LockDisconnected from err
```

The coordinator catches `LockCodeManagerError` and handles retry logic. Do NOT raise generic exceptions or `HomeAssistantError` directly.

## Update Flow

### Poll Mode

1. Timer fires at `usercode_scan_interval`
2. Coordinator calls `get_usercodes()`
3. Provider returns current state
4. Coordinator updates `data` and notifies entities

### Push Mode

1. Device event fires (e.g., code changed)
2. Provider's event handler calls `coordinator.push_update({slot: value})`
3. Coordinator merges update into `data`
4. Coordinator notifies entities

### Drift Detection

1. Timer fires at `hard_refresh_interval`
2. Coordinator calls `hard_refresh_codes()`
3. Provider queries device directly
4. If data changed, coordinator notifies entities

## Best Practices

1. **Prefer push mode** when your integration supports events - it's more responsive and reduces device traffic.

2. **Use drift detection** if codes can be changed outside Home Assistant (e.g., at the lock's keypad).

3. **Cache appropriately** - `get_usercodes()` can return cached data, but `hard_refresh_codes()` should always query the device.

4. **Handle disconnections gracefully** - raise `LockDisconnected` rather than letting exceptions bubble up.

5. **Clean up subscriptions** - always implement `unsubscribe_push_updates()` to prevent memory leaks.

6. **Use rate limiting** - the base class provides rate limiting through `_execute_rate_limited()`. Use the `async_internal_*` methods which apply rate limiting automatically.
