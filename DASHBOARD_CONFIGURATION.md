# Dashboard Configuration

Lock Code Manager provides Lovelace strategies and custom cards for managing and monitoring
PINs. You can use the dashboard strategy (full dashboard), the view strategy (single view
for a config entry), or the custom cards directly.

See [this wiki article](https://github.com/raman325/lock_code_manager/wiki/Add-a-UI-for-lock-code-management#dashboard)
for additional details.

## Dashboard Strategy

Use the dashboard strategy to build a full dashboard with one view per config entry, and
an optional "User Codes" view that shows lock codes cards across all managed locks.

| Option                              | Default              | Description                                                 |
| ----------------------------------- | -------------------- | ----------------------------------------------------------- |
| `use_slot_cards`                    | `true`               | Use streamlined slot cards instead of legacy entities cards |
| `show_code_sensors`                 | `true`               | Show code sensors in lock status section                    |
| `show_conditions`                   | `true`               | Show conditions section in slot cards                       |
| `show_lock_status`                  | `true`               | Show lock status section in slot cards                      |
| `show_lock_sync`                    | `true`               | Show sync status per lock in lock status                    |
| `collapsed_sections`                | `[]`                 | Which sections start collapsed (empty = all expanded)       |
| `show_per_configuration_lock_cards` | `true`               | Show lock cards in per-config-entry views                   |
| `show_all_lock_cards_view`          | `true`               | Add a "User Codes" view with cards showing all lock codes   |
| `code_display`                      | `masked_with_reveal` | Code visibility mode for slot cards and lock codes cards    |

Example dashboard configuration:

```yaml
strategy:
  type: custom:lock-code-manager
  show_all_lock_cards_view: true
  show_per_configuration_lock_cards: true
  code_display: masked_with_reveal
  show_lock_sync: true
```

## View Strategy

Use the view strategy when you want a single view for one config entry. If
`show_lock_cards` is true, lock codes cards are appended below the slot
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
| `show_lock_cards`          | `true`               | Append lock codes cards below the slot cards                      |
| `code_display`             | `masked_with_reveal` | Code visibility mode for slot cards and lock codes cards          |

Example view configuration:

```yaml
strategy:
  type: custom:lock-code-manager
  config_entry_id: 1234567890abcdef
  show_lock_cards: false
  code_display: masked
```

## Section Strategies

For advanced users building custom `sections`-type views, Lock Code Manager provides
section strategies that generate cards when Home Assistant renders the section.

> **Note:** Section strategies simply render a single card (`lcm-slot` or `lcm-lock-codes`)
> within a grid section. You can use the cards directly in your dashboard without using
> section strategies - see the [Slot Card](#slot-card) and [Lock Codes Card](#lock-codes-card)
> sections below.

### Slot Section Strategy

Use `custom:lock-code-manager-slot` to render a single slot section:

| Option              | Required | Default              | Description                                                   |
| ------------------- | -------- | -------------------- | ------------------------------------------------------------- |
| `config_entry_id`   | Yes      | -                    | Config entry ID for the LCM instance                          |
| `slot`              | Yes      | -                    | Slot number to display                                        |
| `use_slot_cards`    | No       | `true`               | Use new lcm-slot card (`true`) or legacy entities card        |
| `code_display`      | No       | `masked_with_reveal` | Code visibility mode                                          |
| `show_conditions`   | No       | `true`               | Show conditions section                                       |
| `show_lock_status`  | No       | `true`               | Show lock status section                                      |
| `show_code_sensors` | No       | `true`               | Show code sensors in lock status                              |
| `show_lock_sync`    | No       | `true`               | Show sync status per lock                                     |
| `collapsed_sections`| No       | `[]`                 | Sections to collapse by default: `conditions`, `lock_status`  |

```yaml
views:
  - type: sections
    sections:
      - strategy:
          type: custom:lock-code-manager-slot
          config_entry_id: 1234567890abcdef
          slot: 1
          code_display: masked_with_reveal
      - strategy:
          type: custom:lock-code-manager-slot
          config_entry_id: 1234567890abcdef
          slot: 2
```

### Lock Section Strategy

Use `custom:lock-code-manager-lock` to render a lock codes section:

| Option           | Required | Default              | Description                           |
| ---------------- | -------- | -------------------- | ------------------------------------- |
| `lock_entity_id` | Yes      | -                    | The entity ID of the lock             |
| `code_display`   | No       | `masked_with_reveal` | Code visibility mode                  |

```yaml
views:
  - type: sections
    sections:
      - strategy:
          type: custom:lock-code-manager-lock
          lock_entity_id: lock.front_door
          code_display: masked
```

## Slot Card

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

## Lock Codes Card

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

## Code Display Modes

- `masked` - Codes are always hidden (shown as bullets)
- `unmasked` - Codes are always visible
- `masked_with_reveal` - Codes are masked but can be revealed with a toggle button (default)
