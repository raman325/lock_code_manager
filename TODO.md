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

## Providers

**Current:** Akuvox, Matter, Schlage, Z-Wave JS, Zigbee2MQTT (MQTT), Virtual (testing)

**Open PRs:** ZHA (#739)

**Future:** Nuki, SwitchBot, SmartThings

**Cannot support:** esphome (no API), august/yale/yalexs_ble/yale_smart_alarm
(library limitations)

### Zigbee2MQTT provider

- **Code slot events** — Map Z2M lock/unlock actions with user identifiers to
  `async_fire_code_slot_event()`. Currently `supports_code_slot_events = False`.
- **Narrow broad `except Exception` in `async_get_usercodes`** — The wait_for
  catch after TimeoutError catches all exceptions. Consider narrowing or
  documenting why it must remain broad.

### Matter provider

- **Direct Matter client commands** — Replace HA service calls
  (`matter.set_lock_credential`, etc.) with direct `MatterClient.send_device_command()`
  calls to get structured response objects (e.g., `SetCredentialResponse.status`
  with `DlStatus.kDuplicate`). Currently duplicate detection relies on string
  matching the error message. Direct commands would give typed status codes for
  duplicate, occupied, resource exhausted, etc.

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

## Code Quality

- **Dual storage pattern** — Simplify `data` + `options` config entry pattern.
  Document when to use each.
- **TypedDict for slot config** — Define `class SlotConfig(TypedDict)` with
  `pin: NotRequired[str]`, `enabled: bool`, `name: NotRequired[str]`,
  `entity_id: NotRequired[str]`, `number_of_uses: NotRequired[int]` to
  replace `dict[str, Any]` for slot inner dicts. Gives pyright real signal
  on slot reads/writes. Would let `EntryConfig.slots` be typed
  `Mapping[int, SlotConfig]`.
- **`coordinator.data` typing** — Currently `dict[int, str | SlotCode]`,
  already int-keyed. `binary_sensor.py:281` and `entity.py:193` cast
  defensively against `self.slot_num`'s type variance — could be cleaned
  up once entities consistently carry int slot_num end-to-end.
- **Other internal dict boundaries** — `websocket.py:401/442/524/529`
  cast into websocket-internal lookup dicts; `__init__.py:561` casts at
  a callback boundary. Same broader "typed slot_num everywhere" theme as
  the EntryConfig migration; resolved by the SlotConfig TypedDict work
  above plus typed callback signatures.
- **Websocket optimization** — Add optional `include_entities`/`include_locks`
  flags to `get_config_entry_data` command.
- **Entity registry change detection** — Warn if LCM entity IDs change (reload
  required).
- **Provider diagnostics** — Add `get_diagnostic_data()` to `BaseLock` for
  provider-specific diagnostic information.

## Testing

- **Live Matter lock testing** — Remaining scenarios not yet validated
  (2026-04-18):
  - PIN change while in sync (re-sync via clear-then-set)
  - Multiple config entries sharing the same lock (conflict detection)
  - Hard refresh drift detection (verify 1-hour poll catches changes)
  - Config flow re-add (picks up existing codes on lock)

## Frontend

- Unify slot and lock data card designs (prefer slot card pattern).
- Test visual editor for both cards.
- **Enhance sync_status display** — The slot card and lock-codes card now show
  granular sync states (in_sync, out_of_sync, syncing, suspended) with distinct
  icons and colors. Future work: spinner animation for syncing state, richer
  suspended state details (e.g., show the repair issue reason).

## Process

- `CLAUDE.md` points to `AGENTS.md`; update `AGENTS.md` after architecture
  changes.
- Review TODOs after completing current work, when starting new features, during
  refactoring sessions, or on `/todos`.
