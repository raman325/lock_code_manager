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

- Give each person a name and a PIN; Lock Code Manager works out which slot
  on which lock they land in
- Synchronize PIN codes across multiple locks and providers
- Automatic sync — codes are set and cleared as needed, with retry and
  drift detection
- Condition entities control when a code is active:
  - `calendar` — active during events
  - `binary_sensor` / `switch` / `input_boolean` — active when on
  - `schedule` — active during scheduled times
- [Services and actions](https://github.com/raman325/lock_code_manager/wiki/Services-and-Actions)
  for adding and removing users, setting/clearing PINs, attaching condition
  entities, hard-refreshing from the lock, and generating safe random PINs
  from automations
- [Blueprints](https://github.com/raman325/lock_code_manager/wiki/Blueprints)
  for advanced use cases like usage limiting, calendar-driven PINs, and more
- Check codes entered somewhere Lock Code Manager doesn't control — a
  do-it-yourself ESPHome keypad, an intercom — against the same users and
  schedules, and record the use like any other
- [Guest and rental workflows](https://github.com/raman325/lock_code_manager/wiki/Managing-Guests-and-Rentals)
  — rotate a standing user per booking, or add and remove them per stay
- Dashboard strategies and custom cards for managing codes and viewing lock
  status — from one-line auto-generated dashboards to fully hand-composed
  layouts

Supported lock integrations:

| Integration | Read PINs | Push Updates | Code Events | Notes |
| --- | --- | --- | --- | --- |
| [Z-Wave][wiki-zwave] | Varies | ✅ | ✅ | Some locks mask PINs |
| [ZHA][wiki-zha] | ✅ | ✅ | ✅ | Drift detection fallback if lock lacks programming events |
| [Zigbee2MQTT][wiki-zigbee2mqtt] (MQTT)² | Varies | ✅ | ✅ | Same broker as Z2M; PIN support depends on lock |
| [Z-Wave JS UI][wiki-zwave-js-ui] (MQTT)³ | Varies | ✅ | ✅ | Locks bridged via zwave-js-ui's MQTT gateway; some locks mask PINs |
| [Matter][wiki-matter] | ❌ | ✅ | ✅ | PINs write-only per spec |
| [Schlage WiFi][wiki-schlage] | ❌ | ❌ | ❌ | Cloud-based, PINs masked |
| [Akuvox][wiki-akuvox]¹ | ✅ | ❌ | ❌ | Local API, polling-based |
| [Virtual][wiki-virtual]¹ | ✅ | ❌ | ✅ | A credential store rather than a device: records uses reported by the `use_credential` action, and lets you try Lock Code Manager without a real lock |

¹ Custom integration required ([Local Akuvox][local-akuvox],
[hass-virtual][hass-virtual])

² **Zigbee2MQTT (MQTT)** — Pair the lock in [Zigbee2MQTT][zigbee2mqtt] with PIN/user-code support for your firmware.
The **Code Events** column refers to PIN-used automations from Lock Code Manager’s event entity
(which slots were used to lock/unlock).
Zigbee2MQTT lock/unlock actions with user identification are mapped to code slot events for PIN-used automations.
Configure Home Assistant’s **MQTT** integration on the **same broker** Zigbee2MQTT uses.
Custom Zigbee2MQTT base topics — including multi-level topics (e.g. `home/z2m`) and multiple
bridges on one broker — are supported automatically: Lock Code Manager reads each lock’s exact
MQTT topic from the discovery data Zigbee2MQTT publishes to Home Assistant, so no extra
configuration is needed. Locks must be added to Home Assistant via MQTT discovery
(Zigbee2MQTT’s default).
During LCM setup, choose your `lock.*` entity from **MQTT**.

³ **Z-Wave JS UI (MQTT)** — For Z-Wave locks exposed to Home Assistant through
[zwave-js-ui][zwave-js-ui]'s MQTT discovery gateway rather than the official websocket
integration (which the [Z-Wave][wiki-zwave] row covers, and which zwave-js-ui itself
recommends — switching needs no re-pairing). Reads and writes go through zwave-js-ui's
MQTT api; gateway type **Named** or **ValueID** gives full functionality including
push updates and PIN-used events, while **Manual** runs polling-only. Configure Home
Assistant’s **MQTT** integration on the same broker zwave-js-ui uses.

[zigbee2mqtt]: https://www.zigbee2mqtt.io/
[wiki-akuvox]: https://github.com/raman325/lock_code_manager/wiki/Akuvox-integration
[wiki-zigbee2mqtt]: https://github.com/raman325/lock_code_manager/wiki/Zigbee2MQTT-integration
[wiki-zwave-js-ui]: https://github.com/raman325/lock_code_manager/wiki/Z-Wave-JS-UI-(MQTT)
[zwave-js-ui]: https://github.com/zwave-js/zwave-js-ui
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

Some integrations create entities whose states don't map to LCM condition entity
semantics ("access allowed" / "access denied"). The integration may still be usable
with LCM by driving slot Enabled switches from its own automation logic instead of
attaching its entities as condition entities. See the
[wiki](https://github.com/raman325/lock_code_manager/wiki/Unsupported-Condition-Entity-Integrations)
for details and the scheduler-component workaround.

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

- **Calendar Condition** — Binary sensor for calendar-based access
- **Date Range Condition** — Binary sensor for start/end date access

### Automation blueprints

- **Slot Usage Limiter** — Disable a user after a set number of uses
- **Calendar PIN Setter** — Extract and set PINs from calendar events
- **Auto Re-lock** — Re-lock after a delay with day/night support
- **Lock on Door Close** — Lock when a door sensor detects closure
- **Slot Usage Notifier** — Notify when someone's credential is used
- **Credential Used** — Run any actions when a credential is used, from a
  lock's own keypad or an external one
- **Condition Linker** — Assign a condition entity to a user via UI

## Learn More

Visit the [Wiki](https://github.com/raman325/lock_code_manager/wiki) for detailed
documentation including configuration, troubleshooting, dashboard setup, and development guides.

**Upgrading from 4.x?** 5.0 is configured by user rather than by slot number,
and migrating renames every entity ID. Read
[Upgrading to 5.0](https://github.com/raman325/lock_code_manager/wiki/Upgrading-to-5.0)
first — the migration is one-way.

## UI & Dashboards

Lock Code Manager ships custom Lovelace **strategies** (which auto-generate UI
from your config) and custom **cards** (which you compose yourself). Pick the
one that matches how much control you want — from a one-click dashboard that
just works (selectable directly from **Settings → Dashboards → Add Dashboard**
on Home Assistant 2026.5+) to a hand-authored layout that places exactly what
you want where you want it.

- [UI overview & decision guide][wiki-ui-overview] — start here
- [UI Strategies][wiki-ui-strategies] — dashboard, view, and section strategies
- [Custom Cards][wiki-ui-cards] — user card, add-user card, lock-codes card, and code-display modes

[wiki-ui-overview]: https://github.com/raman325/lock_code_manager/wiki/Add-a-UI-for-lock-code-management
[wiki-ui-strategies]: https://github.com/raman325/lock_code_manager/wiki/UI-Strategies
[wiki-ui-cards]: https://github.com/raman325/lock_code_manager/wiki/Custom-Cards

## AI Usage

While I began this project before AI coding agents and wrote the bones
myself, since November 2025 it's been hard NOT to code with a coding
agent. You will notice that almost all of my PRs since then have been
co-authored with Claude (I've also experimented with Codex and Copilot).

If you look at any of the PRs, particularly the meaningful ones, you'll
notice many commits. Some come from AI code reviews, but many come from
my own code reviews or from me steering the design. I can't say I've
audited everything line for line, but I can comfortably say that it's not
exfiltrating your data or doing anything evil.

If you are uncomfortable using this integration because of concerns around
AI, I completely understand and that is your prerogative. If you ever want
to chat about it — whether it's to convince me that what I'm doing is
wrong, to learn more about it, or anything in between — find me on the
[Home Assistant Discord](https://www.home-assistant.io/join-chat/).
