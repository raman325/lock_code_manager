# Lock Code Manager

Lock Code Manager is a Home Assistant integration that keeps PIN codes in sync
across one or more locks, even across different lock providers (e.g. Z-Wave and
Matter locks sharing the same codes). Define your codes once and Lock Code
Manager handles setting, clearing, and monitoring them on every lock.

Features:

- Synchronize PIN codes across multiple locks and providers
- Automatic sync — codes are set and cleared as needed, with retry and
  drift detection
- Condition entities control when a code is active:
  - `calendar` — active during events
  - `binary_sensor` / `switch` / `input_boolean` — active when on
  - `schedule` — active during scheduled times
- [Blueprints](https://github.com/raman325/lock_code_manager/wiki/Blueprints)
  for usage limiting, calendar-driven PINs, and more
- Dashboard cards for managing codes and viewing lock status

Locks from the following integrations are currently supported:

**Core integrations:**

- [Matter][wiki-matter]
- [Schlage WiFi][wiki-schlage]
- [Z-Wave][wiki-zwave]

**Custom integrations:**

- [Akuvox][wiki-akuvox] (via [Local Akuvox][local-akuvox])
- [Virtual][wiki-virtual] (via [hass-virtual][hass-virtual])

[wiki-akuvox]: https://github.com/raman325/lock_code_manager/wiki/Akuvox-integration
[wiki-matter]: https://github.com/raman325/lock_code_manager/wiki/Matter-integration
[wiki-schlage]: https://github.com/raman325/lock_code_manager/wiki/Schlage-integration
[wiki-virtual]: https://github.com/raman325/lock_code_manager/wiki/Virtual-integration
[local-akuvox]: https://github.com/pjaudiomv/hass-local-akuvox
[hass-virtual]: https://github.com/twrecked/hass-virtual
[wiki-zwave]: https://github.com/raman325/lock_code_manager/wiki/Z-Wave-integration

Adding support for new lock integrations is straightforward — see the
[Adding a Provider](https://github.com/raman325/lock_code_manager/wiki/Adding-a-Provider)
guide. Contributors welcome!

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

1. Set up your locks in Home Assistant through a supported integration
   (Z-Wave, Matter, Schlage, etc.)
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

1. `keymaster` is still a great solution that works as is.
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
