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

- **Dual storage pattern** — Simplify `data` + `options` config entry pattern.
  Document when to use each.
- **Coordinator-owned sync managers** — Move sync manager lifecycle from binary
  sensor entities to coordinator (survives entity recreation during config
  updates).
- **EntryConfig migration** — Multi-stage refactor centralizing entry-config
  reads through a typed `EntryConfig` dataclass to eliminate the str/int slot
  key inconsistency that HA's JSON storage round-trip creates. **Stage 1
  delivered in PR #1029**: introduced `EntryConfig` with `from_entry` /
  `from_mapping` / `slot` / `has_slot` / `has_lock` / `empty`, cached it on
  `runtime_data.config` (refreshed at the top of `async_update_listener` so
  even entity-driven writes update the cache), and migrated readers in
  `data.get_slot_data` / `get_managed_slots` / `find_entry_for_lock_slot`,
  `coordinator.get_expected_pin`, and `websocket._get_condition_entity_id` /
  `subscribe_code_slot` validator. The accessors absorb `int|str` slot_num
  internally so call sites have no visible casts.

  **Stage 2: writers** — Migrate sites that mutate `config_entry.data[CONF_SLOTS]`
  directly off raw dict access. Affected sites:

  - `entity._update_config_entry` (`entity.py:105`) — `data[CONF_SLOTS][self
    .slot_num][self.key] = value`. Needs an `EntryConfig.with_slot_field_set
    (slot_num, key, value)` immutable helper that returns a new `EntryConfig`,
    plus a `to_dict()` for handing back to `async_update_entry`.
  - `helpers.py:146` `slot_key = slot_num if slot_num in slots else str(slot_num)`
  - `helpers.py:191` and `:209` (same defensive pattern around
    `data[CONF_SLOTS][slot_key][CONF_ENTITY_ID]` set/delete)
  - `config_flow.py:508/544` write paths — these construct fresh slot dicts
    so they're already controlled, mostly want type-tightening.

  After Stage 2, `runtime_data.config.slots` is the only authoritative view
  and writers go through it.

  **Stage 3: listener int-normalization** (was [PR #1028 review item #3](https://github.com/raman325/lock_code_manager/pull/1028)) — Once writers are
  migrated, the listener's `curr_slots` / `new_slots` locals can normalize to
  int keys without breaking downstream consumers. Closes the latent
  `slots_unchanged` `KeyError` risk and lets `EntryConfigDiff` drop its
  source-key-type preservation gymnastics (all dict outputs can be
  `Mapping[int, ...]`, the int-normalization-for-comparison-only special
  case in `compute_entry_config_diff` simplifies to plain set ops).

  **Stage 4: API cleanup** — After the migration is end-to-end:

  - `compute_entry_config_diff(old, new)` becomes a method
    `EntryConfig.diff(self, other) -> EntryConfigDiff`. Module-level helper
    can be removed.
  - `_async_setup_new_locks(hass, entry, locks_to_add, new_slots, ...)` takes
    `EntryConfig` instead of separate `locks_to_add` + `new_slots` args.
  - `get_entry_data(entry, key, default)` callers that don't need the
    options-over-data fallback can switch to `runtime_data.config.locks` /
    `.slots`. Helper can eventually be removed.
  - `get_slot_data(entry, slot_num)` thin-wrapper can be removed; callers
    use `get_entry_config(entry).slot(slot_num)` directly.

  **Out-of-scope boundaries** (related, not part of this migration):

  - **TypedDict for slot config** — `class SlotConfig(TypedDict)` with
    `pin: NotRequired[str]`, `enabled: bool`, `name: NotRequired[str]`,
    `entity_id: NotRequired[str]`, `number_of_uses: NotRequired[int]`.
    Replaces `dict[str, Any]` for slot inner dicts; gives pyright real
    signal on slot reads/writes. Would let `EntryConfig.slots` be typed
    `Mapping[int, SlotConfig]`.
  - **`coordinator.data` typing** — currently `dict[int, str | SlotCode]`.
    Already int-keyed; `binary_sensor.py:281` and `entity.py:193` cast
    defensively against `self.slot_num`'s type variance. Goes away when
    listener is migrated and entities receive int slot_num.
  - **Other internal dict boundaries** — `websocket.py:401/442/524/529`
    cast into websocket-internal lookup dicts, `__init__.py:561` casts at
    a callback boundary. These are separate from EntryConfig but solved
    by the same broader "typed slot_num everywhere" theme.

  **HA storage round-trip stays as-is** — JSON layer continues to serialize
  int keys to str on disk; we don't fight it. Normalization is purely on
  READ via `from_entry()`. The migration is additive — no changes to
  on-disk format, no migration version bump needed.
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
