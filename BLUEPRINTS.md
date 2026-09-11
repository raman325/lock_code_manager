# Blueprints

Lock Code Manager includes pre-built blueprints for common lock
management patterns. Each blueprint can be imported directly into
Home Assistant with one click.

See the [wiki](https://github.com/raman325/lock_code_manager/wiki/Blueprints)
for additional setup guides and examples.

## Table of Contents

1. Access Control
   - [Slot Usage Limiter](#slot-usage-limiter) *(automation)*
   - [Calendar Condition](#calendar-condition) *(template)*
   - [Date Range Condition](#date-range-condition) *(template)*
   - [Calendar PIN Setter](#calendar-pin-setter) *(automation)*
1. Lock Automation
   - [Auto Re-lock](#auto-re-lock) *(automation)*
   - [Lock on Door Close](#lock-on-door-close) *(automation)*
1. Notifications
   - [Slot Usage Notifier](#slot-usage-notifier) *(automation)*
   - [Credential Used](#credential-used) *(automation)*
1. Setup Helpers
   - [Condition Linker](#condition-linker) *(automation)*
1. Reference
   - [Finding a slot number](#finding-a-slot-number)
   - [Credential used entity attributes](#credential-used-entity-attributes)

---

## Access Control

### Slot Usage Limiter

Decrements an `input_number` helper each time a user's credential is
used. When the counter reaches 0, that user is automatically disabled.
Optionally resets the counter when the user is re-enabled.

- Set counter to **-1** for unlimited uses
- Set counter to **0** to disable on next use
- The optional lock filter matches the use's `target` — the lock that
  reported it, or whatever `lock_code_manager.use_credential` was told the
  credential acted on

- **Operations that spend a use** picks which recorded uses count, by what
  the device did with the credential: `unlock` and `unknown` by default,
  `lock` not

> **Breaking: the entity now records more than unlocks.** It used to record
> only unlocks; it records every use of the user's credential now, including
> a lock reporting which slot *locked* it and every use reported through
> `lock_code_manager.use_credential`. **Operations that spend a use** is what
> keeps that from changing your counters: leaving it at its default spends a
> use on `unlock` and `unknown` and not on `lock`, so a guest who unlocks
> with their PIN and locks up behind them with the same PIN spends one use
> rather than two — on a 1-use code, being polite would otherwise lock them
> out. Uses reported through `lock_code_manager.use_credential` are always
> `unknown`, since Lock Code Manager never actuates a lock and so never sees
> what happened next; clear `unknown` and those stop counting entirely. Slot
> Usage Notifier has no such filter and runs on every use, locking included;
> its `operation` variable says which happened.

[![Import Blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Framan325%2Flock_code_manager%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flock_code_manager%2Fslot_usage_limiter.yaml)

| Input | Description | Default |
| ----- | ----------- | ------- |
| Credential used event entity | The user's event entity, which fires when their credential is used | Required |
| Operations that spend a use | Which uses decrement the counter, by what the device did: `unlock`, `lock`, `unknown` | `unlock`, `unknown` |
| Locks (optional) | Only count uses whose target is one of these | All targets |
| Slot enabled switch | The user's Enabled switch, turned off when the counter runs out | Required |
| Uses counter | `input_number` helper tracking remaining uses | Required |
| Initial uses on re-enable | Number of uses to reset to when the user is re-enabled (0 = no reset) | 0 |
| Notification service (optional) | Service called when the user is disabled | None |

### Calendar Condition

Creates a template binary sensor that turns ON when a calendar
event is active and an optional condition template evaluates to
true. Assign the sensor as a user's condition entity to control when
their PIN is active.

- Filter by event title, description, or location using Jinja2 templates
- Supports any HA calendar integration (local, Google, CalDAV, etc.)
- Template variables: `message`, `description`, `location`, `start_time`,
  `end_time`, `all_day`, `slot_number`, `config_entry_title`, `name_state`

> **Breaking: `lock_entity_ids` is gone.** It came from the credential used
> event entity, which no longer lists the config entry's locks as its event
> types, and nothing else exposes a config entry's locks to a template.
> **A condition template that still references it fails quietly.** Home
> Assistant treats an undefined template variable as empty rather than as an
> error, so the binary sensor stays available, reads `off`, and the PIN never
> activates. The one signal is a log line — `Template variable warning:
> 'lock_entity_ids' is undefined when rendering ...` — so check the Home
> Assistant log after upgrading. Nothing can make that louder: the sensor
> cannot tell an undefined variable from a condition that is honestly false.
> There is no replacement and nothing worth stubbing it to. The list was
> identical for every slot in the config entry, so
> `{{ 'lock.front_door' in lock_entity_ids }}` could only ever be constantly
> true or constantly false: a condition entity gates a *user* across every
> lock in the entry, and never one lock. Delete the reference to fix the
> sensor.

[![Import Blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Framan325%2Flock_code_manager%2Fblob%2Fmain%2Fblueprints%2Ftemplate%2Flock_code_manager%2Fcalendar_condition.yaml)

| Input | Description | Default |
| ----- | ----------- | ------- |
| Lock Code Manager config entry | Config entry holding the user | Required |
| Slot number | The user's slot number (see [Finding a slot number](#finding-a-slot-number)) | Required |
| Calendar entity | Calendar to monitor | Required |
| Condition template | Jinja2 template to filter events | `{{ true }}` |

### Date Range Condition

Creates a template binary sensor that turns ON when the current
time is between two `input_datetime` helpers. Use for rental-style
access windows with specific check-in/check-out times.

- Create `input_datetime` helpers first (Settings > Helpers)
- Enable both "Date" and "Time" on each helper for date+time ranges
- All comparisons done in UTC for timezone safety

[![Import Blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Framan325%2Flock_code_manager%2Fblob%2Fmain%2Fblueprints%2Ftemplate%2Flock_code_manager%2Fdate_range_condition.yaml)

| Input | Description | Default |
| ----- | ----------- | ------- |
| Start date/time | `input_datetime` helper for access window start | Required |
| End date/time | `input_datetime` helper for access window end | Required |

### Calendar PIN Setter

Extracts a PIN from calendar event attributes using a Jinja2
template and sets it as a user's PIN. Optionally clears the PIN when
the event ends. Useful for automated guest access via shared
calendars.

- Extract PINs from event title, description, or location
- Supports optional notifications when PINs are set/cleared

[![Import Blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Framan325%2Flock_code_manager%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flock_code_manager%2Fcalendar_pin_setter.yaml)

| Input | Description | Default |
| ----- | ----------- | ------- |
| Lock Code Manager config entry | Config entry holding the user | Required |
| Slot number | The user whose PIN to set (see [Finding a slot number](#finding-a-slot-number)) | Required |
| Calendar entity | Calendar to monitor for events | Required |
| PIN template | Jinja2 template to extract the PIN from the event | Required |
| Clear PIN when event ends | Clear the PIN when the calendar event ends | `true` |
| Notification service (optional) | Service called when the PIN is set or cleared | None |

---

## Lock Automation

### Auto Re-lock

Automatically re-locks a lock after it has been unlocked for a
configurable amount of time. Supports separate day and night delays
based on the sun entity's state (sunrise/sunset).

- If the lock is locked before the timer expires, the lock is skipped
- Set night delay to 0 to use the same delay for both day and night
- Uses `mode: restart` so a new unlock resets the timer

[![Import Blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Framan325%2Flock_code_manager%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flock_code_manager%2Fauto_relock.yaml)

| Input | Description | Default |
| ----- | ----------- | ------- |
| Lock | Lock entity to auto-relock | Required |
| Day delay (minutes) | Minutes to wait before re-locking during the day | 5 |
| Night delay (minutes) | Minutes to wait at night (0 = use day delay) | 0 |

### Lock on Door Close

Automatically locks a lock when a door sensor detects the door
has closed while the lock is unlocked.

- Only locks if the lock is currently unlocked when door closes
- Optional delay to allow the door to fully close before locking
- Uses `mode: single` to prevent duplicate lock commands

[![Import Blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Framan325%2Flock_code_manager%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flock_code_manager%2Fdoor_lock_on_close.yaml)

| Input | Description | Default |
| ----- | ----------- | ------- |
| Lock | Lock entity to control | Required |
| Door sensor | Binary sensor (door class) for open/closed state | Required |
| Lock delay (seconds) | Seconds to wait after door closes before locking | 5 |

---

## Notifications

### Slot Usage Notifier

Runs actions when a user's credential is used on a lock. Use it to
send notifications, trigger scripts, or run any HA action.

- Template variables: `slot_name`, `slot_num`, `lock_name`, `timestamp`,
  `operation`, `credential_type`
- `lock_name` and the optional lock filter both read the use's `target` —
  the lock that reported it, or whatever `lock_code_manager.use_credential`
  was told the credential acted on
- Uses `mode: queued` to handle rapid successive uses
- **Breaking:** it now runs on every recorded use, locking by code
  included, where it used to run only on unlocks. `operation` says which
  happened; put it in your message, or skip a kind with a condition inside
  your actions

[![Import Blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Framan325%2Flock_code_manager%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flock_code_manager%2Fslot_usage_notifier.yaml)

| Input | Description | Default |
| ----- | ----------- | ------- |
| Credential used event entities | One or more users' event entities | Required |
| Locks (optional) | Only run for uses whose target is one of these | All targets |
| Actions | HA actions to run (notifications, scripts, etc.) | Required |

### Credential Used

Runs actions every time a credential is used, whether Lock Code
Manager saw it happen on a lock or was told about it through the
`lock_code_manager.use_credential` action.

- Template variables: `name`, `source`, `target`, `credential_type`,
  `operation`, `config_entry_id`, `config_entry_title`, `timestamp`
- Every filter is optional; leave them all empty to act on every use
- Uses `mode: queued` to handle rapid successive uses

Slot Usage Notifier and Slot Usage Limiter trigger on one user's
credential used *entity*, so each automation covers the users you point
it at. This blueprint triggers on the `lock_code_manager_credential_used`
event instead, so one automation covers every user in every
configuration, and its filters narrow from there. All three see the same
uses: whether Lock Code Manager watched a lock report one or was told
about it through `lock_code_manager.use_credential`, and whatever the
use was against.

[![Import Blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Framan325%2Flock_code_manager%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flock_code_manager%2Fcredential_used.yaml)

| Input | Description | Default |
| ----- | ----------- | ------- |
| Lock Code Manager config entry (optional) | Only run for uses in this configuration | All configurations |
| Users (optional) | Only run for these users, by name | All users |
| Sources (optional) | Only run for credentials entered on these entities | Any source |
| Targets (optional) | Only run for credentials used against these entities | Any target |
| Actions | HA actions to run (notifications, scripts, etc.) | Required |

---

## Setup Helpers

### Condition Linker

A one-shot automation that assigns a condition entity to a user via
the `lock_code_manager.set_condition` service. Run it once from
the Automations page, then delete or keep for reference.

- Uses a synthetic event trigger that never fires automatically
- Manually run from the Automations page (three-dot menu > Run)

[![Import Blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Framan325%2Flock_code_manager%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flock_code_manager%2Fcondition_linker.yaml)

| Input | Description | Default |
| ----- | ----------- | ------- |
| Lock Code Manager config entry | Config entry holding the user | Required |
| Name | The person to assign the condition to, exactly as they are named in the config entry | Required |
| Condition entity | Entity to use as the condition | Required |

---

## Finding a slot number

Lock Code Manager assigns each user a slot number and manages it for
you, so the configuration editor and the dashboard cards never show
it. Blueprints and services still take one, because a lock addresses
its credentials by position.

To find the number for a user, open any of that user's entities and
look at its `code_slot` attribute. In a template:

```jinja
{{ state_attr('text.raman_pin', 'code_slot') }}
```

---

## Credential used entity attributes

Every user has a `credential_used` event entity. Its state is the
timestamp of their last recorded use, and its attributes are what a
template or blueprint reads about that use:

| Attribute | Meaning |
| --------- | ------- |
| `event_type` | The kind of credential presented — `pin` today. It says *what* was used, never where |
| `event_types` | The kinds this user's uses can be: what Lock Code Manager manages, plus everything the configuration's locks advertise |
| `name` | The user whose credential was used |
| `target` | What the credential was used against — a lock that reported the use, or whatever `lock_code_manager.use_credential` was told it acted on |
| `source` | Where the credential was entered. Same entity as `target` when a lock observed the use itself |
| `credential_type` | The kind of credential presented, the same value as `event_type` |
| `operation` | What the device did with it: `unlock`, `lock`, or `unknown` when nothing reported which. Always `unknown` for a use reported through `lock_code_manager.use_credential` |
| `config_entry_id` | The Lock Code Manager configuration holding the user |
| `config_entry_title` | That configuration's title |
| `code_slot` | The user's slot number |
| `slot_field` | Always `credential_used` |

Filtering by credential kind is a template away: `event_type` (or
`credential_type`) is `pin` for a PIN and would be `rfid` for a card read at
a lock's own reader, so an automation can act on one kind without acting on
every other.

> **Breaking:** the entity used to record only unlocks; it now records every
> use of the user's credential, including a lock reporting which slot locked
> it, and says which in `operation`. Its `event_type` used to be the lock
> entity ID and is now the credential kind, so `event_types` is the kinds of
> credential rather than a list of locks. It also used to publish the older
> lock-shaped payload. Templates reading
> `event_type` as a lock must read `target` instead, and
> `lock_code_manager_config_entry_id`, `code_slot_name`, `action_text`,
> `notification_source`, `from`, `to`, `state`, `entity_id`, `device_id`,
> `extra_data` and `lock_config_entry_id` are gone from the entity, and gone
> entirely: the `lock_code_manager_lock_state_changed` bus event that used to
> carry them was removed in 6.0. What survives of them is on
> `lock_code_manager_credential_used` — `target` for the lock, `name` for the
> person, `operation` for what the lock did — and the target entity's own state
> for the rest.
