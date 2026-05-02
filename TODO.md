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

**Current:** Akuvox, Matter, Schlage, Z-Wave JS, ZHA, Zigbee2MQTT (MQTT), Virtual (testing)

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
- **Enhance sync_status display** — The slot card and lock-codes card now show
  granular sync states (in_sync, out_of_sync, syncing, suspended) with distinct
  icons and colors. Future work: spinner animation for syncing state, richer
  suspended state details (e.g., show the repair issue reason).

### Slot/lock card redesign — Phase B (deferred from PR #1116)

Polish + a11y items deferred from the slot/lock card redesign. Phase A
fixed the WCAG-blocking interactions and the design hierarchy issues.
Phase B is non-blocking but worth a follow-up pass.

**Visual / consistency:**

- **Color opacity unification** — settle on 2-3 canonical opacity stops for
  state colors. Currently warning is used at 4%, 6%, 8%, 10%, 16% across
  the slot card alone. Decide a system (e.g., 6% backgrounds, 12% surfaces,
  16% chips/badges) and apply consistently.
- **Active state vocabulary** — slot card has three positive signals for
  the same Active state (card-level blue tint, green chip with dot, green
  badge). Decide whether Active should be neutral ("color the exception,
  not the norm") or unify to a single color family.
- **Active-Unmanaged chip on the lock card** is nearly identical to the
  Empty chip. Add a subtle warm-gray accent so an active unmanaged code
  reads as "occupied" at a glance.
- **`Slot N · {entry_title}` rendering inconsistency** — slot card uses
  18px title style, lock card uses 11px uppercase kicker style. Pick one.
- **Redundant `ENABLED` label** in the hero next to the switch — a switch
  self-describes. Consider dropping the label.
- **Pencil + editable-span dual click target** on the name in the hero —
  pick one affordance and remove the other.
- **`.collapsible-content { max-height: 500px }`** could clip when many
  helpers + a calendar entity row stack. Use `auto` with a transition shim
  or a much larger ceiling.
- **Condition summary badge color** — allowing currently uses primary blue
  tint instead of success green. Switch to success-color treatment so
  green = good, warning = needs attention everywhere.
- **`Helpers` sublabel** uses 10px while other small-caps labels use 11px.
  Unify to one scale.
- **Replace ✓ / ✗ glyphs** in summary badges with `mdiCheck` / `mdiClose`
  for visual parity with the rest of the card chrome.
- **Card-header icon bubble semantics** — slot card uses a generic key
  icon, lock card uses a lock icon. Decide whether the bubble is "this is
  an LCM card" (consistent) or surfaces state on the slot card (key when
  active, clock when blocked, lock-off when disabled).
- **Dialog microcopy** — "Pick a calendar, schedule, binary sensor, switch,
  or input boolean" enumerates technical domains. Consider "Pick a
  calendar, schedule, helper, or any on/off entity" for non-power users.

**Accessibility:**

- **Color contrast verification** against HA default light + dark themes.
  Likely sub-AA combinations to check:
  - `.hero-field-label` on the hero tinted background.
  - `.lcm-code.off` (`disabled-text-color`) on `--lcm-section-bg` — likely
    the worst case.
  - `.lock-synced-time` (11px secondary-text).
  - `.summary-cell-zero` on numeric "0" cells — these convey data, not
    just decoration.
  - `.action-error` (white on `--error-color, #db4437`) — about 4.21:1,
    below AA for normal text. Use a darker red or bump font weight.
- **`prefers-reduced-motion` opt-out** for transitions — collapsible
  chevron rotation, content max-height animation, slot chip hover
  translate, editable hover background.
- **`aria-hidden="true"`** on decorative dots in `.state-chip .dot` and
  `.lcm-badge .dot` (and the `.lcm-code-pending-icon` clock prefix).
- **Edit inputs missing accessible names** — name/PIN inputs and
  slot-code-input have placeholders but no `aria-label` (placeholders
  are not accessible names).
- **Card titles should be real headings** — `.card-header-title` and
  `.header-title` are `<span>`s. HA stock cards use `<h2>`/`<h3>`.
- **Helper / lock lists** could be `<ul>`/`<li>` so screen readers
  announce the item count.
- **Conditions/Lock Status section titles** inside collapsible headers
  could be `<h3>`s for heading navigation.
- **Summary table missing semantics** — add
  `<caption class="visually-hidden">` and `scope="col"` / `scope="row"`.
- **Suspended banner** on lock card needs `role="status"` (persistent
  state info, but `alert` would be too aggressive).
- **Touch targets** — pencil and reveal buttons at 28px pass WCAG 2.5.5
  AA (24px) but miss the 44px AAA recommendation. Action error dismiss
  button is borderline 24px.
- **Lock card "Last used: Never used" with clickable arrow** opens an
  empty more-info dialog. Either suppress the arrow or reword.
- **State chip color-only differentiation** — `.lcm-code.off` vs
  `.lcm-code.pending` distinguish via clock-icon prefix only. Add
  `aria-label` to the icon or visually-hidden text inside the pending
  span.
- **Collapsible badge symbols (✓/✗)** — most screen readers say "check
  mark"/"ballot X". Add `aria-label="Allowing access: {name}"` on the
  parent badge.

## Process

- `CLAUDE.md` points to `AGENTS.md`; update `AGENTS.md` after architecture
  changes.
- Review TODOs after completing current work, when starting new features, during
  refactoring sessions, or on `/todos`.
