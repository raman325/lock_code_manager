# TODO

## New Items

- Unify design across slot and lock data cards, with a preference towards the slot card design.
- **Config flow: lock reset step** — Add a config flow step that checks for existing
  unmanaged codes on the lock. Inform the user the lock will be reset before configuring.
  Either they cancel or proceed and all slots are cleared. LCM will re-set its managed
  codes immediately after.
- **Config flow: conflicting integration detection** — Check for Keymaster or other
  code management integrations at setup and warn the user.
- **Update migration docs** — Revise any wiki content that talks about "slowly migrating"
  locks between tools. The guidance is now to commit fully to one tool.
- **"Clear all unmanaged" UI action** — Add a button or service to clear all unmanaged
  code slots on a lock, so users can clean up stale codes without manual intervention.
- **Unify Z-Wave event 15 duplicate handler with CodeRejectedError** — The reactive
  duplicate handler (`_async_handle_duplicate_code` in `zwave_js.py`) uses
  `async_disable_slot` (shared helper in `util.py`). It should surface through
  the exception hierarchy instead. Options: route through the coordinator to
  trigger a sync manager state update, or have the provider set a flag that the
  sync manager checks on next sync cycle. This would also allow the event 15
  handler to reset the sync tracker.
- **Investigate lock-specific duplicate detection carve-outs** — Some locks don't mask
  PINs or don't reject duplicates. Explore whether duplicate detection behavior can be
  adapted per lock capability rather than one-size-fits-all.
- Add type checking to CI:
  - Add type checking CI job to python-checks.yml (mypy already in pre-commit)
  - Explore alternatives to mypy (Astral may have a replacement - check for "ty" or similar)
  - Fix existing type errors (~49 errors as of Mar 2026)
- Test visual editor for both cards.

## Testing

- Strategy UI module has unit tests in `ts/*.test.ts` and Python tests for
  resource registration/unload in `tests/test_init.py`; still need end-to-end
  UI coverage (Lovelace resource registration + reload in a real frontend).
- Add provider tests when new integrations beyond Z-Wave JS and virtual are added.
- Test rate limiting and connection failure timing in live environment.

## Refactors and Maintenance

### Simplify Tests

- Consolidate duplicate test setup code in `tests/common.py` into shared fixtures
- Consider parametrized tests where multiple similar tests exist
- Reduce test boilerplate with helper functions

### Simplify Code

#### Dual Storage and Config Entry Patterns

1. **Dual storage pattern** (`data` + `options` in config entries) - Can this be
   simplified?
2. **Clarify data vs options usage** — Document and standardize when to read from
   `config_entry.data` vs `config_entry.options`. Current understanding: prefer
   `options` only within the config entry update listener during options updates;
   elsewhere use `data` to avoid mid-update inconsistencies.

#### Other Complexity

- **Entity unique ID format** - Is `{entry_id}|{slot}|{type}` optimal?
- **`async_internal_*` method wrappers** — are all necessary?

#### Sync Manager Follow-ups

- Make unload symmetric with setup (unload treats everything as "remove all
  slots/locks" through the same update_listener path)
- Upgrade catch-all state tracking to targeted once entities are discovered
  (currently deferred — modifying subscriptions from within a callback causes
  timing issues)
- Consider coordinator owning sync managers instead of binary sensor entities
  (manager lifecycle would survive entity recreation during config updates)

### Convert Config and Internal Dicts to Dataclasses

Convert config entry data to typed dataclasses with `from_dict`/`from_entry`
class methods. Use object instances internally instead of iterating through raw
config dicts. Audit codebase for other complex dicts that would benefit from
dataclass conversion (e.g., slot data, lock state, coordinator data). This
improves type safety, IDE autocompletion, and code readability.

**Why not Voluptuous?** Voluptuous is for validation, not object instantiation.
Other options like `dacite` or Pydantic add dependencies.

### Rewrite Matter Provider

Rewrite PR #741 using the `SlotCode.UNKNOWN` model. The Matter provider:

- Returns `SlotCode.UNKNOWN` for occupied slots (PINs are write-only)
- Returns `SlotCode.EMPTY` for cleared slots
- Handles user/credential mapping internally (SetUser + SetCredential)
- Uses HA's `matter.set_lock_credential` / `matter.clear_lock_credential` services
- Sync handles `UNKNOWN` natively (no special resolution needed)

HA core Matter lock services: `set_lock_user`, `clear_lock_user`,
`set_lock_credential`, `clear_lock_credential`, `get_lock_users`,
`get_lock_credential_status`, `get_lock_info`.

### Add Optional Flags to `get_config_entry_data` Websocket Command

Not all callers need all the data from `get_config_entry_data`. Add optional
`include_entities` and `include_locks` flags (default `True` for backwards
compatibility) to skip expensive entity registry queries when not needed.

### Entity Registry Change Detection

Track entity registry updates and warn if LCM entities change entity IDs (reload
required).

### Add Provider Diagnostic Data Method

Add `get_diagnostic_data()` method to `BaseLock` for exposing provider-specific
diagnostic information.

### Drift Detection Failure Alerting

Add mechanism to alert users when drift detection consistently fails over
extended periods (e.g., lock offline). Currently failures are logged but there is
no visibility to users or entities.

## Features

### Add Support for Additional Lock Providers

**Current Providers:**

- Z-Wave JS (`zwave_js.py`)
- Virtual (`virtual.py`) - for testing only

**High Priority:**

- **ZHA (Zigbee Home Automation)** - Very popular, supports many lock brands
- **Matter** - Future-proof, industry standard (PR open)
- **MQTT** - Generic protocol, many custom implementations

**Medium Priority:**

- **Nuki** - Popular in Europe
- **Schlage** - Popular in North America
- **SwitchBot** - Growing popularity
- **SmartThings** - Large user base
- **HomeKit Controller** - Apple ecosystem

**Cannot Be Supported** (see README for details):

- `esphome` - No user code API in ESPHome
- `august`, `yale`, `yalexs_ble`, `yale_smart_alarm` - Library limitations

See `AGENTS.md` for implementation approach and `BaseLock` interface.

### Add Relevant New Home Assistant Core Features

**Key Features to Evaluate:**

1. **Selector Improvements** — Review config flow UI for better selectors
2. **Repair Platform** — Consider adding repairs for common misconfigurations

### Improve Dashboard UI/UX

- Add visual indicator when codes are out of sync
- Bulk operations (enable/disable multiple slots)
- Import/export slot configuration
- History view showing when codes were used

### Add Service Actions

- `lock_code_manager.set_temporary_code` - Create time-limited PIN
- `lock_code_manager.generate_pin` - Auto-generate secure PIN
- `lock_code_manager.bulk_enable` / `bulk_disable` - Enable/disable multiple slots
- Note: `hard_refresh_usercodes` service already exists for lock-wide refresh.

### Configuration Validation

- Warn if PIN is too simple (e.g., "1234", "0000")
- Warn if multiple slots use same PIN
- Validate PIN format against lock requirements
- Check for slot conflicts across config entries

## Docs and Process

- `CLAUDE.md` points to `AGENTS.md`; update `AGENTS.md` after architecture changes.
- Review TODOs after completing current work, when starting new features, during
  refactoring sessions, or on `/todos`.
