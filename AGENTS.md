# AGENTS.md

This file provides guidance to all coding agents (e.g., GPT-4/4o/5 via Codex
CLI, Opus via Claude Code CLI, etc.) when working with code in this repository.
`CLAUDE.md` points here so that Claude shares a consistent view of the project
with other coding agents.

## Overview

Lock Code Manager is a Home Assistant custom integration that allows users to manage lock
usercodes across multiple locks. It reduces entity/automation sprawl compared to
alternatives by handling logic internally rather than generating numerous Home Assistant
entities.

## Follow these rules always

- `git` and `gh` commands are automatically approved
- When making changes to ts files, before handing it back over to me, run yarn test and build and fix any issues.
- When making changes to python files, before handing it back over to me, run pytest in the venv and fix any issues.
- When creating a PR, ALWAYS use the .github/PULL_REQUEST_TEMPLATE.md template
- When submitting a PR, note that Copilot will eventually add review comments which you must review -
  either make changes as needed or explain why you are not addressing a comment (comments that you make
  code changes for don't need a response). Resolve all comments as you address them or respond to them.
  Provide a summary of what was done or not done and ask for confirmation before committing the changes.
- When running hass or any other python driven commands, always run from the venv
- Before committing, ensure pre-commit checks succeed and tests pass.
- Use `prek` instead of `pre-commit` for running pre-commit checks (`prek run --all-files`).
  `prek` is a faster drop-in replacement.

### Git conventions

- **Never amend commits after a PR has been created.** Always commit forward. Squash-on-merge
  handles the final cleanup. Separate commits show what changed in response to each review round.
- Force-push is only acceptable for rebasing onto an updated base branch, not for rewriting history.
- Tests go with code changes in the same PR. Test-only PRs are only for refactoring existing tests.

## External repositories/directories that are relevant

- `../lock_code_manager.wiki/` - hosts the GitHub wiki for this project. Changes are automatically live when pushed, and
  changes related to user facing changes in the integration should only be pushed when that code is released.
- `../home-assistant/` the `home-assistant/core` repo (I am a member of the `home-assistant` Organization)
- `../zwave-js-server/` a server that enables clients like `zwave-js-server-python` to interact with the Z-Wave JS driver
  over WebSockets. (`zwave-js/zwave-js-server`)
- `../zwave-js-server-python` the Python library that the HA zwave_js integration uses to communicate with Z-Wave JS
  (`home-assistant-libs/zwave-js-server-python`)
- `../frontend/` the `home-assistant/frontend` repo
- `../keymaster/` the `FutureTense/keymaster` repo, another integration to manage lock user codes
- `~/.homeassistant/` the config folder that is used when I run HA locally for testing purposes

## Architecture

### Layering

The package (`custom_components/lock_code_manager/`) is split into layers.
`tests/test_architecture.py` enforces the import rules, counting imports under
`TYPE_CHECKING` too.

- **`sync/`** -- the sync engine: `worker.py` (per-lock worker), `registry.py`
  (slot registry), `slot.py` (slot model). Imports nothing from Home Assistant
  and nothing else from this package.
- **`model/`** -- everything shared by providers and the HA layer: data types,
  rules, and read-only queries over config entries (`credentials.py`,
  which also holds `SlotCredential` and the `SlotCode` labels,
  `names.py`, `pin_generator.py`, `occupancy.py`, `slot_assignment.py`,
  `config.py`, `user_migration.py`, `exceptions.py`, `events.py`,
  `queries.py`, `locks.py`, `pin_masking.py`). It may use Home
  Assistant (hass, the bus, the config entries) but never imports `ha/` or
  `providers/`.
- **`ha/`** -- the Home Assistant runtime: `entry.py` (config entry runtime data
  and the `LockCodeManagerConfigEntry` alias), `runtime.py` (lock runtime),
  `adapter.py` (provider port for the sync engine), `lock_store.py`, `locks.py`,
  `slot_coordinator.py`, `callbacks.py`, `references.py`, `unmanaged.py`,
  `allocation.py`, `services.py`, `validation.py`. It may import everything.
- **`providers/`** -- one module per lock integration. Imports `model/`, never
  `ha/`.
- Top-level modules (platforms, `config_flow.py`, `websocket.py`, `repairs.py`,
  `diagnostics.py`, `entity.py`, `const.py`, `__init__.py`) may import any layer.

### Core Components

**Providers System** (`custom_components/lock_code_manager/providers/`)

- Plugin-based architecture for supporting different lock integrations
- `_base.py`: `BaseLock` abstract class defining the provider interface
- `_mqtt.py`: `BaseMqttLock`, shared behaviour for locks reached through an MQTT bridge
  (operational preamble, sequential per-slot read, resubscribe policy, poll cadences)
- `zwave_js.py`: Z-Wave JS lock implementation
- `matter.py`: Matter lock implementation
- `schlage.py`: Schlage WiFi lock implementation
- `akuvox.py`: Akuvox intercom/lock implementation
- `zha.py`: Zigbee Home Automation lock implementation
- `zigbee2mqtt.py`: Zigbee2MQTT lock implementation (via MQTT)
- `zwave_js_ui.py`: zwave-js-ui lock implementation (via MQTT, api-driven)
- `virtual.py`: Virtual lock implementation -- a credential store rather than a device.
  Advertises every credential type with no limits, and reports code slot events so it can be
  the recording surface for credentials used where Lock Code Manager observes nothing
  (`use_credential`). Also the way to try Lock Code Manager without a real lock.
- Each provider implements: `async_get_users()`, `async_set_credential()`, `async_delete_credential()`,
  `async_is_integration_connected()`, `async_hard_refresh_codes(slots)`
- Providers listen for lock-specific events and translate them to LCM events via `async_fire_code_slot_event()`
- `BaseLock` provides `managed_slots` property (aggregates slots across all config entries for the lock,
  disabled ones included; it is `slots_configured_on`)
  and `async_call_service()` helper (wraps HA service calls with `LockDisconnected` error handling)
- Config questions -- which entry's configuration lists this lock or slot -- go through the named
  questions in `model/queries.py` (`entries_configuring`, `slots_configured_on`, `configured_owner`,
  `another_entry_configures`, `every_configured_lock`). Each counts a different set of entries;
  ask the one you mean rather than filtering `async_entries` by hand. Live lock-instance ownership
  is a separate question and filters by load state on purpose: the lock registry
  (`sync/registry.py`), and `iter_loaded_lcm_entries` / `get_managed_locks` for the locks loaded
  entries hold

**Sync engine** (`sync/`, `ha/runtime.py`, `ha/adapter.py`, `ha/lock_store.py`)

- One `LockWorker` per lock (`sync/worker.py`), shared by every entry that holds the lock
  (`sync/registry.py` `LockRegistry`). A single task takes jobs from a queue -- sync one
  slot, read, a push, a backoff probe -- one at a time. That queue is the whole of the
  serialization; the gap between exchanges is the provider's `_min_operation_delay` (2 s).
- Path of a change: `SlotEntityCoordinator.async_push_want` -> `ha/runtime.async_want` ->
  the lock's worker -> `ProviderPort` (`ha/adapter.py`) -> the provider's
  `async_set_usercode(..., source="sync")` / `async_clear_usercode` /
  `async_release_managed_slot(slot, *, written=...)`.
- Every write is read back (`async_get_usercodes`, or `async_hard_refresh_codes(slots)` when
  the provider sets `fresh_read_is_hard_refresh`). The provider's `WriteResult` is ignored.
- `ProviderPort` also keeps the provider's cadence (scan poll for non-push locks,
  `hard_refresh_interval` drift read, `connection_check_interval` check), hands pushes to the
  worker, and refuses a PIN another slot holds or wants, unsent (`_duplicate_of`).
