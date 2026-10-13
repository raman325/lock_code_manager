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
  `async_is_integration_connected()`, `async_hard_refresh_codes()`
- Providers listen for lock-specific events and translate them to LCM events via `async_fire_code_slot_event()`
- `BaseLock` provides `managed_slots` property (aggregates slots across all config entries for the lock,
  disabled ones included; it is `slots_configured_on`)
  and `async_call_service()` helper (wraps HA service calls with `LockDisconnected` error handling)
- Config questions -- which entry's configuration lists this lock or slot -- go through the named
  questions in `domain/queries.py` (`entries_configuring`, `slots_configured_on`, `configured_owner`,
  `syncing_owner`, `another_enabled_entry_configures`). Each counts a different set of entries;
  ask the one you mean rather than filtering `async_entries` by hand. Live lock-instance ownership
  is a separate question and filters by load state on purpose: `_find_shared_lock_instance` and
  `_lock_instance_held_by_another_entry` in `__init__.py`, and `iter_loaded_lcm_entries` /
  `get_managed_locks` for the locks loaded entries hold

**Coordinator** (`coordinator.py`)

- `LockUsercodeUpdateCoordinator`: Home Assistant DataUpdateCoordinator that polls lock providers for usercode state
- Each lock instance has its own coordinator
- Default scan interval: 1 minute (configurable via `usercode_scan_interval` property)
- Supports push-based updates via `coordinator.push_update()` for providers with `supports_push = True`
- `slot_sync_mgrs_suspended` property / `suspend_slot_sync_mgrs()`: per-lock suspension flag that
  stops all sync managers for a lock when the circuit breaker trips or an unexpected error occurs.
  Cleared automatically on recovery via `_reset_backoff` (successful poll or push update).

**Main Module** (`__init__.py`)

- Entry point manages config entry lifecycle
- `async_update_listener()`: Core function handling dynamic entity creation/removal when config changes
- Uses dispatcher signals to notify entities of changes (e.g., `{DOMAIN}_{entry_id}_add_lock_slot`)
- Registers Lovelace strategy resource for dashboard UI

**Config Flow** (`config_flow.py`)

- Multi-step flow: select locks → configure slots → configure individual slot properties
- Validates slots aren't already configured across other config entries
- Supports YAML object mode for advanced slot configuration

### Entities

- `binary_sensor.py`: PIN active status (enabled + conditions met) and per-lock in-sync status
  with `sync_status` extra state attribute (`in_sync`, `out_of_sync`, `syncing`, `suspended`).
  `in_sync` is the aggregate over every managed credential; one `<type>_in_sync` sensor per
  credential type (`pin_in_sync` today) views a single credential and is disabled by default
- `sensor.py`: Per-lock slot PIN sensors showing current codes on each lock
- `text.py`: Name and PIN configuration entities
- `number.py`: Number of uses tracking (decrements on PIN use)
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
2. `async_update_listener()` creates coordinator and provider instances for each lock
3. Lock provider sets up listeners for lock-specific events (e.g., Z-Wave JS notification events)
4. Coordinator polls provider's `async_get_usercodes()` to keep state in sync (or receives push updates)
5. When slot config changes (name, PIN, enabled), sync calls
   `BaseLock.async_set_usercode()` / `async_clear_usercode()`, which drive the
   provider's credential primitives
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

### Sync State Machine (`sync.py`)

`SlotSyncManager` uses a `SyncState` enum with five states:

- **LOADING** → initial, waiting for entity states
- **IN_SYNC** → lock code matches desired state
- **OUT_OF_SYNC** → mismatch detected, queued for next tick
- **SYNCING** → sync operation in progress
- **SUSPENDED** → circuit breaker tripped or unexpected error, retries paused

Transitions: LOADING → IN_SYNC or OUT_OF_SYNC; IN_SYNC ↔ OUT_OF_SYNC; OUT_OF_SYNC �� SYNCING;
SYNCING → IN_SYNC, OUT_OF_SYNC (retry), or SUSPENDED; SUSPENDED → OUT_OF_SYNC (on recovery).

Error handling taxonomy:

- `CodeRejectedError`/`DuplicateCodeError` → disable slot (profile-wide, code is bad)
- `LockDisconnected`/`LockOperationFailed` → retry (OUT_OF_SYNC)
- Generic `Exception` → suspend lock (per-lock, creates `slot_suspended` repair issue)
- Circuit breaker (3 attempts in 5 minutes) → suspend lock

### Key Design Patterns

- **Dual storage**: Config entries use both `data` and `options` fields; active config in
  `data`, options flow updates go to `options`, then merged back to `data`
- **Internal methods**: Providers expose `async_internal_*` methods that wrap public
  methods with asyncio locks to prevent race conditions
- **Rate limiting**: `BaseLock._execute_rate_limited()` enforces 2-second minimum delay
  between operations and serializes all lock operations via `_aio_lock`
