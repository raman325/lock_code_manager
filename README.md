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

## Add a UI for lock management

`Lock Code Manager` makes it easy for you to generate a UI for managing and monitoring
your PINs.

See [this wiki article](https://github.com/raman325/lock_code_manager/wiki/Add-a-UI-for-lock-code-management#dashboard)
for more details

### Dashboard Strategy Configuration

The dashboard strategy supports the following configuration options:

| Option                       | Default | Description                                               |
| ---------------------------- | ------- | --------------------------------------------------------- |
| `include_code_slot_sensors`  | `false` | Show code slot sensor entities in each slot's card        |
| `include_in_sync_sensors`    | `true`  | Show in-sync sensor entities in each slot's card          |
| `include_code_data_view`     | `false` | Add a "Lock Codes" view with cards showing all lock codes |

Example dashboard configuration:

```yaml
strategy:
  type: custom:lock-code-manager
  include_code_data_view: true
  include_in_sync_sensors: true
```

### Lock Code Data Card

The `lock-code-manager-lock-data` card displays all code slots for a specific lock. It can
be added manually or is automatically included when `include_code_data_view` is enabled.

#### Card Configuration

| Option           | Required | Default              | Description                                     |
| ---------------- | -------- | -------------------- | ----------------------------------------------- |
| `lock_entity_id` | Yes      | -                    | The entity ID of the lock to display codes for  |
| `title`          | No       | Lock name            | Custom title for the card                       |
| `code_display`   | No       | `masked_with_reveal` | How to display codes: see modes below           |

Example card configuration:

```yaml
type: custom:lock-code-manager-lock-data
lock_entity_id: lock.front_door
title: Front Door Codes
code_display: masked_with_reveal
```

**Code Display Modes:**

- `masked` - Codes are always hidden (shown as bullets)
- `unmasked` - Codes are always visible
- `masked_with_reveal` - Codes are masked but can be revealed with a toggle button

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