- What LCM wrote to each slot is persisted as a salted fingerprint (or `cleared`) in the
  `lock_code_manager.core` store, keyed by the lock's entity registry id
  (`ha/lock_store.py`). A masked or silent slot whose fingerprint matches counts as in sync,
  so a restart does not rewrite PINs. Owed release clears persist too.
- Releasing a slot no entry manages any more removes only what LCM wrote; it is a worker
  clear, retried like any other.

**Main Module** (`__init__.py`)

- Entry point manages config entry lifecycle
- `async_update_listener()`: Core function handling dynamic entity creation/removal when config changes
- Uses `EntityCallbackRegistry` (`ha/callbacks.py`) to add and remove entities as config changes
- Registers Lovelace strategy resource for dashboard UI

**Config Flow** (`config_flow.py`)

- Main flow: name and locks, then users (UI or YAML). Users are config subentries
  (`LockCodeManagerUserSubentryFlow`); slot numbers are allocated, never chosen, and only
  in slots free on every lock
- Refuses a lock that already holds codes in the entry's slots (`existing_codes`) or
  whose occupancy cannot be read (`occupancy_unknown`); warns, without asking, about a
  lock that cannot report its codes (`cannot_report`)

### Entities

- `binary_sensor.py`: PIN active status (enabled + conditions met) and per-lock in-sync status
  with `sync_status` extra state attribute (`in_sync`, `out_of_sync`, `syncing`, `suspended`).
  `in_sync` is the aggregate over every managed credential; one `<type>_in_sync` sensor per
  credential type (`pin_in_sync` today) views a single credential and is disabled by default
- `sensor.py`: Per-lock slot PIN sensors showing current codes on each lock
- `text.py`: Name and PIN configuration entities
- `switch.py`: Slot enabled/disabled toggle
- `event.py`: per-user `credential_used` event entity. Its `event_types` are
  credential kinds -- `MANAGED_CREDENTIAL_TYPES` unioned with everything the
  entry's locks advertise -- and a recorded use fires with the kind it was.
  It records from `BUS_EVENT_CREDENTIAL_USED` alone and publishes that
  payload (`name`, `config_entry_id`, `config_entry_title`, `source`,
  `target`, `credential_type`, `operation`) as its state attributes,
  alongside `code_slot` and `slot_field`. Where the credential was used is
  `target`, not `event_type`

### Data Flow

1. User configures slots and locks via config flow
2. `async_update_listener()` holds each lock in the lock registry, which builds its
   provider, `ProviderPort` and `LockWorker` the first time any entry holds it
