# Lock Code Manager

Lock Code Manager is a Home Assistant integration that allows you to more easily manage
your usercodes on your locks. Once you have configured it, the integration will set and
clear codes on your locks as needed depending on how you decide to configure them.

Features:

- Synchronize multiple locks with a single set of codes
- Optionally use a condition entity to control when a code is active. Supported entity types:
  - `calendar` - code is active when an event is in progress
  - `binary_sensor` - code is active when the sensor is `on`
  - `switch` - code is active when the switch is `on`
  - `schedule` - code is active when the schedule is `on`
  - `input_boolean` - code is active when the input boolean is `on`
- Optionally define a maximum number of uses for a code before the code is disabled

Locks from the following integrations are currently supported:

- Z-Wave
- Matter
- [Virtual](https://github.com/twrecked/hass-virtual) custom integration. See the
  [Wiki page on this integration](https://github.com/raman325/lock_code_manager/wiki/Virtual-integration)
  for more details on why it was built and how it works.

The code was written to make it (I think) easy to add support for locks in other
integrations. Check the [Wiki](https://github.com/raman325/lock_code_manager/wiki) if you
want to learn more about that and take a stab at it. Contributors welcome!

## Integrations That Cannot Currently Be Supported

Some lock integrations cannot currently be supported due to limitations in their underlying
libraries. See the [wiki](https://github.com/raman325/lock_code_manager/wiki/Unsupported-Integrations)
for details.

## Condition Entity Integrations Not Supported

Some condition entity integrations are not compatible. See the
[wiki](https://github.com/raman325/lock_code_manager/wiki/Unsupported-Condition-Entity-Integrations)
for details.

## Installation

The best way to install this integration is via HACS.

1. Set up your locks as entities to your Home Assistant instance through the corresponding
   integration (e.g. Z-Wave)
2. Add this repository as a custom integration repository in HACS
3. Go to Settings > Devices & Services > Add Integration
4. Select Lock Code Manager
5. Follow the prompts - additional information about the configuration options are
   available in the Wiki

## Blueprints

Pre-built automations for common patterns:

- **Slot Usage Limiter** — Disable a slot after a set number of uses
  [![Import Blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Framan325%2Flock_code_manager%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flock_code_manager%2Fslot_usage_limiter.yaml)
- **Calendar PIN Setter** — Extract and set PINs from calendar event attributes
  [![Import Blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Framan325%2Flock_code_manager%2Fblob%2Fmain%2Fblueprints%2Fautomation%2Flock_code_manager%2Fcalendar_pin_setter.yaml)
- **Calendar Condition** — Control slot access based on calendar events
  [![Import Blueprint](https://my.home-assistant.io/badges/blueprint_import.svg)](https://my.home-assistant.io/redirect/blueprint_import/?blueprint_url=https%3A%2F%2Fgithub.com%2Framan325%2Flock_code_manager%2Fblob%2Fmain%2Fblueprints%2Ftemplate%2Flock_code_manager%2Fcalendar_condition.yaml)

See the [wiki](https://github.com/raman325/lock_code_manager/wiki/Blueprints) for detailed setup and configuration.

## Learn More

Visit the [Wiki](https://github.com/raman325/lock_code_manager/wiki) for detailed
documentation including configuration, troubleshooting, dashboard setup, and development guides.

## UI & Dashboards

Lock Code Manager provides Lovelace strategies and custom cards for managing PINs.
See the [wiki](https://github.com/raman325/lock_code_manager/wiki/Add-a-UI-for-lock-code-management)
for configuration options and setup guidance.

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
