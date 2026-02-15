# User Code CC V1 Support

## Problem

The Z-Wave JS provider is optimized for User Code Command Class V2. Locks using
V1 experience sync loops because:

1. V1 nodes lack Supervision CC, so the Z-Wave JS driver always schedules
   verification polls after set/clear (no optimistic cache update).
2. `async_internal_set_usercode` calls `coordinator.async_request_refresh()`
   after the optimistic `push_update()`, reading stale cache and overwriting the
   correct optimistic data.
3. V1 hard refresh queries every slot individually (no checksum optimization),
   flooding the push handler with stale value-updated events.
4. V1 nodes are not required to send unsolicited User Code Reports via Lifeline,
   making push-based updates unreliable for detecting external changes.

## Changes

### 1. Remove `async_request_refresh()` after set/clear

**File:** `providers/_base.py`

Remove the `coordinator.async_request_refresh()` calls in
`async_internal_set_usercode` and `async_internal_clear_usercode`. These defeat
optimistic `push_update()` by reading potentially stale cache. The coordinator
already receives correct data via:

- Optimistic `push_update()` (push providers)
- Next poll cycle (poll providers)

This change benefits all providers, not just V1.

### 2. Detect CC version and branch behavior

**File:** `providers/zwave_js.py`

Add a `_usercode_cc_version` cached property that reads the User Code CC version
from the node's command class list.

Branch key properties based on version:

| Property | V1 | V2 |
| --- | --- | --- |
| `supports_push` | `False` | `True` |
| `usercode_scan_interval` | 2 minutes | N/A (push) |
| `hard_refresh_interval` | 30 minutes | 1 hour |
| `connection_check_interval` | 30 seconds | `None` (config entry state) |

V1 nodes still subscribe to Notification CC events (lock/unlock with code slot)
since those use a different command class than User Code CC.

### 3. Suppress push handler events during hard refresh

**File:** `providers/zwave_js.py`

Add a `_hard_refresh_in_progress` flag. Set it before
`async_refresh_cc_values`, clear after. In `on_value_updated`, skip processing
when the flag is set. This prevents stale value-updated events from V1's
one-by-one slot re-query during drift detection.

This also benefits V2 since intermediate events during cache rebuild are noise.

### 4. TODOs for future work

- Make `hard_refresh_interval` a user-configurable option.
- Split hard refresh between managed and unmanaged slots. Managed slots (those
  with LCM config) get refreshed at the normal interval (e.g., 30 min).
  Unmanaged slots only need occasional drift awareness (e.g., once a day).
