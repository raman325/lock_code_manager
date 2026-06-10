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
  - `generate_pin` — auto-generate secure PIN
- **Custom Jinja templates** — Ship `.jinja` macros for LCM entity resolution
  (e.g. `lcm_slot_entities(config_entry_id, slot_num)`). Auto-install to
  `custom_templates/` during setup.

## Providers

**Future:** Nuki, SimpliSafe (has `async_get_pins()` in simplipy — full
read/write, name-based, max 4 user PINs), SwitchBot, SmartThings

**Cannot support:** esphome (no API), august/yale/yalexs_ble/yale_smart_alarm
(library limitations), ISY994 (pyisy has no code read support)

### Matter provider

- **Known Aqara U300 limitations** (discovered during live testing 2026-04-18):
  - User names with spaces rejected (500 error from `set_lock_user`)
  - Status `unknown(133)` when setting credential on occupied slot without clearing
  - Lock disconnects from Thread network after repeated HA restarts (needs battery
    pull + Matter server restart to recover)

## Architecture Considerations

- **Event-driven vs optimistic push updates** — Matter, Z-Wave JS, and
  Zigbee2MQTT all need optimistic pushes from set/clear methods. Z-Wave JS
  needs them to avoid sync loops with stale cache reads. Matter needs them
  because PINs are write-only. Zigbee2MQTT uses MQTT QoS 0 which has no
  delivery confirmation. Removing optimistic pushes is not viable for any
  push-based provider. Event-only updates would leave a latency window where
  the coordinator has stale data, triggering unnecessary re-sync attempts.

- **Coordinator-owned sync managers** — Move sync manager lifecycle from binary
  sensor entities to coordinator (survives entity recreation during config
  updates).

- **Expand sync "in sync" predicate to include user name + every managed
  credential type** — Today sync compares PIN only. With names (User
  Credential CC) and future credential types (PASSWORD, etc.), a slot
  should be "out of sync" if any field — name, PIN, or any managed
  non-PIN credential — drifts from config. Smart writes only the deltas:
  name-only via `async_set_user`, credential-only via `async_set_credential`
  per mismatching type, both via `_put_credential` for the create path.
  Trivially-equal name comparison on User Code CC locks (both sides
  None) keeps the same code path safe for legacy locks. Requires the
  coordinator/sync state to carry `User` rather than `dict[int,
  SlotCredential]` so name is visible to the sync predicate.

- **Least-common-denominator capability aggregation across locks in a
  config entry** — When a config entry covers multiple locks, aggregate
  per-credential-type limits as the LCD so a single configured value
  works on all of them. Rules per capability kind:
  - Numeric bounds: `min_length = max(...)`, `max_length = min(...)`
    across the locks that support the type. Skip locks where the
    capability doesn't apply (e.g. User Code CC locks contribute
    nothing to name-length aggregation because UC has no names).
  - Type availability: any lock supports the type → the field appears
    on the slot config; the sync layer per-lock decides whether to
    write. When no lock supports the type, hide the field entirely.
  - Boolean feature flags (e.g. `supports_learn` for RFID): AND across
    the supporting locks.
  Empty aggregate (e.g. PIN min > max because one lock requires <=6 and
  another requires >=8) must be a hard config flow failure surfaced in
  both the initial config flow and the option flow ("these locks have
  incompatible PIN length requirements; split them into separate config
  entries"). On lock add/remove, re-validate existing slot configs
  against the recomputed aggregate and disable any slots whose stored
  values fall outside the new bounds, with a clear repair-suggestion
  notification. Enforce at the `text` entity level via `native_min` /
  `native_max` for both PIN and name. Lands after the password
  expansion + name-reconciliation sync work above.

- **Multi-credential-type support: password (Option B architecture
  decision)** — Z-Wave User Credential CC and Matter DoorLock both
  expose `PASSWORD` natively; this is the first concrete second type
  beyond PIN. Sequence-wise lands after the sync-state expansion above
  and the LCD aggregation, since both are prerequisites for a coherent
  multi-type UX.
  The feature work: slot config schema becomes `{name, pin, password,
  ...}` (slot per user, multiple credential types per slot);
  capability-gated per slot by `max_user_name_length > 0` and at least
  one lock advertising `credential_types[PASSWORD]`; sync layer
  multiplexes the write/clear per type.
  The architecture decision (Option B): the base orchestration already
  carries Option A (parametric per-type projection via
  `_project_users_to_slots(credential_type)`) so the seam supports
  adding types additively. Open question is whether the
  coordinator/entities also become type-scoped from the top — separate
  slot entity trees per credential type — or whether a single slot
  surfaces multiple type fields. Tied up with the lock-side slot
  mapping: Z-Wave User Credential CC indexes (user_id,
  credential_type, credential_slot) independently, so LCM "slot N"
  could hold PIN at one lock-side index and PASSWORD at another.
  Simplest is to enforce `lock_slot = user_id = LCM_slot_N` for every
  credential type (matches the existing 1:1:1 invariant, may waste
  lock-side slot capacity when types are sparsely used); richer is a
  per-type mapping table (more flexibility, more state to track and
  recover at setup). Decide the entity-tree shape and the slot-mapping
  rule together when wiring password — they're one design question
  with two surfaces.

## Code Quality

- **Dual storage pattern** — Simplify `data` + `options` config entry pattern.
  Document when to use each.
- **TypedDict for slot config** — Define `class SlotConfig(TypedDict)` with
  `pin: NotRequired[str]`, `enabled: bool`, `name: NotRequired[str]`,
  `entity_id: NotRequired[str]` to replace `dict[str, Any]` for slot inner
  dicts. Gives pyright real signal on slot reads/writes. Would let
  `EntryConfig.slots` be typed `Mapping[int, SlotConfig]`.
- **`coordinator.data` typing** — Currently `dict[int, str | SlotCode]`,
  already int-keyed. `binary_sensor.py:281` and `entity.py:193` cast
  defensively against `self.slot_num`'s type variance — could be cleaned
  up once entities consistently carry int slot_num end-to-end.
- **Other internal dict boundaries** — `websocket.py:401/442/524/529`
  cast into websocket-internal lookup dicts; `__init__.py:561` casts at
  a callback boundary. Same broader "typed slot_num everywhere" theme as
  the EntryConfig migration; resolved by the SlotConfig TypedDict work
  above plus typed callback signatures.
- **Entity registry change detection** — Warn if LCM entity IDs change (reload
  required).

## Testing

- **Live testing** — Remaining scenarios not yet validated on any lock:
  - Multiple config entries sharing the same lock (conflict detection)
  - Hard refresh drift detection (verify poll catches out-of-band changes)
  - Config flow re-add (picks up existing codes on lock)

## Frontend

- Test visual editor for both cards (post-redesign).

## Process

- `CLAUDE.md` points to `AGENTS.md`; update `AGENTS.md` after architecture
  changes.
- Review TODOs after completing current work, when starting new features, during
  refactoring sessions, or on `/todos`.
