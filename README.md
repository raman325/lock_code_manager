# Lock Code Manager

> NOTE: This integration is in very early stages, so expect things to not work as expected.
> Feel free to open issues to report anything you find, but I would not recommend using
> this for production usage yet.

Lock Code Manager is a Home Assistant integration that allows you to more easily manage
your usercodes on your locks. Once you have configured it, the integration will set and
clear codes on your locks as needed depending on how you decide to configure them.

Features:

- Synchronize multiple locks with a single set of codes
- Optionally use a calendar entity to activate and deactivate a code
- Optionally define a maximum number of uses for a code before the code is disabled

Locks from the following integrations are currently supported:

- Z-Wave
- [Virtual](https://github.com/twrecked/hass-virtual) custom integration. See the
  [Wiki page on this integration](https://github.com/raman325/lock_code_manager/wiki/Virtual-integration)
  for more details on why it was built and how it works.

The code was written to make it (I think) easy to add support for locks in other
integrations. Check the [Wiki](https://github.com/raman325/lock_code_manager/wiki) if you
want to learn more about that and take a stab at it. Contributors welcome!

## Installation

The best way to install this integration is via HACS.

1. Set up your locks as entities to your Home Assistant instance through the corresponding
   integration (e.g. Z-Wave)
2. Add this repository as a custom integration repository in HACS
3. Go to Settings > Devices & Services > Add Integration
4. Select Lock Code Manager
5. Follow the prompts - additional information about the configuration options are
   available in the Wiki

## Learn More

The [Wiki](https://github.com/raman325/lock_code_manager/wiki) is a WIP but has some
content that might be helpful for you!

## UI & Dashboards

Lock Code Manager can generate Lovelace dashboards for managing and monitoring PINs. You
can use the dashboard strategy (full dashboard), the view strategy (single view for a
config entry), or the custom card directly.

See [this wiki article](https://github.com/raman325/lock_code_manager/wiki/Add-a-UI-for-lock-code-management#dashboard)
for more details.

### Dashboard Strategy

Use the dashboard strategy to build a full dashboard with one view per config entry, and
an optional "User Codes" view that shows lock codes cards across all managed locks.

| Option                     | Default              | Description                                                        |
| -------------------------- | -------------------- | ------------------------------------------------------------------ |
| `use_slot_cards`           | `true`               | Use streamlined slot cards instead of legacy entities cards        |
| `show_code_sensors`        | `true`               | Show code sensors in lock status section                           |
| `show_conditions`          | `true`               | Show conditions section in slot cards                              |
| `show_lock_status`         | `true`               | Show lock status section in slot cards                             |
| `show_lock_sync`           | `true`               | Show sync status per lock in lock status                           |
| `collapsed_sections`       | `[]`                 | Which sections start collapsed (empty = all expanded)              |
| `show_all_codes_for_locks` | `true`               | Add a "User Codes" view with cards showing all lock codes          |
| `code_display`             | `masked_with_reveal` | Code visibility mode for slot cards and lock codes cards           |

Example dashboard configuration:

```yaml
strategy:
  type: custom:lock-code-manager
  show_all_codes_for_locks: true
  code_display: masked_with_reveal
  show_lock_sync: true
```

### View Strategy

Use the view strategy when you want a single view for one config entry. If
`show_all_codes_for_locks` is true, lock codes cards are appended below the slot
cards within the same view (no extra view is created).

| Option                     | Default              | Description                                                       |
| -------------------------- | -------------------- | ----------------------------------------------------------------- |
| `config_entry_id`          | -                    | Config entry ID to render                                         |
| `config_entry_title`       | -                    | Config entry title to render (alternative to ID)                  |
| `use_slot_cards`           | `true`               | Use streamlined slot cards instead of legacy entities cards       |
| `show_code_sensors`        | `true`               | Show code sensors in lock status section                          |
| `show_conditions`          | `true`               | Show conditions section in slot cards                             |
| `show_lock_status`         | `true`               | Show lock status section in slot cards                            |
| `show_lock_sync`           | `true`               | Show sync status per lock in lock status                          |
| `collapsed_sections`       | `[]`                 | Which sections start collapsed (empty = all expanded)             |
| `show_all_codes_for_locks` | `true`               | Append lock codes cards below the slot cards                      |
| `code_display`             | `masked_with_reveal` | Code visibility mode for slot cards and lock codes cards          |

Example view configuration:

```yaml
strategy:
  type: custom:lock-code-manager
  config_entry_id: 1234567890abcdef
  show_all_codes_for_locks: false
  code_display: masked
```

### Slot Card

The `lcm-slot` displays a single code slot with inline editing, real-time WebSocket
updates, and collapsible sections for conditions and lock status. This is the default card
used by the strategies when `use_slot_cards: true` (the default).

| Option               | Required | Default              | Description                                                  |
| -------------------- | -------- | -------------------- | ------------------------------------------------------------ |
| `config_entry_id`    | Yes*     | -                    | Config entry ID for the LCM instance                         |
| `config_entry_title` | Yes*     | -                    | Config entry title (alternative to ID)                       |
| `slot`               | Yes      | -                    | Slot number to display                                       |
| `code_display`       | No       | `masked_with_reveal` | How to display codes: see modes below                        |
| `show_conditions`    | No       | `true`               | Show the conditions section                                  |
| `show_lock_status`   | No       | `true`               | Show the lock status section                                 |
| `show_code_sensors`  | No       | `true`               | Show code sensors in lock status                             |
| `show_lock_sync`     | No       | `true`               | Show sync status per lock                                    |
| `collapsed_sections` | No       | `[]`                 | Sections to collapse by default: `conditions`, `lock_status` |

*Either `config_entry_id` or `config_entry_title` is required, but not both.

Example card configuration:

```yaml
type: custom:lcm-slot
config_entry_id: 1234567890abcdef
slot: 1
code_display: masked_with_reveal
collapsed_sections:
  - lock_status
```

### Lock Codes Card

The `lcm-lock-codes` displays all code slots for a specific lock with status badges,
inline editing for unmanaged slots, and click-to-navigate for LCM-managed slots.

| Option           | Required | Default              | Description                                    |
| ---------------- | -------- | -------------------- | ---------------------------------------------- |
| `lock_entity_id` | Yes      | -                    | The entity ID of the lock to display codes for |
| `title`          | No       | Lock name            | Custom title for the card                      |
| `code_display`   | No       | `masked_with_reveal` | How to display codes: see modes below          |

Example card configuration:

```yaml
type: custom:lcm-lock-codes
lock_entity_id: lock.front_door
title: Front Door Codes
code_display: masked_with_reveal
```

**Code Display Modes:**

- `masked` - Codes are always hidden (shown as bullets)
- `unmasked` - Codes are always visible
- `masked_with_reveal` - Codes are masked but can be revealed with a toggle button (default)

## Inspiration

I spent some time contributing to [keymaster](https://github.com/FutureTense/keymaster),
and what I learned working on it, and the regular complaints users had about it generating
too many automations, entities, etc. led me to take a different approach. This isn't a
knock on `keymaster`, unfortunately a lot of what is built in this integration wasn't
possible for most of `keymaster`'s life. I briefly considered implementing this into
`keymaster` but:

1. `keymaster` is still a great solution that works as is, and is more feature rich than
   this integration will likely ever be.
2. `keymaster` is surprisingly simple under the hood because it makes Home Assistant do a
   lot of the heavy lifting for figuring out when to enable and disable a usercode. This
   integration, on the other hand, attempts to do all of the heavy lifting internally in
   code, which means it will generate less entities and automations but it is likely more
   fragile to changes in HA Core or even changes in the codebase.
3. It would be impossible to seamlessly migrate users from the current implementation of
   `keymaster` to this integration's implementation. Rewriting `keymaster` to do this
   would have been the equivalent of creating a new integration anyway, and since it's a
   separate integration, users have a choice of what implementation they want to use.
   Additionally, you can install the integrations side by side and slowly migrate your
   locks over in either direction as needed.

## Thanks

A big thank you to the other `keymaster` maintainers:

- @FutureTense
- @firstof9

As well as the person who created the base concept that `keymaster` evolved from:
@ptdalen
