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

Supported lock integrations:

| Integration | Read PINs | Push Updates | Code Events | Notes |
| --- | --- | --- | --- | --- |
| [Z-Wave][wiki-zwave] | Varies | Yes | Yes | Some locks mask PINs |
| [Matter][wiki-matter] | No | Yes | Yes | PINs write-only per spec |
| [Schlage WiFi][wiki-schlage] | No | No | No | Cloud-based, PINs masked |
| [Akuvox][wiki-akuvox]¹ | Yes | No | No | Local API, polling-based |
| [Virtual][wiki-virtual]¹ | Yes | No | No | For testing only |

¹ Custom integration required ([Local Akuvox][local-akuvox],
[hass-virtual][hass-virtual])

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

Pre-built automations and templates for common lock management
patterns. See [BLUEPRINTS.md](BLUEPRINTS.md) for full details,
input tables, and import buttons.

### Template blueprints

- **Calendar Condition** — Binary sensor for calendar-based slot access
- **Date Range Condition** — Binary sensor for start/end date access

### Automation blueprints

- **Slot Usage Limiter** — Disable a slot after a set number of uses
- **Calendar PIN Setter** — Extract and set PINs from calendar events
- **Auto Re-lock** — Re-lock after a delay with day/night support
- **Lock on Door Close** — Lock when a door sensor detects closure
- **Slot Usage Notifier** — Notify when a code slot PIN is used
- **Condition Linker** — Assign a condition entity to a slot via UI

## Learn More

Visit the [Wiki](https://github.com/raman325/lock_code_manager/wiki) for detailed
documentation including configuration, troubleshooting, dashboard setup, and development guides.

## UI & Dashboards

Lock Code Manager provides Lovelace strategies and custom cards for managing PINs.
See the [wiki](https://github.com/raman325/lock_code_manager/wiki/Add-a-UI-for-lock-code-management)
for configuration options and setup guidance.

## Inspiration

I am a [keymaster](https://github.com/FutureTense/keymaster) maintainer,
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
   would have been the equivalent of creating a new integration anyway.

## Thanks

A big thank you to the other `keymaster` maintainers:

- @FutureTense
- @firstof9

As well as the person who created the base concept that `keymaster` evolved from:
@ptdalen