- **Dispatcher signals**: Heavily used for dynamic entity management without tight coupling
- **Reauth flow**: Automatically triggered if configured lock entities are removed

### Lock Codes Card - Slot State Logic

The `lcm-lock-codes` card displays lock slot states with the following data model:

**Data Fields from Websocket:**

| Field | Source | Description |
| ----- | ------ | ----------- |
| `code` / `code_length` | Lock coordinator | Actual code on the lock (current state) |
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

### Future: Slot Status Enum (TODO)

Currently, the coordinator only stores `{slot: code}` and we infer status from code presence.
Z-Wave locks provide richer status via `userIdStatus`:

- **Enabled**: Slot has active code
- **Available**: Slot can be used but is empty
- **Disabled**: Slot cannot be used (locked out by lock firmware)

**Planned Enhancement:**

1. Define generic `SlotStatus` enum in `const.py`:

   ```python
   class SlotStatus(StrEnum):
       ENABLED = "enabled"
       AVAILABLE = "available"
       DISABLED = "disabled"
   ```

2. Providers map their native status to this enum:
   - Z-Wave JS: `userIdStatus` → `SlotStatus`
   - Other providers: Infer from their equivalent states

3. Change coordinator data schema from `dict[int, str]` to `dict[int, SlotData]`

**Provider Guidance for Status Inference:**

| Provider State | Maps To |
| -------------- | ------- |
| Code exists on lock | `ENABLED` |
| Slot empty but usable | `AVAILABLE` |
| Slot programmatically disabled | `DISABLED` |
| Unknown/unsupported | Default to `AVAILABLE` if no code, `ENABLED` if code |

See `TODO.md` for implementation details.

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
    run the real scheduled sync tick by moving both clocks; `async_advance_clock` moves them
    any distance, for deadlines and the stop grace; `async_trip_slot_breaker` reaches
    a breaker through real failures. Prefer these to `async_trigger_sync_tick` (drives sync
    internals) and `async_advance_time` (fires timers but moves neither clock) in new tests
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
  `times`, `hits`, `clear()`, context manager), shaped like the other fakes' faults. Neither
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
- Don't use `MagicMock()` for coordinators when the real coordinator can be used.

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
       a time, holding the operation lock throughout.
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
   device is unresponsive (default returns `True`). Operations are gated on both
   integration connectivity and device availability.
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
  observation to `self._confirm_slot(slot, SlotCredential)`. That routes it
  through `coordinator.observe_push`, which resolves any write still pending on
  the slot before updating the data. Do not call `coordinator.push_update`
  from an event handler: it bypasses that resolution.
- `unsubscribe_push_updates()`: Clean up subscriptions

### Writes, and how they are confirmed

The coordinator owns a write from the moment it is sent until a read or a push
shows it on the lock (`record_write` / `observe_push` / `take_failed_write` in
`domain/coordinator.py`). A provider never tracks pending writes itself; it
only chooses what to return from `async_set_credential`:

```python
async def async_set_credential(self, ref: CredentialRef, value: str, ...) -> WriteResult:
    # ... perform the set operation ...
    await self._call_service_to_set_code(ref.slot, value)

    # A push provider pushes the value it wrote before returning CONFIRMED:
    # its own push is the lock's word, and the seam records nothing pending.
    self._push_credential_update(ref.slot, SlotCredential.known(value))
    return WriteResult.CONFIRMED
```

- `CONFIRMED` from a **push** provider: the lock accepted the write and the
  provider (or the driver's event) has pushed the value. Nothing is pending.
- `CONFIRMED` from a **polled** provider: the service accepted the write, but
  only a later read can vouch for it. The seam records it pending, unbelieved;
  the coordinator reads the lock back on its own schedule and the slot is in
  sync once the read shows it.
- `OPTIMISTIC`: the write most likely landed but the provider cannot say (for
  example an ambiguous driver result). The seam records it pending with the
  value believed, so the entity shows it at once, and the coordinator reads it
  back. A write the lock never shows is given up after `PENDING_WRITE_TTL` and
  charged to the slot breaker once.
- `NO_CHANGE`: nothing was written; nothing is recorded.

Push `SlotCredential` values, never raw strings — every consumer calls
`.is_present` / `.matches` on them. `_push_credential_update` is the helper that
wraps `coordinator.push_update` correctly.

**Why the push before CONFIRMED matters:** for Z-Wave the command is acknowledged
by the lock (via Supervision CC) but the JS value cache updates later. Without
the push, the coordinator would hold the stale pre-write value, the write would
be judged against it, and the slot would re-sync for nothing.

**Polled providers with synchronous caches** (like Virtual) need nothing special:
the coordinator's read straight after the write finds the new value.

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
| `lock_code_manager/set_lock_usercode` | Set/clear usercode on unmanaged slots |
| `lock_code_manager/update_slot_condition` | Add/edit/remove slot condition entity |

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
