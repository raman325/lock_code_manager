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
  reads and writes through a typed `EntryConfig` dataclass to eliminate the
  str/int slot key inconsistency that HA's JSON storage round-trip creates.

  **Stage 1 delivered in PR #1029**: introduced `EntryConfig` with
  `from_entry` / `from_mapping` / `slot` / `has_slot` / `has_lock` / `empty`,
  cached it on `runtime_data.config` (refreshed at the top of
  `async_update_listener` so even entity-driven writes update the cache),
  and migrated readers in `data.get_slot_data` / `get_managed_slots` /
  `find_entry_for_lock_slot`, `coordinator.get_expected_pin`, and
  `websocket._get_condition_entity_id` / `subscribe_code_slot` validator.
  The accessors absorb `int|str` slot_num internally so call sites have no
  visible casts.

  **Stage 2 delivered (this PR)**: added immutable update API
  (`with_slot_field_set`, `with_slot_field_removed`, `to_dict`) and migrated
  every writer off raw `data[CONF_SLOTS][...]` mutation —
  `entity._update_config_entry`, `helpers.async_set_slot_condition`,
  `helpers.async_clear_slot_condition`, `helpers.get_slot_config` (drops
  the defensive `slot if slot in slots else str(slot)` pattern). Also
  deleted `get_entry_data` entirely after a survey showed 100% of its
  callers were doing `CONF_LOCKS` or `CONF_SLOTS` lookups — all migrated
  to `EntryConfig` (`__init__.py`, `helpers.py`, `config_flow.py`,
  `websocket.py`).

  **Stage 3 delivered (this PR)**: listener now uses `EntryConfig` views
  for its locals (`curr_slots = old_config.slots`, etc.) — int-keyed
  throughout, closing the latent `slots_unchanged` `KeyError`
  ([PR #1028][pr-1028] review item #3 plus the post-merge Copilot
  comments on #1030). `EntryConfigDiff` slot outputs are now typed
  `Mapping[int, Mapping[str, Any]]` and `frozenset[int]` —
  source-key-type preservation gymnastics gone. The listener writes
  back via `new_config.to_dict()` so persisted data stays JSON-safe.

  **Stage 4 still pending: API cleanup**:

  - `compute_entry_config_diff(old, new)` becomes a method
    `EntryConfig.diff(self, other) -> EntryConfigDiff`. Module-level helper
    can be removed.
  - `_async_setup_new_locks(hass, entry, locks_to_add, new_slots, ...)` takes
    `EntryConfig` instead of separate `locks_to_add` + `new_slots` args.
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

[pr-1028]: https://github.com/raman325/lock_code_manager/pull/1028
