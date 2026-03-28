# Lock Code Manager Architecture

## Overview

Lock Code Manager (LCM) manages user codes across locks via providers. Each lock
gets a coordinator that holds the full slot-to-code mapping (managed AND unmanaged).

## Data Flow

```text
Config Entry (desired state)
    |
Binary Sensor (sync decision)
    | set/clear
Provider (async_set_usercode / async_clear_usercode)
    | Z-Wave service call
Lock (firmware)
    | push notification / poll response
Coordinator (actual state: ALL slots)
    | listener notification
Binary Sensor (re-evaluate sync)
```

## Coordinator

Stores `dict[int, int | str]` mapping slot number to code for ALL slots on the
lock. Does not distinguish managed vs unmanaged — that distinction lives in the
config entries.

- Push-based providers (Z-Wave JS) set `update_interval = None`
- Poll-based providers use periodic refresh via `_async_update_data()`

## Push vs Poll vs Hard Refresh

- **Push**: Z-Wave value update events → provider filters → `coordinator.push_update()`
- **Poll**: Coordinator's `_async_update_data()` → provider's `async_get_usercodes()` on interval
- **Hard refresh**: `async_hard_refresh_codes()` → refreshes Z-Wave CC values cache from device,
  then reads all slots. Triggered via the `hard_refresh_usercodes` service.

## Managed vs Unmanaged Slots

A slot is **managed** if it exists in any LCM config entry's `CONF_SLOTS` for this
lock AND the corresponding LCM entities exist.

Detection: `_get_slot_entity_states()` returns `None` for unmanaged slots.

Coordinator stores both managed and unmanaged; sync only operates on managed slots;
the UI displays both.

## Sync Decision

The in-sync binary sensor compares **desired state** (PIN from text entity, active
state from condition) against **actual state** (coordinator data):

- If active + PIN differs from coordinator → set
- If inactive + code present in coordinator → clear
- If states match → in sync (no operation)

## Masked PINs

Some locks return `****` instead of actual codes. LCM handles this:

- **Managed slots**: Resolved via the text entity's expected PIN — if the expected
  PIN is set and the coordinator shows `****`, the binary sensor treats it as in-sync
  (the lock has *a* code, and we set it, so we trust it matches).
- **Unmanaged slots**: Kept as-is — `****` just indicates "slot in use."

## Duplicate Code Detection

Two layers of defense against duplicate PINs causing infinite sync loops:

1. **Pre-flight check** (`_check_duplicate_code()` in `BaseLock`): Scans coordinator
   data for matching PINs before sending to the lock. Raises `DuplicateCodeError`.
   Skips masked values (`****`) since they can't be compared.

2. **Event 15 handler** (Z-Wave JS only, `_async_handle_duplicate_code()`): Reactive
   safety net for cases where the pre-flight check can't detect the duplicate (e.g.,
   masked codes from unmanaged slots).

Both paths disable the slot and create a persistent notification.

## Sync Attempt Tracking

The sync mechanism tracks consecutive successful attempts (provider call did not
raise) that fail to resolve the out-of-sync state. If `MAX_SYNC_ATTEMPTS` are
reached within `SYNC_ATTEMPT_WINDOW`, the slot is disabled and the user is notified.

Only "successful" provider calls are tracked — `LockDisconnected` exceptions are
transient and use the separate retry mechanism with `RETRY_DELAY`.

## Base Provider (`BaseLock`)

The `@final` methods `async_internal_set_usercode` and `async_internal_clear_usercode`
are the single entry point for all lock operations. They enforce cross-cutting concerns
in this order:

1. **Duplicate code check** (`_check_duplicate_code`) — pre-flight scan before rate limiting
2. **Connection check** — verify lock is reachable
3. **Rate limiting** — serialize via `asyncio.Lock` with minimum delay
4. **Provider call** — delegate to subclass `async_set_usercode` / `async_clear_usercode`
5. **Coordinator refresh** — update state (skipped for push-based providers)

Helper methods available to all providers:

- `is_masked(code)` — detect `****` masked codes
- `is_slot_managed(code_slot)` — check if any LCM config entry manages this slot

The `source` parameter (`"sync"` or `"direct"`) indicates whether the call came from
the sync path (binary sensor) or a user action (websocket). Currently informational;
future use for differentiated error handling policies.

## Rate Limiting

All lock operations are serialized via `asyncio.Lock` per provider instance, with
a 2-second minimum delay between operations. This prevents overwhelming the lock's
radio and Z-Wave mesh.
