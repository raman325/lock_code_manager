# Blueprints

Lock Code Manager includes pre-built blueprints for common lock
management patterns. Each blueprint can be imported directly into
Home Assistant with one click.

This page is the full reference for every blueprint: what it does, its
inputs, the template variables it offers, and examples. For combining
blueprints with each other and with Home Assistant's own condition logic,
see [Combining Blueprints and Condition
Logic](https://github.com/raman325/lock_code_manager/wiki/Blueprints#combining-blueprints-and-condition-logic)
on the wiki.

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

Decrements an `input_number` helper each time a user's PIN is used. When
the counter reaches 0, that user is automatically disabled. Optionally resets
the counter when the user is re-enabled.

- Set counter to **-1** for unlimited uses: the counter stays at -1 and the
  user is never disabled
- Set counter to **0** to disable on next use
- The counter is an ordinary `input_number`, so the remaining count shows
  wherever Home Assistant shows the helper, and you can reset it from the
  user interface or from another automation
- The optional lock filter matches the use's `target` — the lock that
  reported it, or whatever `lock_code_manager.use_credential` was told the
  credential acted on

- **Operations that spend a use** picks which recorded uses count, by what
  the device did with the credential: `unlock` and `unknown` by default,
  `lock` not
- Only a PIN use (`event_type` `pin`) spends a use. Disabling the user takes
  away the PIN Lock Code Manager manages, but a card, fingerprint or face
  enrolled on the lock itself keeps working, so counting those would spend a
  limit that cannot be enforced on them
- A state change that is not a use spends nothing: the entity appearing after
  a restart or reload, or coming back from `unavailable`
- Uses `mode: queued` (up to 10) so rapid successive uses each count

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
| Uses counter | `input_number` helper tracking remaining uses; give it a minimum of -1 so it can hold "unlimited" | Required |
| Initial uses on re-enable | Value the counter resets to each time the user is re-enabled: 0 = no reset, -1 = unlimited | 0 |
| Notification service (optional) | A `notify` entity, messaged when the counter runs out and the user is disabled | None |

To set it up:

1. Create an `input_number` helper (Settings → Devices & Services →
   Helpers) with a minimum of -1, a maximum of your desired limit, and a
   step of 1.
1. Import the blueprint with the button above.
1. Create an automation from it and pick the user's credential used event
   entity, their Enabled switch, and the helper you created.

### Calendar Condition

Creates a template binary sensor that is ON while a calendar event belongs to
one user. Assign the sensor as that user's condition entity to control when
their PIN is active. One shared calendar can gate many users: create one
sensor per user, each matching that user's events.

[![Import Blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Framan325%2Flock_code_manager%2Fblob%2Fmain%2Fblueprints%2Ftemplate%2Flock_code_manager%2Fcalendar_condition.yaml)

| Input | Description | Default |
| ----- | ----------- | ------- |
| Lock Code Manager config entry | Config entry holding the user | Required |
| Slot number | The user's slot number (see [Finding a slot number](#finding-a-slot-number)) | Required |
| Calendar entity | Calendar to monitor | Required |
| Match | Which events belong to the user (below) | Any event, for sensors from before Match existed |
| Match text | Text the event's title or description must also contain, ignoring case | None |
| Condition template (no longer used) | Leave as `not set` and choose a Match instead. A sensor that still has a template here is unavailable until it has a Match | `not set` |

| Match | The sensor is on while the current event… |
| ----- | ----------------------------------------- |
| Any event | is any event |
| User's name in the event title | has the user's name in its title |
| User's name in the event description | has the user's name in its description |
| "slot N" in the event description | says `slot 3` in its description, for the user on slot 3 (`slot 12` does not count as `slot 1`) |

- Names match as whole words and ignore case, so `Al` doesn't match
  `Alice`. A user the sensor can't find (no such slot, or
  their name entity unavailable while all the configuration's locks are)
  matches no event
- The sensor updates as soon as the calendar moves to another event or the
  user is renamed
- It is unavailable while the calendar entity is `unknown` or `unavailable`,
  and unknown if its template fails to render. Only `on` makes a PIN active,
  so a problem leaves the PIN inactive rather than active
- Free-form condition templates are not supported. For a rule these choices
  don't cover, make your own [template binary
  sensor](https://www.home-assistant.io/integrations/template/) and use it as
  the condition entity

> **Breaking: the condition template no longer works, replaced by Match and
> Match text.** A sensor set up before this version that never set a condition
> template keeps working as **Any event**. One that set its own template is
> unavailable, so its user's PIN stays inactive, until you choose a Match.
> Re-import the blueprint to get this version: Home Assistant keeps its own
> copy of an imported blueprint.

To set it up:

1. Import the blueprint with the button above.
1. Configure it with your Lock Code Manager config entry, the user's slot
   number, the calendar entity, and a Match.
1. Assign the binary sensor it creates as that user's condition entity.

### Date Range Condition

Creates a template binary sensor that turns ON when the current
time is between two `input_datetime` helpers. Use for rental-style
access windows with specific check-in/check-out times.

- Create `input_datetime` helpers first (Settings > Helpers)
- Enable both "Date" and "Time" on each helper for date+time ranges
- All comparisons done in UTC for timezone safety
- If either helper is `unknown` or `unavailable`, the sensor is unavailable,
  which blocks access

[![Import Blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Framan325%2Flock_code_manager%2Fblob%2Fmain%2Fblueprints%2Ftemplate%2Flock_code_manager%2Fdate_range_condition.yaml)

| Input | Description | Default |
| ----- | ----------- | ------- |
| Start date/time | `input_datetime` helper for access window start | Required |
| End date/time | `input_datetime` helper for access window end | Required |

To give a guest access for one stay:

1. Create two `input_datetime` helpers (Settings → Devices & Services →
   Helpers), for example `input_datetime.guest_start` and
   `input_datetime.guest_end`, with both "Date" and "Time" enabled on each.
1. Set the start to the guest's arrival and the end to their departure.
1. Import the blueprint with the button above and select both helpers.
1. Assign the binary sensor it creates as the guest's condition entity.

The `condition_helpers` dashboard property shows the two `input_datetime`
helpers inline on the user's card, so the dates can be changed without
leaving the dashboard. See [Displaying Condition Helper Controls
Inline](https://github.com/raman325/lock_code_manager/wiki/Using-Condition-Entities#displaying-condition-helper-controls-inline)
on the wiki.

### Calendar PIN Setter

Extracts a PIN from calendar event attributes using a Jinja2
template and sets it as a user's PIN. Optionally clears the PIN when
the event ends. Useful for automated guest access via shared
calendars.

- Extract PINs from event title, description, or location
- Supports optional notifications when PINs are set/cleared
- When the calendar turns on, the PIN template is rendered and, if the
  result is not empty, set as the user's PIN. An empty result leaves the PIN
  as it is
- When the calendar turns off and **Clear PIN when event ends** is on, the
  PIN is cleared
- If no user holds the slot number, nothing is set or cleared

[![Import Blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Framan325%2Flock_code_manager%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flock_code_manager%2Fcalendar_pin_setter.yaml)

| Input | Description | Default |
| ----- | ----------- | ------- |
| Lock Code Manager config entry | Config entry holding the user | Required |
| Slot number | The user whose PIN to set (see [Finding a slot number](#finding-a-slot-number)), as a number or a template | Required |
| Calendar entity | Calendar to monitor for events | Required |
| PIN template | Jinja2 template to extract the PIN from the event. An empty result skips setting the PIN | Required |
| Clear PIN when event ends | Clear the PIN when the calendar event ends | `true` |
| Notification service (optional) | A `notify` entity, messaged when the PIN is set or cleared | None |

**Slot number** takes a static number, or a template that reads it from the
event, e.g. `{{ description | regex_findall('slot (\\d+)') | first }}`.

The PIN template and the slot number template can use these variables:

| Variable | Meaning |
| -------- | ------- |
| `message` | The event's summary (title) |
| `description` | The event's description |
| `location` | The event's location |
| `start_time` | The event's start time |
| `end_time` | The event's end time |
| `all_day` | Whether the event is an all-day event |

Example PIN templates:

- Extract a 4-digit PIN from the description:
  `{{ description | regex_findall('\\b\\d{4}\\b') | first | default('') }}`
- Use the first 4 characters of the event title:
  `{{ message[:4] if message | length >= 4 else '' }}`

Write `\\b` and `\\d` with doubled backslashes, as above. A quoted string in a
template treats a backslash as the start of an escape, so `'\b'` becomes a
backspace character and the pattern never matches.

To set it up:

1. Import the blueprint with the button above.
1. Create an automation from it and configure it with your Lock Code Manager
   config entry, the calendar entity, the slot number, and a PIN template.

---

## Lock Automation

### Auto Re-lock

Automatically re-locks a lock after it has been unlocked for a
configurable amount of time. Supports separate day and night delays
based on the sun entity's state (sunrise/sunset).

- If the lock is locked before the timer expires, no lock command is sent
- Set night delay to 0 to use the same delay for both day and night
- Uses `mode: restart` so a new unlock resets the timer
- Night is when `sun.sun` is `below_horizon` at the moment the lock unlocks.
  Without the Sun integration, the night delay never applies

[![Import Blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Framan325%2Flock_code_manager%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flock_code_manager%2Fauto_relock.yaml)

| Input | Description | Default |
| ----- | ----------- | ------- |
| Lock | Lock entity to auto-relock | Required |
| Day delay (minutes) | Minutes to wait before re-locking during the day (1–60) | 5 |
| Night delay (minutes) | Minutes to wait at night, between sunset and sunrise (0–60; 0 = use day delay) | 0 |

### Lock on Door Close

Automatically locks a lock when a door sensor detects the door
has closed while the lock is unlocked.

- Only locks if the lock is currently unlocked when door closes
- Optional delay to allow the door to fully close before locking; the lock
  is checked again after the delay and only locked if it is still unlocked
- Uses `mode: single` to prevent duplicate lock commands

[![Import Blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Framan325%2Flock_code_manager%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flock_code_manager%2Fdoor_lock_on_close.yaml)

| Input | Description | Default |
| ----- | ----------- | ------- |
| Lock | Lock entity to control | Required |
| Door sensor | Binary sensor with the `door` device class: `on` when open, `off` when closed | Required |
| Lock delay (seconds) | Seconds to wait after door closes before locking (0–60; 0 = lock immediately) | 5 |

---

## Notifications

### Slot Usage Notifier

Runs actions when a user's credential is used on a lock. Use it to
send notifications, trigger scripts, or run any HA action.

- `lock_name` and the optional lock filter both read the use's `target` —
  the lock that reported it, or whatever `lock_code_manager.use_credential`
  was told the credential acted on
- A state change that is not a use runs nothing: the entity appearing after
  a restart or reload, or coming back from `unavailable`
- Uses `mode: queued` (up to 10) to handle rapid successive uses
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

Your actions can use these variables:

| Variable | Meaning |
| -------- | ------- |
| `slot_name` | The name of the user whose credential was used |
| `slot_num` | The user's slot number |
| `lock_name` | The friendly name of the use's target; its entity ID if Home Assistant has no state for it |
| `timestamp` | When the use was recorded, in local time (`YYYY-MM-DD HH:MM:SS`) |
| `operation` | What the device did with the credential: `unlock`, `lock`, or `unknown` when nothing reported which |
| `credential_type` | The kind of credential presented: `pin`, or `rfid`, `fingerprint` or `face` on a lock that reports which |

To set it up:

1. Import the blueprint with the button above.
1. Create an automation from it and select the credential used event entity
   of each user to watch (for example, "Raman Credential used").
1. Configure the actions to run when a credential is used.

### Credential Used

Runs actions every time a credential is used, whether Lock Code
Manager saw it happen on a lock or was told about it through the
`lock_code_manager.use_credential` action.

- Every filter is optional; leave them all empty to act on every use
- Uses `mode: queued` (up to 10) to handle rapid successive uses
- Use it for an external keypad opening something Lock Code Manager does not
  manage — a cover, a relay, an alarm panel. See [External
  Keypads](https://github.com/raman325/lock_code_manager/wiki/External-Keypads)
  on the wiki

Slot Usage Notifier and Slot Usage Limiter trigger on one user's
credential used *entity*, so each automation covers the users you point
it at. This blueprint triggers on the `lock_code_manager_credential_used`
event instead, so one automation covers every user in every
configuration, and its filters narrow from there. All three see the same
uses: whether Lock Code Manager watched a lock report one or was told
about it through `lock_code_manager.use_credential`, and whatever the
use was against. Reach for the other two when you want the focused,
per-user recipe; reach for this one when you want the whole picture.

[![Import Blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Framan325%2Flock_code_manager%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flock_code_manager%2Fcredential_used.yaml)

| Input | Description | Default |
| ----- | ----------- | ------- |
| Lock Code Manager config entry (optional) | Only run for uses in this configuration | All configurations |
| Users (optional) | Only run for these users, by name. Names match exactly, so update this when you rename a user | All users |
| Sources (optional) | Only run for credentials entered on these entities | Any source |
| Targets (optional) | Only run for credentials used against these entities | Any target |
| Actions | HA actions to run (notifications, scripts, etc.) | Required |

Your actions can use these variables:

| Variable | Meaning |
| -------- | ------- |
| `name` | The name of the user whose credential was used |
| `source` | The entity ID where the credential was entered |
| `target` | The entity ID it was used against |
| `credential_type` | The kind of credential presented: `pin`, or `rfid`, `fingerprint` or `face` on a lock that reports which |
| `operation` | What the device did with it: `unlock`, `lock`, or `unknown` when nothing reported which |
| `config_entry_id` | The ID of the Lock Code Manager configuration |
| `config_entry_title` | The title of that configuration |
| `timestamp` | When the credential was used, in local time (`YYYY-MM-DD HH:MM:SS`) |

---

## Setup Helpers

### Condition Linker

A one-shot automation that assigns a condition entity to a user via
the `lock_code_manager.set_condition` service. Run it once from
the Automations page, then delete or keep for reference.

- Uses a synthetic event trigger that never fires automatically
- Manually run from the Automations page (three-dot menu > Run)
- **Breaking:** it takes the person's **Name**, not a slot number. An
  automation created from an earlier version has to be reconfigured with the
  name, or recreated, since it only runs once

[![Import Blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Framan325%2Flock_code_manager%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flock_code_manager%2Fcondition_linker.yaml)

| Input | Description | Default |
| ----- | ----------- | ------- |
| Lock Code Manager config entry | Config entry holding the user | Required |
| Name | The person to assign the condition to, as they are named in the config entry. Case and surrounding spaces are ignored | Required |
| Condition entity | Entity to use as the condition: a `binary_sensor`, `calendar`, `input_boolean`, `schedule` or `switch`. The person's PIN is active only while it is `on` | Required |

To set it up:

1. Import the blueprint with the button above.
1. Create an automation from it and configure it with your Lock Code Manager
   config entry, the person's name, and the condition entity.
1. Run the automation once from the Automations page (three-dot menu > Run).
1. The condition entity is now assigned to that person. Delete the
   automation or keep it for reference.

---

## Finding a slot number

Lock Code Manager assigns each user a slot number and manages it for
you, so the configuration editor and the dashboard cards never show
it. The [Calendar Condition](#calendar-condition) blueprint takes one to
say which user its sensor gates, and the [Calendar PIN
Setter](#calendar-pin-setter) takes one because the number can come from a
template over the calendar event. Every action, and every other blueprint,
addresses the person by name instead.

To find the number for a user, open any of that user's entities and
look at its `code_slot` attribute. In a template, for a user named Raman
in a configuration titled All locks:

```jinja
{{ state_attr('text.all_locks_raman_pin', 'code_slot') }}
```

The number is stable: renaming a user does not change it. It can change
only if you remove the user and add them again.

---

## Credential used entity attributes

Every user has a `credential_used` event entity. Its state is the
timestamp of their last recorded use, and its attributes are what a
template or blueprint reads about that use:

| Attribute | Meaning |
| --------- | ------- |
| `event_type` | The kind of credential presented — `pin`, or on a lock that reports which: `rfid` (Matter, ZHA, Zigbee2MQTT), `fingerprint` (Matter, and Zigbee2MQTT when the lock's converter names it) or `face` (Matter). It says *what* was used, never where. On a Zigbee or Z-Wave lock a use is a `pin` only when typed at the keypad or sent with the user's code; the thumb-turn, a remote command naming no user and a source the lock does not identify record no use |
| `event_types` | The kinds this user's uses can be: `pin`, plus every kind the configuration's locks can report. A use of a kind not listed here is not recorded on the entity, though `lock_code_manager_credential_used` still fires for it |
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
`credential_type`) is `pin` for a PIN and `rfid` for a card read at a lock's
own reader, so an automation can act on one kind without acting on every
other. The Slot Usage Limiter counts only `pin` uses, since disabling a user
cannot stop a card or fingerprint enrolled on the lock itself.

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
