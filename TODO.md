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
- **EntryConfig: typed boundary container** — Introduce a frozen `EntryConfig`
  dataclass in `data.py` as the single chokepoint for reading entry config.
  Solves the long-standing str/int slot key inconsistency that HA's JSON
  storage round-trip creates (visible today as defensive `slot_key = slot
  if slot in d else str(slot)` patterns scattered across the codebase).

  **Core shape:**

  ```python
  @dataclass(frozen=True, slots=True)
  class EntryConfig:
      locks: tuple[str, ...]
      slots: Mapping[int, Mapping[str, Any]]  # always int keys

      @classmethod
      def from_entry(cls, entry: ConfigEntry) -> EntryConfig: ...
      @classmethod
      def from_mapping(cls, m: Mapping) -> EntryConfig: ...
      def diff(self, other: EntryConfig) -> EntryConfigDiff: ...
      def has_lock(self, lock_entity_id: str) -> bool: ...
      def has_slot(self, slot_num: int) -> bool: ...
      def with_slot_updated(self, slot_num: int, key: str, value: Any) -> EntryConfig: ...
  ```

  **Type boundary cleanup that lands with this:**

  - Migrate readers off raw `entry.data[CONF_SLOTS]` / `entry.options[CONF_SLOTS]`
    indexing onto `EntryConfig.from_entry(entry).slots`. Sites today:
    `coordinator.py:89` (`get_expected_pin`), `entity.py:82/105`,
    `helpers.py:146/191/209`, `websocket.py:194/1018`, providers `virtual.py`
    `:80/93/121`, `__init__.py` listener locals, `config_flow.py:508/544`
    write paths.
  - `get_slot_data(entry, slot_num)` becomes `EntryConfig.from_entry(entry)
    .slots.get(slot_num, {})` — one helper to delete.
  - `get_entry_data(entry, key, default)` becomes `EntryConfig.from_entry(entry)
    .locks` / `.slots` attribute access. Helper can be removed once all
    callers are migrated.
  - `get_managed_slots(hass, lock_entity_id)` keeps its signature but its body
    iterates entries and calls `EntryConfig.from_entry(entry)` instead of
    raw indexing.
  - `find_entry_for_lock_slot(hass, lock_entity_id, code_slot)` similarly.
  - `compute_entry_config_diff(old, new)` becomes a method
    `EntryConfig.diff(self, other) -> EntryConfigDiff`. Module-level helper
    can be removed (or kept as a thin shim during transition).
  - `_async_setup_new_locks(hass, entry, locks_to_add, new_slots, ...)`
    can take `EntryConfig` instead of separate `locks_to_add` + `new_slots`
    args.

  **Defensive patterns to delete after migration:**

  - `helpers.py:146` `slot_key = slot_num if slot_num in slots else str(slot_num)`
  - `helpers.py:191` and `:209` (same pattern)
  - `websocket.py:194` `slots_data.get(slot_num) or slots_data.get(str(slot_num))`
  - `websocket.py:1018` `if slot_num not in slots and str(slot_num) not in slots`
  - `coordinator.py:89` `.get(str(slot_num), {})` becomes `.get(slot_num, {})`
  - The `for code_slot in get_entry_data(entry, CONF_SLOTS, {})` patterns where
    the int(code_slot) cast is needed (find_entry_for_lock_slot, get_managed_slots)

  **Ancillary improvements worth bundling:**

  - **TypedDict for slot config** — define `class SlotConfig(TypedDict)` with
    fields `pin: NotRequired[str]`, `enabled: bool`, `name: NotRequired[str]`,
    `entity_id: NotRequired[str]`, `number_of_uses: NotRequired[int]`. Replaces
    `dict[str, Any]` typing for slot inner dicts; gives pyright real signal on
    slot reads/writes.
  - **Listener int normalization (was PR #1028 review item #3)** — once the
    str-hardcoded readers are migrated, the listener can normalize its locals
    (`curr_slots`, `new_slots`) to int keys without breaking downstream
    consumers. Closes the latent `slots_unchanged` `KeyError` risk.
  - **EntryConfigDiff source-key-type complexity goes away** — currently
    preserves the source's str-or-int key type to avoid breaking the listener.
    With normalized inputs guaranteed, all dict outputs can be `Mapping[int, ...]`
    and the int-normalization-for-comparison-only special case in
    `compute_entry_config_diff` simplifies to plain set ops.
  - **Drop `get_entry_data`'s options-over-data fallback in callers that don't
    need it** — once `EntryConfig.from_entry()` encapsulates the priority logic,
    callers stop carrying that detail.
  - **HA storage round-trip stays as-is** — JSON layer continues to serialize
    int keys to str on disk; we don't fight it. Normalization is purely on
    READ via `from_entry()`. This keeps the migration additive (no changes to
    on-disk format, no migration version bump needed).

  **Migration approach:** introduce `EntryConfig` and migrate one module at
  a time (coordinator → entities → helpers → websocket → providers →
  listener). Each step deletes its local defensive str-handling. Single PR
  for the introduction + first migration target; follow-up PRs for the rest
  to keep reviews focused.
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
