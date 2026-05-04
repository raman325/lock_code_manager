# Lock Code Manager

**Lock Code Manager scales down to a simple UI for managing your lock's PIN
codes — and scales up to a programmable, multi-lock, multi-provider PIN
platform with scheduled access, condition-driven activation, and
automation-ready services.** Define your codes once and LCM handles setting,
clearing, and monitoring them on every lock, across providers (Z-Wave, ZHA,
Matter, and more) without you wiring up the differences.

Whether you just want a clean dashboard for your front door or you're rotating
PINs across an Airbnb fleet on a nightly schedule, LCM has you covered.

Features:

- Synchronize PIN codes across multiple locks and providers
- Automatic sync — codes are set and cleared as needed, with retry and
  drift detection
- Condition entities control when a code is active:
  - `calendar` — active during events
  - `binary_sensor` / `switch` / `input_boolean` — active when on
  - `schedule` — active during scheduled times
- [Services and actions](https://github.com/raman325/lock_code_manager/wiki/Services-and-Actions)
  for setting/clearing PINs, attaching condition entities, hard-refreshing
  from the lock, and generating safe random PINs from automations
- [Blueprints](https://github.com/raman325/lock_code_manager/wiki/Blueprints)
  for advanced use cases like usage limiting, calendar-driven PINs, and more
- Dashboard strategies and custom cards for managing codes and viewing lock
  status — from one-line auto-generated dashboards to fully hand-composed
  layouts

Supported lock integrations:

| Integration | Read PINs | Push Updates | Code Events | Notes |
| --- | --- | --- | --- | --- |
| [Z-Wave][wiki-zwave] | Varies | ✅ | ✅ | Some locks mask PINs |
| [ZHA][wiki-zha] | ✅ | ✅ | ✅ | Drift detection fallback if lock lacks programming events |
| [Zigbee2MQTT][wiki-zigbee2mqtt] (MQTT)² | Varies | ✅ | ✅ | Same broker as Z2M; PIN support depends on lock |
| [Matter][wiki-matter] | ❌ | ✅ | ✅ | PINs write-only per spec |
| [Schlage WiFi][wiki-schlage] | ❌ | ❌ | ❌ | Cloud-based, PINs masked |
| [Akuvox][wiki-akuvox]¹ | ✅ | ❌ | ❌ | Local API, polling-based |
| [Virtual][wiki-virtual]¹ | ✅ | ❌ | ❌ | For testing only |

¹ Custom integration required ([Local Akuvox][local-akuvox],
[hass-virtual][hass-virtual])

² **Zigbee2MQTT (MQTT)** — Pair the lock in [Zigbee2MQTT][zigbee2mqtt] with PIN/user-code support for your firmware.
The **Code Events** column refers to PIN-used automations from Lock Code Manager’s event entity
(which slots were used to lock/unlock).
Zigbee2MQTT lock/unlock actions with user identification are mapped to code slot events for PIN-used automations.
Configure Home Assistant’s **MQTT** integration on the **same broker** Zigbee2MQTT uses.
The default Zigbee2MQTT base topic `zigbee2mqtt` matches what Lock Code Manager expects unless you customize topics
(`{base_topic}/{friendly_name}/set|get`).
During LCM setup, choose your `lock.*` entity from **MQTT**.
If you rename the device in HA, keep it aligned with the **friendly name** in Zigbee2MQTT.

[zigbee2mqtt]: https://www.zigbee2mqtt.io/
[wiki-akuvox]: https://github.com/raman325/lock_code_manager/wiki/Akuvox-integration
[wiki-zigbee2mqtt]: https://github.com/raman325/lock_code_manager/wiki/Zigbee2MQTT-integration
[wiki-matter]: https://github.com/raman325/lock_code_manager/wiki/Matter-integration
[wiki-schlage]: https://github.com/raman325/lock_code_manager/wiki/Schlage-integration
[wiki-virtual]: https://github.com/raman325/lock_code_manager/wiki/Virtual-integration
[local-akuvox]: https://github.com/pjaudiomv/hass-local-akuvox
[hass-virtual]: https://github.com/twrecked/hass-virtual
[wiki-zha]: https://github.com/raman325/lock_code_manager/wiki/ZHA-integration
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
   (Z-Wave, ZHA, Matter, Schlage, Zigbee2MQTT/MQTT, etc.)
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

Lock Code Manager ships custom Lovelace **strategies** (which auto-generate UI
from your config) and custom **cards** (which you compose yourself). Pick the
one that matches how much control you want — from a one-click dashboard that
just works (selectable directly from **Settings → Dashboards → Add Dashboard**
on Home Assistant 2026.5+) to a hand-authored layout that places exactly what
you want where you want it.

- [UI overview & decision guide][wiki-ui-overview] — start here
- [UI Strategies][wiki-ui-strategies] — dashboard, view, and section strategies
- [Custom Cards][wiki-ui-cards] — slot card, lock-codes card, and code-display modes

[wiki-ui-overview]: https://github.com/raman325/lock_code_manager/wiki/Add-a-UI-for-lock-code-management
[wiki-ui-strategies]: https://github.com/raman325/lock_code_manager/wiki/UI-Strategies
[wiki-ui-cards]: https://github.com/raman325/lock_code_manager/wiki/Custom-Cards

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
