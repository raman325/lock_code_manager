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
- Automatic sync — every write is read back from the lock to confirm it, with
  retries and drift detection
- Never touches codes it didn't create: new users go only in free slots, a
  lock that already holds codes in the configuration's slots is refused until
  you clear them, clearing or removing a user never deletes a lock user
  without Lock Code Manager's tag, and on Matter only PINs Home Assistant
  created are removed
- Shows what each lock reports for each slot, including when a lock can't tell
- [Repairs][wiki-repairs] tell you when a code can't be set or cleared, a lock
  is offline, or a lock was re-paired
- [Condition entities][wiki-conditions] control when a code is active: a
  calendar, a schedule, or anything on/off. Any other state, such as people
  being home, works through a template binary sensor
- [Services and actions][wiki-services] for managing users, PINs, and
  conditions from automations
- [Blueprints](BLUEPRINTS.md) for usage limiting, calendar-driven PINs, and more
- [External keypads and code readers][wiki-external-keypads], such as an
  ESPHome keypad, checked against the same users and schedules
- [Guest and rental workflows][wiki-guests]
- [Dashboard strategies and custom cards](#ui--dashboards), from one-line
  auto-generated dashboards to fully hand-composed layouts

## Supported Integrations

| Integration | Read PINs | Push Updates | Code Events | Notes |
| --- | --- | --- | --- | --- |
| [Z-Wave][wiki-zwave] | Varies | ✅ | ✅ | Some locks mask PINs |
| [ZHA][wiki-zha] | ✅ | ✅ | ✅ | Some locks can't report their PINs |
| [Zigbee2MQTT][wiki-zigbee2mqtt] (MQTT) | Varies | ✅ | ✅ | PIN support depends on the lock; some can't report their PINs |
| [Z-Wave JS UI][wiki-zwave-js-ui] (MQTT) | Varies | ✅ | ✅ | For locks bridged through zwave-js-ui's MQTT gateway; the Z-Wave row is recommended |
| [Matter][wiki-matter] | ❌ | ✅ | ✅ | PINs are write-only per the spec |
| [Schlage WiFi][wiki-schlage] | ❌ | ❌ | ❌ | Cloud-based, PINs masked |
| [Akuvox][wiki-akuvox]¹ | ✅ | ❌ | ❌ | Local API, polling-based |
| [Virtual][wiki-virtual]¹ | ✅ | ❌ | ✅ | Try Lock Code Manager without a real lock |
| [ESPHome][wiki-external-keypads] | ❌ | ❌ | ✅ | Keypads and readers, not locks: codes are checked, nothing is written to the device |

**Code Events** means Lock Code Manager can tell which user's code was used, for
automations.

¹ Custom integration required ([Local Akuvox][local-akuvox],
[hass-virtual][hass-virtual])

Each integration's wiki page covers its setup and limitations. Adding support
for a new lock integration is straightforward — see the
[Adding a Provider][wiki-adding-provider] guide. Contributors welcome!

Some lock integrations [can't currently be supported][wiki-unsupported], and
some integrations' entities [don't work as condition entities][wiki-unsupported-conditions].

## Installation

The best way to install this integration is via HACS.

1. Set up your locks in Home Assistant through a supported integration
   (Z-Wave, ZHA, Matter, Schlage, Zigbee2MQTT/MQTT, etc.)
2. Add this repository as a custom integration repository in HACS
3. Go to Settings > Devices & Services > Add Integration
4. Select Lock Code Manager
5. Follow the prompts - additional information about the configuration options are
   available in the Wiki

**Upgrading across a major version?** Read its upgrade guide in the
[wiki][wiki] first — major-version migrations are one-way.

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

## Learn More

Visit the [Wiki][wiki] for detailed documentation including configuration,
troubleshooting, dashboard setup, and development guides.

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

[wiki]: https://github.com/raman325/lock_code_manager/wiki
[wiki-repairs]: https://github.com/raman325/lock_code_manager/wiki/Repair-Issues
[wiki-conditions]: https://github.com/raman325/lock_code_manager/wiki/Using-Condition-Entities
[wiki-services]: https://github.com/raman325/lock_code_manager/wiki/Services-and-Actions
[wiki-guests]: https://github.com/raman325/lock_code_manager/wiki/Managing-Guests-and-Rentals
[wiki-adding-provider]: https://github.com/raman325/lock_code_manager/wiki/Adding-a-Provider
[wiki-unsupported]: https://github.com/raman325/lock_code_manager/wiki/Unsupported-Integrations
[wiki-unsupported-conditions]: https://github.com/raman325/lock_code_manager/wiki/Unsupported-Condition-Entity-Integrations
[wiki-zwave]: https://github.com/raman325/lock_code_manager/wiki/Z-Wave-integration
[wiki-zha]: https://github.com/raman325/lock_code_manager/wiki/ZHA-integration
[wiki-zigbee2mqtt]: https://github.com/raman325/lock_code_manager/wiki/Zigbee2MQTT-integration
[wiki-zwave-js-ui]: https://github.com/raman325/lock_code_manager/wiki/Z-Wave-JS-UI-(MQTT)
[wiki-matter]: https://github.com/raman325/lock_code_manager/wiki/Matter-integration
[wiki-schlage]: https://github.com/raman325/lock_code_manager/wiki/Schlage-integration
[wiki-akuvox]: https://github.com/raman325/lock_code_manager/wiki/Akuvox-integration
[wiki-virtual]: https://github.com/raman325/lock_code_manager/wiki/Virtual-integration
[wiki-external-keypads]: https://github.com/raman325/lock_code_manager/wiki/External-Keypads
[wiki-ui-overview]: https://github.com/raman325/lock_code_manager/wiki/Add-a-UI-for-lock-code-management
[wiki-ui-strategies]: https://github.com/raman325/lock_code_manager/wiki/UI-Strategies
[wiki-ui-cards]: https://github.com/raman325/lock_code_manager/wiki/Custom-Cards
[local-akuvox]: https://github.com/pjaudiomv/hass-local-akuvox
[hass-virtual]: https://github.com/twrecked/hass-virtual
