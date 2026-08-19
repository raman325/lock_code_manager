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
1. Setup Helpers
   - [Condition Linker](#condition-linker) *(automation)*

---

## Access Control

### Slot Usage Limiter

Decrements an `input_number` helper each time a user's credential is
used. When the counter reaches 0, that user is automatically disabled.
Optionally resets the counter when the user is re-enabled.

- Set counter to **-1** for unlimited uses
- Set counter to **0** to disable on next use
- Requires a lock that reports credential use

[![Import Blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Framan325%2Flock_code_manager%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flock_code_manager%2Fslot_usage_limiter.yaml)

| Input | Description | Default |
| ----- | ----------- | ------- |
| Credential used event entity | The user's event entity, which fires when their credential is used | Required |
| Locks (optional) | Only count uses on these locks | All locks |
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

- Requires a lock that reports credential use
- Template variables: `slot_name`, `slot_num`, `lock_name`, `timestamp`
- Uses `mode: queued` to handle rapid successive uses

[![Import Blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Framan325%2Flock_code_manager%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flock_code_manager%2Fslot_usage_notifier.yaml)

| Input | Description | Default |
| ----- | ----------- | ------- |
| Credential used event entities | One or more users' event entities | Required |
| Locks (optional) | Only run for uses on these locks | All locks |
| Actions | HA actions to run (notifications, scripts, etc.) | Required |

---

## Setup Helpers

### Condition Linker

A one-shot automation that assigns a condition entity to a user via
the `lock_code_manager.set_slot_condition` service. Run it once from
the Automations page, then delete or keep for reference.

- Uses a synthetic event trigger that never fires automatically
- Manually run from the Automations page (three-dot menu > Run)

[![Import Blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Framan325%2Flock_code_manager%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flock_code_manager%2Fcondition_linker.yaml)

| Input | Description | Default |
| ----- | ----------- | ------- |
| Lock Code Manager config entry | Config entry holding the user | Required |
| Slot number | The user to assign the condition to (see [Finding a slot number](#finding-a-slot-number)) | Required |
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