3. Lock provider sets up listeners for lock-specific events (e.g., Z-Wave JS notification events)
4. The worker reads the lock (first read, the adapter's polls, pushes from the provider)
5. When slot config changes (name, PIN, enabled, condition), the slot's
   `SlotEntityCoordinator` pushes what it wants to each lock's worker, which writes
   through `BaseLock.async_set_usercode()` / `async_clear_usercode()` and reads the
   slot back
6. Provider fires `BUS_EVENT_CREDENTIAL_USED`
   (`lock_code_manager_credential_used`) when a credential is used, carrying
   the user's name, the config entry, and the `source`/`target` entities. This
   is the event consumers should build on.
7. That is the only bus event. The older lock-shaped
   `lock_code_manager_lock_state_changed` was removed in 6.0; it was built
   around the lock rather than the person and carried a `code_slot` that tied
   every consumer to a number the integration reassigns. The per-slot
   `credential_used` event entity records from the unified event, which is
   what lets it record a use whose target is not a lock at all.

### Slot sync (`sync/slot.py`, `sync/worker.py`)

A slot's `Status` is `unknown`, `in_sync`, `out_of_sync` or `suspended` (held). The
`sync_status` attribute adds `syncing` while the worker is on it.

- A sync reads the slot if never read, writes it if it differs, then reads it back.
- A sync that does not end in sync costs the slot one try, charged only once the lock
  has answered. After 3 tries the slot is held (2 for a release): `slot_suspended`, or
  `slot_clear_failed` for a clear. Fights -- the lock going from agreeing to disagreeing,
  e.g. a keypad change -- hold it the same way, aging out over 300 s. Failed tries pause
  the slot 30 s, then 120 s. A hold is lifted only by a change of target or by
  acknowledging its repair.
- The adapter maps provider errors (`ha/adapter.py` `lock_error`): `CodeRejectedError` /
  `DuplicateCodeError` on a set disables the slot (`slot_disabled`); a refused clear holds it
  at once (`slot_clear_failed`); `LockOperationError` (`LockDisconnected`,
  `LockOperationFailed`) and `LockBusy` are no answer. A `LockOperationBlocked` message is
  shown in the repair. Any other exception is a provider bug and charges a try every time.
- No answer three times in a row backs the lock off (60 s up to 30 min); twelve marks it
  offline with a `lock_offline` repair.

### Key Design Patterns

- **Dual storage**: Config entries use both `data` and `options` fields; active config in
  `data`, options flow updates go to `options`, then merged back to `data`
- **One owner per lock**: every exchange goes through the lock's worker queue. A provider
  that starts an exchange on its own (an event's follow-up read) runs it through
  `coordinator.async_in_turn`. `_sequence_lock` remains only for a provider's own
  multi-step sequences (Schlage, Akuvox)
- **Callbacks and signals**: `EntityCallbackRegistry` drives dynamic entity management;
  each lock's worker announces changes with one dispatcher signal (`signal_lock_changed`)
- **Reauth flow**: Automatically triggered if configured lock entities are removed

### Lock Codes Card - Slot State Logic

The `lcm-lock-codes` card displays lock slot states with the following data model:

**Data Fields from Websocket:**

| Field | Source | Description |
| ----- | ------ | ----------- |
| `code` / `code_length` | Lock's worker | Code on the lock, or a `SlotCode` label (`empty`, `unreadable_code`, `unanswered`, `disabled_on_lock`, `checking`, `not_reported`) |
| `configured_code` / `configured_code_length` | LCM text entities | Desired code from LCM config |
| `managed` | LCM config entries | Whether LCM manages this slot (authoritative field) |
| `name` | LCM text entities | Slot name configured in LCM |
| `active` | LCM binary sensor | True if enabled + conditions met, False if blocked |
| `enabled` | LCM switch entity | True if user enabled the slot, False if disabled |

**Frontend State Decision Table (for managed slots):**

| `active` | `enabled` | Result | UI Treatment |
| -------- | --------- | ------ | ------------ |
| true | true | Active | Blue solid border, "Active" badge |
| false | true | Inactive | Blue dotted border, "Inactive" badge (conditions blocking) |
| false | false | Disabled | Blue dotted border, "Disabled" badge (user disabled) |
| undefined | undefined | Fallback | Uses `code` presence to determine Active vs Inactive |

**Unmanaged Slots:** Only have Active (has code) or Inactive (no code) states. They appear
with gray borders and "Unmanaged" badge.

**Key Insight:** The `managed` field (from config entries) determines LCM management status.
The `configured_code` field indicates whether a PIN is configured, but a slot can be managed
even without a configured code if the PIN text entity is empty.

## Development Commands

### Setup

```bash
scripts/setup
```

Creates Python 3.14 venv, installs dependencies with uv, sets up git hooks
(preferring `prek` with fallback to uv-managed `pre-commit`), and installs
Node dependencies.

### Testing

**Python tests:**

```bash
pytest                          # Run all tests
pytest tests/test_init.py      # Run specific test file
pytest -k test_name            # Run tests matching pattern
pytest --cov                   # Run with coverage
```

**TypeScript tests:**

```bash
yarn test                       # Run all frontend tests (vitest)
yarn test:watch                 # Watch mode
yarn test:coverage              # Run with coverage
```

### Linting

```bash
prek run --all-files           # Run all linters (preferred over pre-commit)
ruff check                     # Python linting
ruff format                    # Python formatting
yarn lint                      # TypeScript/JavaScript linting
yarn lint:fix                  # Auto-fix JS/TS issues
```

### Building

```bash
yarn build                     # Build frontend strategy module
yarn watch                     # Watch mode for development
```

**Important:** After modifying any TypeScript files in `ts/`, the JavaScript
bundle must be rebuilt before testing in the browser. Use `yarn watch` during
development for automatic rebuilds, or run `yarn build` manually. The compiled
output is `custom_components/lock_code_manager/www/generated/lock-code-manager.js`.
If you test with stale JavaScript, the Lovelace strategy will fail with:
`Timeout waiting for strategy element ll-strategy-dashboard-lock-code-manager to be registered`

## Code Style

- Python: Ruff for linting/formatting (line length: 88)
- Import order: future → stdlib → third-party → homeassistant → first-party → local
- **No inline/local imports** — always import at the top of the file. Restructure modules to avoid circular dependencies.
- **No `nonlocal`/`global`** — use mutable containers or restructure instead.
- Type annotations: Keep older style (`dict[str, Any]` not `dict[str, Any] | None` where possible)
- Docstrings: Required for all public functions. Prefer prose explaining intent over mechanical
  re-statements of parameters. Do NOT add Google-style `Args:`/`Returns:` sections — type
  annotations already convey type/shape. Mention parameters and return values inline only when
  their semantics are non-obvious.
- **Spell out acronyms** in code comments instead of using abbreviations.
- **Prefer idiomatic Python** — comprehensions over loops, `next()` over `for`/`break`, concise expressions.
- Async: Prefer async/await; use `hass.async_add_executor_job()` to wrap sync code

## Testing Notes

- Uses `pytest-homeassistant-custom-component` for HA test utilities
- `tests/conftest.py`: Shared fixtures (`mock_lock_config_entry`, `lock_code_manager_config_entry`)
- `tests/common.py`: `MockLCMLock` (in-memory provider), mock entities, test constants
  - Fault knobs on `MockLCMLock` (`ignore`, `refuse`, `fail`, `hang`, `drop_reply`, `slow`),
    each targeting an operation and slot and returning a `LockFault` to clear. Besides `set`,
    `clear` and `get`, the `setup` and `probe` (device availability check) operations can be
    faulted by name. `when_mock_lock_built` acts on each mock lock as an entry builds it, for
    faults that must be in place before setup or codes a lock keeps across a restart.
    `attempt_times` records each set, clear or read asking the device availability check --
    the only trace an attempt on an offline lock leaves -- and leaves out the connection
    check's asks; `noop_set_usercode` records sets that changed nothing, and `call_order`
    the order of everything `service_calls` records. `async_advance_across_deadline`
    checks a wait is held a tick before its deadline and let go a tick after
  - `MockLCMLock.capabilities` is what the mock reports it can hold
    (`PIN_CAPABILITIES`, `NO_PIN_CAPABILITIES`). Setting it makes setup run its
    capability check, which BaseLock runs for native-user providers only, and
    sizes deadlines as for one (three exchanges per write, managed slots plus
    one per clear); the mock's writes and reads stay slot-level. BaseLock
    caches the first answer any caller gets, and only setup's check drops a
    failing one: a fix after a failed check is seen at the lock integration's
    next reload only if nothing else asked in between. `None`, the default,
    keeps the mock slot-only.
    `current_entry` builds an entry already in the current shape, so setting it
    up migrates nothing -- a migration reads the lock through an instance of its
    own, which a test watching what an entry builds or reads would see too
  - Time helpers for the `freezer` fixture: `async_advance_sync_ticks` and `async_tick_until`
    move both clocks in 2 s steps, stopping at each timer on the way (polls, pauses,
    backoff); `async_advance_clock` moves them any distance, for deadlines and the stop grace.
    `async_trigger_sync_tick` asks the slot's lock worker to sync it now. Prefer these to
    `async_advance_time` (fires timers but moves neither clock) in new tests
  - `MockLCMPushLock.subscribe_error`, when set, is raised by every push subscription
    attempt instead of subscribing. The push lock's own attributes are set before
    `when_mock_lock_built` acts run, so an act can set it
  - A `MockLCMPushLock` subscription is a real listener: `async_fire_mock_lock_use`
    reports a code use the way the lock's integration would, and reaches every
    subscription still registered -- one an instance failed to remove included
  - `async_setup_mock_lock_entry(hass)` sets up the mock lock integration (the two lock
    entities and calendars) that the `mock_lock_config_entry` fixture provides, for a
    test that builds its own Home Assistant per example. Each setup builds fresh
    entities, so the entry can be reloaded; calendars keep their events, and
    `hass.data[MOCK_CALENDARS_KEY]` holds the current ones. The `mock_calendars`
    fixture looks them up on every use, so it follows a reload
  - The clock helpers never spend a step while executor threads have work in flight:
    the autouse `counted_executor_work` fixture counts what each loop hands
    `run_in_executor` until the result is back on the loop, and
    `async_step_clock_until` waits that out in real time instead, so a step budget
    counts only waits on the clock, whatever the machine's speed. Work running past
    `EXECUTOR_WORK_CAP_SECONDS` (60) fails as `ExecutorWorkStuck`, not as the code
    under test hanging
  - `@pytest.mark.paced` keeps the providers' real delay between lock operations; such a
    test settles with `settle=False`, since settling only steps a fraction of a second
  - Settling skips the tasks registered in `LIFELONG_TASKS`: ones an integration under test
    runs for its whole life, like the Z-Wave JS client's listen loop, which its fixture
    registers. So the provider end-to-end harnesses can use these helpers too
- `tests/test_harness.py`: tests for the helpers and knobs above
- `tests/providers/helpers.py`: Shared test mixins for service-based providers
- `tests/providers/zwave_js/fake_node.py`: `FakeZWaveLock`, a stateful Z-Wave JS lock node
  faked where Home Assistant's client meets the server, so zwave-js-server-python, Home
  Assistant's `lock_helpers` and the provider run for real above it. It answers the node's
  `endpoint.access_control.*` commands (and User Code `getUsersCount`) the way node-zwave-js's
  `AccessControl.ts` does, and raises the node events the server forwards, in the driver's
  order. Every behavior cites the driver, server, client or Home Assistant line it follows
  (the module docstring pins the versions read). It keeps the lock's memory (`holds`, `codes`,
  `uc_slots`, `users`, `credentials`) apart from the driver's value cache (`cached`): cached
  reads answer from the cache, and only an exchange changes it -- a write, a fresh read, a report.
  - Fixtures (`tests/providers/zwave_js/conftest.py`): `fake_zwave_lock` (the node the
    `lock_schlage_be469_state` dump describes; `fake_zwave_lock_options` passes keyword
    arguments), `fake_zwave_lock_entry` (an entry on it, slots 1 and 2, without the class-wide
    mocks), `fake_e2e_lock` (that entry's provider). Override `lock_schlage_be469_state` with
    `user_credential_lock_state` (a User Credential CC node -- synthesized, as no such dump
    exists in Home Assistant core or the Z-Wave JS repos) or `supervised_lock_state` (adds
    Supervision CC). `mock_access_control`/`mock_lock_helpers` stay for the method-level tests
    built on canned answers; prefer the fake for anything that writes and then reads.
  - User Code CC: a report persists status then code, each raising a value event even when
    unchanged; no unified credential event (the provider's report shim hears it). An
    unsupervised write is read back (version 1 accepts an asterisk per digit) then announced
    with `credential added`/`modified` carrying the code written; a supervised success persists
    the code unread; a refused or unconfirmed write answers `Error_Unknown`. A clear reads back,
    resets the slot's cached values, and announces only a code the cache held. A Get whose
    report never arrives answers nothing, not an error. Out-of-range users and slots raise
    `FailedZWaveCommand` 322 before any traffic.
  - Lock knobs: `supported_users`/`interviewed` (capacity, and whether the interview cached it:
    `reinterview()` or the provider's `getUsersCount` recovery caches it later), `code_display`
    (`clear`, `masked`, `withheld`), `supervision` (Sets are supervised only on a dump advertising
    Supervision CC; there it picks the reported outcome -- Success by default, Working, Fail,
    NoSupport, after which the driver resends unsupervised -- and it is refused elsewhere),
    `driver_before_15_25_2` (the driver before zwave-js#8927: a supervised success caches
    nothing and a supervised failure is not read back -- what the provider's post-write
    reconcile read is for),
    `rejects_duplicates`/`duplicate_parameters` (refuse a code another slot holds and send
    Access Control notification 15 -- User Code CC cannot report it in-band), `codes` (what the
    lock holds when the dump caches asterisks). User Credential CC: `max_users`, `pin_slots`,
    `max_user_name_length`, `max_credential_hash_length` (above 0: no data read back);
    duplicates are rejected in-band, a Delete removes only a credential its user owns (yet is
    reported deleted), and fresh reads follow the driver's cursor walk.
  - The lock acting on its own: `program(slot, code)` (keypad change, reported),
    `send_user_code_report(slot, status, code)` (any report, stale ones included), `report`,
    `notify_access_control`, `keypad_unlock`, `mark_dead`/`mark_alive`, `emit_statistics`
    (counters also move per exchange: `commandsTX`, `commandsRX`, `commandsDroppedTX`,
    `timeoutResponse`).
  - Faults, named after `MockLCMLock`'s (ignore and refuse only on `set`/`clear`) and returning
    a `NodeFault` (`times`, `hits`, `held`, `async_wait_held`, context manager), act on radio
    exchanges -- `set`, `clear`, `get`
    (any Get: fresh read, walk, verification read, and the users-count Get as slot 0 --
    `drop_reply("get", 0)` is a lock asleep through its interview, and since every User Code
    walk asks the count first, it also empties every walk, as the driver does),
    `user`: `ignore` (acknowledged, not
    applied; a supervised one reports Success anyway), `refuse` (not applied; supervised: Fail),
    `fail` (never acknowledged: `FailedZWaveCommand`, 204 by default), `hang`, `slow`,
    `drop_reply` (applied, but the report never arrives -- on a set, both the Supervision report
    and the verification read: the #1397 shape). The teardown hook releases them like
    `LockFault`s. `exchanges`, `sets`, `clears` and `commands` record what happened; each
    exchange carries `at`, the monotonic time the lock saw it (for pacing, with `paced`).
  - `test_node_harness.py` self-tests each behavior against the driver, and end to end through
    a loaded entry.

- `tests/providers/matter/fake_node.py`: `FakeMatterLock`, a stateful Matter door lock faked
  at the Matter client's `send_command` (the websocket's place), so `MatterClient.send_device_command`
  and its `dataclass_to_dict` encoding, Home Assistant's Matter `lock_helpers` and the provider
  run for real above it. It answers the Door Lock cluster's `GetUser`, `SetUser`, `ClearUser`,
  `SetCredential`, `GetCredentialStatus` and `ClearCredential` the way the Matter SDK's
  door-lock server does (connectedhomeip v1.4.2.0, cited by line), and delivers `LockOperation`
  and `LockUserChange` events through the client's own subscriber filters.
  - Fixtures (`tests/providers/matter/conftest.py`): `fake_matter_lock` (the real Secuyou dump
    from Home Assistant core, which manages users: ten users, ten PINs, three per user, PINs of
    six to eight digits; `fake_matter_lock_options` passes keyword arguments),
    `fake_matter_lock_entity`, `fake_matter_lock_entry` (an entry on it, slots 1 and 2,
    `FAKE_MATTER_SLOTS`), `fake_e2e_matter_lock` (that entry's provider).
    `matter_mock_helpers` stays for the method-level tests built on canned answers; prefer the
    fake for anything that writes and then reads.
  - Capacities come from the node's Door Lock attributes. `max_user_name` is the longest name
    the lock stores (ten, the SDK's default): the provider's canonical `lcm:<slot>:<name>` tag
    is longer than that, so on the SDK's default the provider falls back to its compact tag.
  - Users: an empty index answers no user fields and the next occupied index; adding to an
    occupied index fails; modifying needs an occupied index, and only the creating fabric may
    rename; clearing a user clears its credentials.
  - Credentials, answered in band: Duplicate (another index holds the PIN), Occupied (adding
    over one), InvalidField (index or length out of range, a user that is not there or not this
    fabric's), ResourceExhausted (past the per-user limit). With no user index a user is created
    at the first free index; clearing a user's only PIN clears the user.
  - Every remote write is logged as a `LockUserChange` naming Home Assistant's fabric, as the SDK
    logs it; `program(user, pin)` changes the lock as the keypad or another admin would (no
    fabric), `keypad_unlock(credential_index)` logs a `LockOperation`, and `set_available(bool)`
    reports the node unreachable or back as the server's `node_updated` (entities follow it).
  - Faults, named as the other fakes' and returning a `NodeFault` (`times`, `hits`, `held`,
    `async_wait_held`, context manager), act on a command and optionally one index: `ignore`
    (answered as done, nothing changed), `refuse` (`SetCredential` in band with a `DlStatus`,
    other writes by Interaction Model status), `fail` (`NodeNotResolving` by default), `hang`,
    `slow`. Errors are the server Home Assistant uses (matterjs-server): a device rejection
    reaches the client as `UnknownError` with matter.js's `StatusResponseError` text
    (`Invalid command (code 133)`, `Failure (code 1; cluster code 3)`), an unreachable node as
    `NodeNotResolving("Node 42 is not resolving")`; identical in-flight commands reach the lock
    once, as the server coalesces them.
    `commands` records every command answered.
  - `test_node_harness.py` self-tests each behavior, and end to end through a loaded entry.

- `tests/providers/schlage/fake_service.py`: `FakeSchlageCloud`, the Schlage cloud's access codes
  behind `schlage.get_codes`/`add_code`/`delete_code`, answered as Home Assistant core's Schlage
  lock entity does (name or code already there refused with core's own message, a missing name
  deleted as a success). For `read_lag` seconds after a write, refreshes answer the codes from
  before it, as the cloud did in #1083; `time_out(service)` raises the raw `requests` `ReadTimeout` core
  lets through. Fixture `fake_schlage_cloud` (async: it caches core's Schlage exception
  translations, so errors read as they do in production).
- `tests/providers/akuvox/fake_service.py`: `FakeAkuvoxDevice`, an Akuvox device's users behind
  the `local_akuvox` integration's `list_users`/`add_user`/`modify_user`/`delete_user`, answered
  as that integration does (PIN 4-8 digits, unknown or cloud-provisioned users refused, every
  `pylocal-akuvox` `User` field listed; local users marked as A08S/E18C firmware marks them).
  `ignore(service)` acknowledges a write without applying it. Fixture `fake_akuvox_device`.
  Both knobs take `times` and return a `ServiceFault` (`tests/providers/service_fault.py`:
  `times`, `hits`, `clear()`, context manager), shaped like the other fakes' faults.
  `refuse_pin(pin)` makes Akuvox's `add_user` answer that PIN with the integration's device error,
  as firmware refusing a PIN another user holds would. Neither
  fixture can be combined with the canned `*_mock_services` one: both answer the same services.
  Both fakes have self-tests in `test_fake_service.py`.

- `tests/providers/zigbee2mqtt/fake_bridge.py`: `FakeZ2MBridge`, Zigbee2MQTT and the lock behind
  it, on the Home Assistant MQTT client mock: the provider's `/set` and `/get` publishes reach it,
  and what Zigbee2MQTT publishes back is fired on the device topic (zigbee2mqtt `344b6e03` and
  zigbee-herdsman-converters `3cdf1b86`, cited by line). Writes change the lock's PIN table and
  publish nothing; reads are answered only in `users` (`lock_pin_code_response`, `pin_code` only
  with `expose_pin`); every publish is the whole cached state, merged deeply, so a users entry
  is republished as last read and a key a new answer leaves out keeps its old value; `action`
  and `action_*` are never cached. Knobs: `set_user` (keypad change, no event), `report`
  (an attribute report: the cache republished), `report_after_write`, `operation_event`,
  `programming_event`, `silent` (reads never answered; writes land), `expose_pin`, `pin_code_count`
  (default the Yale lock extend's 250). Fixture `fake_z2m_bridge` (default topic); self-tests
  in `test_fake_bridge.py`.

### Test philosophy

**Mock at the boundary, not the code under test.** Let as much end-to-end code run as possible.

- **Provider tests**: Mock the external integration (Z-Wave client, Matter client), not LCM's own
  `async_call_service`. Service calls should flow through the real HA service layer to the mock client.
- **Integration tests**: Use `mock_lock_config_entry` (creates prerequisite lock entities) and
  `lock_code_manager_config_entry` (runs real `async_setup_entry`). Don't manually inject `hass.data`.
- **Pure-logic tests** (e.g., `calculate_in_sync`): Fine to use mocks since they test isolated logic.

### Test structure

Provider tests live in dedicated modules mirroring the integration's test patterns:

Each provider package has a `conftest.py`, a `test_provider.py`, and a
`test_e2e.py`; anything further is split by subject.

```text
tests/providers/
  akuvox/          # + fake_service, test_fake_service
  matter/          # + helpers, fixtures/, fake_node, test_node_harness,
                   #   test_sdk_exception_translation
  schlage/         # + fake_service, test_fake_service
  virtual/
  zha/             # + helpers (Zigbee frames, quirk-style clusters)
  zigbee2mqtt/     # + fake_bridge, test_fake_bridge, test_payload, test_topic_resolution
  zwave_js/        # + helpers, fixtures/, fake_node, test_events, test_node_harness
  zwave_js_ui/     # + test_api, test_borrowed, test_payload, test_push,
                   #   test_topic_resolution
  helpers.py       # Shared test mixins (ServiceProviderConnectionTests, etc.)
  service_fault.py # ServiceFault, the service fakes' fault
  test_base.py, test_dispatch.py, test_seam.py, test_util.py
```

### Test conventions

- **Minimize caplog text assertions** — prefer checking parameters or structured data.
- Provider `async_setup` must be idempotent — tests verify it can be called multiple times.
- Don't use `MagicMock()` for coordinators or workers when the real one can be used.

## Property-based testing

Two complementary layers exist alongside the example-based suites:

- **Python (Hypothesis)** — `tests/properties/` runs as part of the normal
  `pytest tests/` invocation. The default `dev` profile uses 15 examples to
  keep the suite fast; CI sets `HYPOTHESIS_PROFILE=ci` (200 examples). Run
  the deep profile locally with `HYPOTHESIS_PROFILE=ci pytest tests/properties/`.
  `test_credential_machine.py` is a stateful machine driving the real
  BaseLock write orchestration against `MockLCMLock`.
  `test_shared_lock_machine.py` is a stateful machine over entries sharing
  locks, driven only through Home Assistant (config entries, the options
  flow, startup, the locks' own integration) and checked through entity
  state and the mock locks' records. Each example builds its own Home
  Assistant on its own loop and frozen clock (Hypothesis keeps the real
  one). It caps examples at 20 steps and reports one bug; most failures
  shrink in seconds, the slowest seen took about a minute.
  Every step is bounded in its own loop by steps of the frozen clock, and
  fails as `ExampleHung`, the same on every replay, so a hang shrinks like
  any other failure, in seconds. Teardown is bounded, drains the loop, and
  always takes the example's Home Assistant off the harness's running list,
  so the session goes on. A synchronous spin that never yields is bounded by
  nothing in the machine: only the test's pytest timeout catches it, and
  Hypothesis then reports it unshrunk. That timeout is raised to 420
  seconds, past Hypothesis's own cutoff for a slow shrink, as a last-resort
  backstop only: the suite's 60 seconds could cut a shrink short, and a
  timeout firing inside an example is taken by Hypothesis for that example's
  failure and reported as flaky.
- **TypeScript (Bombadil)** — `ts/pbt/` contains a browser harness that mounts
  the built cards with a scripted mock `hass` plus a chaos panel, and a
  Bombadil spec (`ts/pbt/spec.ts`) with temporal-logic properties (no PIN leaks
  from masked cards, chip counts match the model, pushed data eventually
  renders). Run locally with `yarn test:pbt` (env knobs:
  `BOMBADIL_TIME_LIMIT`, `BOMBADIL_HEADLESS=1`). CI runs it nightly and on
  manual dispatch (`bombadil.yml`), never on pull requests. Inspect
  violations with `yarn bombadil browser inspect pbt-output`.

Counterexample triage: a found counterexample is a deliverable. Either fix
the code, or — only for documented contract edges — narrow the strategy
with a comment citing the contract. Never silence one by rerunning.

## Adding Lock Provider Support

1. Create new file in `providers/` (e.g., `my_provider.py`)
2. Subclass `BaseLock` from `providers/_base.py`. A lock reached through an MQTT
   bridge subclasses `BaseMqttLock` from `providers/_mqtt.py` instead — it supplies
   the MQTT domain, the operational preamble, the sequential per-slot read, the
   resubscribe policy, and the poll cadences that transport implies.
3. Implement required abstract methods:
   - `domain` property: return integration domain string
   - `async_is_integration_connected()`: check if integration is connected. Raises
     `LockCodeManagerError` by default if `lock_config_entry` is None — providers
     without a config entry (e.g., virtual) must override.
   - `async_get_users(slots=None)`: read the lock's users and their credentials.
     `BaseLock` projects these to the slot→credential mapping the rest of the
     integration uses, so most providers implement this rather than
     `async_get_usercodes()`.
     - `slots` names which credential indices to answer about; `None` means the
       slots this integration manages. A provider that reads the whole lock in
       one call may ignore it. **A provider that must ask the lock about one
       index at a time MUST honour it** — a caller widening the question
       otherwise walks the lock's entire advertised capacity, one round trip at
       a time, holding the lock's turn throughout.
     - The result may exceed the scope; callers bound it themselves.
   - `async_set_credential()`: write one credential to the lock
   - `async_delete_credential()`: remove one credential. Returns whether
     anything actually changed — a delete that found nothing to delete is not
     evidence the slot is empty.

   `async_set_usercode()` / `async_clear_usercode()` are `BaseLock`
   orchestration on top of these, not provider methods: they own the
   create-the-user-first lifecycle and the readable-PIN checks. Do not
   override them.
4. Getting `SlotCredential` right is the whole job of a read. Three states, and
   the wrong one is a defect, not an inaccuracy:
   - `known(pin)` — the lock holds this code **and is accepting it**. Sync
     compares it against the configured PIN, so a code the lock is refusing
     must not be reported this way: the slot would read in sync while the door
     stays shut.
   - `empty()` — **confirmed cleared**. Only an explicit "nothing here" from
     the lock earns it. Sync treats it as proof a clear landed, and slot
     allocation treats the index as free to hand out, so guessing here
     overwrites a real credential on a real door.
   - `unreadable()` — something is there and its value cannot be trusted: a
     write-only code, a failed read, a status this provider cannot interpret,
     a slot the lock reports as disabled. This is the safe answer whenever the
     lock has not plainly said "free" or "here is the code I am accepting".

   When in doubt, `unreadable()`. It costs a user one slot number; the other
   two mistakes cost them the code on their door.
5. Optionally override `async_is_device_available()` to return `False` when the physical
   device is unresponsive (default returns `True`). Together with integration connectivity
   it decides `async_internal_is_reachable()`, which the connection check uses to
   resubscribe; set `check_available_before_exchange` to also refuse every exchange with
   an unavailable device.
6. Override `async_setup()` to register event listeners
7. Call `async_fire_code_slot_event()` when lock events indicate PIN usage
8. Add tests in `tests/providers/<provider>/` (`conftest.py`,
   `test_provider.py`, `test_e2e.py`)
9. Use `self.managed_slots` (property) to get managed slot numbers
10. Use `self.async_call_service()` for HA service calls — it wraps `HomeAssistantError`
   as `LockDisconnected` automatically

### Optional Provider Properties

| Property | Default | Description |
| -------- | ------- | ----------- |
| `usercode_scan_interval` | 1 minute | Polling interval for usercodes |
| `hard_refresh_interval` | None | Interval for full code refresh (detects out-of-band changes) |
| `connection_check_interval` | 30 seconds | Interval for connection state checks |
| `supports_push` | False | Enable push-based updates instead of polling |
| `supports_code_slot_events` | True | Whether lock reports which code slot was used. Diagnostics only; nothing gates on it |

### Push Support

If `supports_push` returns `True`, implement:

- `subscribe_push_updates()`: Subscribe to real-time updates and hand each slot
  observation to `self._confirm_slot(slot, SlotCredential)` (or
  `_push_credential_update`). Either reaches the lock's worker as a push. A push may
  come from a cache, so it never resets a slot's tries, and the worker ignores a
  provider's echo of the write it is making.
- `unsubscribe_push_updates()`: Clean up subscriptions

Push `SlotCredential` values, never raw strings — every consumer calls
`.is_present` / `.matches` on them.

### Writes, and how they are confirmed

The worker confirms every write by reading the slot back straight after it
(`sync/worker.py` `_write`). A provider never tracks pending writes, and the core
ignores the `WriteResult` from `async_set_credential`. What a provider must get
right is the read:

- **A read must never come from a cache.** A cached old value counts against a
  write that worked (#873, #1090). A provider whose `async_get_usercodes` can
  answer from a cache sets `fresh_read_is_hard_refresh = True`, and the worker
  reads through `async_hard_refresh_codes(slots)` instead.
- A cloud that keeps serving the table from before a write sets `read_lag`
  (seconds): until then a slot just written reads as silent and the write is
  trusted.
- A provider that can tell which slots the lock said nothing about reports them in
  `last_silent_slots`, and the ones it answered it cannot tell (#1581) in
  `last_cannot_tell_slots`. A slot found only under an untagged user goes in
  `last_foreign_slots`.
- `check_available_before_exchange` makes the adapter refuse, unsent, an exchange
  with a device the provider says is unavailable (Z-Wave queues commands for a dead
  node). `per_exchange_budget` sizes the time limit of a walk one exchange at a time.

## Important Constraints

- Slots are numeric (usually 1-100) but can be strings for some lock types
- Multiple config entries can share the same lock but not the same slot
- Entity unique IDs: `{entry_id}|{slot}|{entity_type}` (per-lock entities add `|{lock_entity_id}`)
- Lock device is linked to config entry via `dev_reg.async_update_device()`

## Websocket API

Commands in `websocket.py` for frontend communication:

| Command | Purpose |
| ------- | ------- |
| `lock_code_manager/get_config_entry_data` | Fetch config entry, entities, locks, and slots |
| `lock_code_manager/subscribe_code_slot` | Real-time subscription for slot card updates (includes `sync_status` per lock) |
| `lock_code_manager/subscribe_lock_codes` | Real-time subscription for lock codes card (includes `sync_status` when suspended) |
| `lock_code_manager/write_unmanaged_code` | Set a code on a slot LCM does not manage |
| `lock_code_manager/clear_unmanaged_code` | Clear a code on a slot LCM does not manage |
| `lock_code_manager/set_condition` | Add or edit a slot's condition entity |
| `lock_code_manager/clear_condition` | Remove a slot's condition entity |

## Frontend

### Slot Card (`custom:lcm-slot`)

Primary card for managing individual code slots. Features:

- **Inline editing**: Name and PIN editable directly in card
- **Condition management**: Add/edit/remove condition entity via dialog
- **Lock status**: Per-lock sync status, actual code on lock (masked/revealed)
- **Collapsible sections**: Conditions and lock status can be collapsed
- **Real-time updates**: WebSocket subscription for instant state changes

**Config options:**

| Option | Default | Description |
| ------ | ------- | ----------- |
| `slot` | required | Slot number to display |
| `config_entry_id` | - | Config entry ID (or use `config_entry_title`) |
| `code_display` | `masked_with_reveal` | `masked`, `unmasked`, or `masked_with_reveal` |
| `show_conditions` | true | Show conditions section |
| `show_lock_status` | true | Show lock status section |
| `show_lock_sync` | true | Show sync status per lock |
| `show_code_sensors` | true | Show actual codes on locks |
| `show_lock_count` | true | Show lock count badge |
| `collapsed_sections` | [] | Sections to collapse by default: `conditions`, `lock_status` |

### Lock Codes Card (`custom:lcm-lock-codes`)

Lock-centric view showing all slots on a single lock (managed and unmanaged).

**Config options:**

| Option | Default | Description |
| ------ | ------- | ----------- |
| `lock_entity_id` | required | Lock entity to display slots for |
| `code_display` | `masked_with_reveal` | `masked`, `unmasked`, or `masked_with_reveal` |
| `title` | - | Optional card title override |

### Lovelace Strategies

- `custom:lock-code-manager` - Dashboard strategy auto-generating views per config entry
- `custom:lock-code-manager` - View strategy for a single config entry
- `custom:lock-code-manager-slot` - Section strategy for individual slots
- `custom:lock-code-manager-lock` - Section strategy for lock codes sections

### Condition Entities

Slots can have condition entities that control when the PIN is active. The PIN is only active when
the condition entity state is `on` (for calendars, this means an event is in progress).

**Supported domains** (defined in `CONDITION_ENTITY_DOMAINS` in `const.py`):

| Domain | Active When | Use Case |
| ------ | ----------- | -------- |
| `calendar` | Event in progress | Time-based access via calendar events |
| `schedule` | Within scheduled time range | HA Schedule helper for recurring windows |
| `binary_sensor` | State is `on` | Custom logic via templates, presence, etc. |
| `switch` | State is `on` | Manual toggle control |
| `input_boolean` | State is `on` | Simple on/off control via UI |

**UI Features:**

- Calendar and schedule entities show next event details in the slot card
- Condition can be added/edited/removed via the slot card's condition dialog

**Excluded Integrations:**

Some integrations create entities in supported domains but have state semantics incompatible with
access control. These are blocked via `EXCLUDED_CONDITION_PLATFORMS` in `const.py`:

| Integration | Reason |
| ----------- | ------ |
| `scheduler` (scheduler-component) | `on` means "schedule enabled", not "access allowed now" |

Validation occurs in both `config_flow.py` (UI configuration) and `websocket.py` (slot card editing).
