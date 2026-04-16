# TODO

## User-Facing Features

- **Config flow: conflicting integration detection** — Warn if Keymaster or other
  code management integrations are detected at setup.
- **Clear all unmanaged codes** — Button or service to clear unmanaged code slots
  on a lock.
- **Bulk operations** — Enable/disable multiple slots at once.
- **Import/export slot configuration**
- **PIN validation** — Warn on simple PINs ("1234", "0000"), duplicate PINs
  across slots, format violations against lock requirements, and slot conflicts
  across config entries.
- **Service actions:**
  - `set_temporary_code` — time-limited PIN
  - `generate_pin` — auto-generate secure PIN
  - `bulk_enable` / `bulk_disable`
- **Custom Jinja templates** — Ship `.jinja` macros for LCM entity resolution
  (e.g. `lcm_slot_entities(config_entry_id, slot_num)`). Auto-install to
  `custom_templates/` during setup.
- **Drift detection alerting** — Alert users when drift detection consistently
  fails (e.g. lock offline). Currently only logged.

## Providers

**Current:** Akuvox, Matter, Schlage, Z-Wave JS, Virtual (testing)

**Open PRs:** ZHA (#739), MQTT/Zigbee2MQTT (#740)

**Future:** Nuki, SwitchBot, SmartThings

**Cannot support:** esphome (no API), august/yale/yalexs_ble/yale_smart_alarm
(library limitations)

## Architecture Considerations

- **Event-driven vs optimistic push updates** — For providers that support push
  events (Matter LockUserChange, Z-Wave value updates), consider removing
  optimistic pushes from set/clear methods and relying solely on events. The
  event is the lock's actual confirmation the credential was stored, while
  optimistic pushes only confirm the service call was accepted. Event-only
  updates give a single source of truth and simpler code, at the cost of a
  brief latency window before the coordinator updates. Z-Wave may still need
  optimistic pushes to avoid sync loops with stale cache reads.

- **Matter provider: direct Matter client commands** — Replace HA service calls
  (`matter.set_lock_credential`, etc.) with direct `MatterClient.send_device_command()`
  calls to get structured response objects (e.g., `SetCredentialResponse.status`
  with `DlStatus.kDuplicate`). Currently duplicate detection relies on string
  matching the error message. Direct commands would give typed status codes for
  duplicate, occupied, resource exhausted, etc.

## Code Quality

- **Provider exception hierarchy** — Introduce `LockCodeManagerProviderError`
  as a parent for exceptions raised by lock providers (`LockDisconnected`,
  `CodeRejectedError`/`DuplicateCodeError`, `ProviderNotImplementedError`).
  `EntityNotFoundError` stays at the `LockCodeManagerError` base since it's
  about LCM's internal entity-registry view, not the lock itself. The
  internal `_LockQuerySkipped` sentinel in `config_flow.py` also stays at
  the base. Once the parent exists, `_async_get_all_codes` can collapse its
  two-try workaround into a single try with three `except` arms
  (skip / provider-failure / unexpected). Touches every provider and every
  catch site in coordinator/sync/repair — file as its own PR for focused
  review of the classification.
- **Config flow + update listener: shared diff helper** — The options flow's
  added-`(lock, slot)`-pair calculation in
  `LockCodeManagerOptionsFlow._maybe_confirm_then_persist` and the
  `slots_to_add/remove` + `locks_to_add/remove` calculation in
  `__init__.py:async_update_listener` compute related views of the same
  old-vs-new diff. Extract a single `compute_entry_config_diff(old, new)`
  helper in `data.py` returning a frozen dataclass with all three views
  (slot dict diff, lock list diff, cartesian pair diff). Single source of
  truth for slot-key int/str normalization. While there: collapse the two
  near-identical `_create_entry_and_clear_slots` /
  `_persist_options_and_clear_slots` methods into one mixin helper, and
  extract the `scoped_codes` builder in `_maybe_confirm_then_persist` into
  a named helper for readability.
- **Dual storage pattern** — Simplify `data` + `options` config entry pattern.
  Document when to use each.
- **Coordinator-owned sync managers** — Move sync manager lifecycle from binary
  sensor entities to coordinator (survives entity recreation during config
  updates).
- **Dataclass conversion** — Convert config entry data and internal dicts to typed
  dataclasses with `from_dict`/`from_entry` class methods.
- **Websocket optimization** — Add optional `include_entities`/`include_locks`
  flags to `get_config_entry_data` command.
- **Entity registry change detection** — Warn if LCM entity IDs change (reload
  required).
- **Provider diagnostics** — Add `get_diagnostic_data()` to `BaseLock` for
  provider-specific diagnostic information.

## Frontend

- Unify slot and lock data card designs (prefer slot card pattern).
- Test visual editor for both cards.

## Process

- `CLAUDE.md` points to `AGENTS.md`; update `AGENTS.md` after architecture
  changes.
- Review TODOs after completing current work, when starting new features, during
  refactoring sessions, or on `/todos`.
