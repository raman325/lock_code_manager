# Add a UI for lock code management

Lock Code Manager provides custom Lovelace strategies and cards for managing PINs directly from your Home Assistant dashboard.

## Dashboard

The easiest way to get started is to create a dedicated dashboard using the Lock Code Manager
strategy. This automatically generates views for each of your LCM config entries.

### Creating the Dashboard

1. Go to **Settings > Dashboards > Add Dashboard**
2. Choose a name (e.g., "Lock Codes") and icon
3. Open the dashboard and click the pencil icon to edit
4. Click the three dots menu and select **Raw configuration editor**
5. Replace the content with:

```yaml
strategy:
  type: custom:lock-code-manager
```

### Dashboard Options

| Option                  | Default              | Description                                                    |
| ----------------------- | -------------------- | -------------------------------------------------------------- |
| `use_slot_cards`        | `true`               | Use streamlined slot cards instead of legacy entities cards    |
| `show_code_sensors`     | `false`              | Show code sensors in lock status section                       |
| `show_conditions`       | `true`               | Show conditions section in slot cards                          |
| `show_lock_status`      | `true`               | Show lock status section in slot cards                         |
| `show_lock_sync`        | `true`               | Show sync status per lock in lock status                       |
| `collapsed_sections`    | `[]`                 | Sections collapsed by default: `conditions`, `lock_status`     |
| `include_code_data_view`| `false`              | Add a "User Codes" view with cards showing all lock codes      |
| `code_display`          | `masked_with_reveal` | Code visibility mode for slot cards and lock codes cards       |

**Legacy options** (still supported but deprecated):

- `include_code_slot_sensors` → use `show_code_sensors`
- `include_in_sync_sensors` → use `show_lock_sync`
- `code_data_view_code_display` → use `code_display`

### Example Dashboard Configuration

```yaml
strategy:
  type: custom:lock-code-manager
  include_code_data_view: true
  code_display: masked_with_reveal
  show_lock_sync: true
```

## View Strategy

If you want to add Lock Code Manager to an existing dashboard, you can use the view strategy for a single config entry.

### View Options

| Option                  | Default              | Description                                                   |
| ----------------------- | -------------------- | ------------------------------------------------------------- |
| `config_entry_id`       | -                    | Config entry ID to render (use this OR config_entry_title)    |
| `config_entry_title`    | -                    | Config entry title to render (use this OR config_entry_id)    |
| `use_slot_cards`        | `true`               | Use streamlined slot cards instead of legacy entities cards   |
| `show_code_sensors`     | `false`              | Show code sensors in lock status section                      |
| `show_conditions`       | `true`               | Show conditions section in slot cards                         |
| `show_lock_status`      | `true`               | Show lock status section in slot cards                        |
| `show_lock_sync`        | `true`               | Show sync status per lock in lock status                      |
| `collapsed_sections`    | `[]`                 | Sections collapsed by default: `conditions`, `lock_status`    |
| `include_code_data_view`| `false`              | Append a "User Codes" section below the slot cards            |
| `code_display`          | `masked_with_reveal` | Code visibility mode for slot cards and lock codes cards      |

**Legacy options** (still supported but deprecated):

- `include_code_slot_sensors` → use `show_code_sensors`
- `include_in_sync_sensors` → use `show_lock_sync`
- `code_data_view_code_display` → use `code_display`

### Example View Configuration

```yaml
views:
  - strategy:
      type: custom:lock-code-manager
      config_entry_title: House Locks
    icon: mdi:lock-smart
    title: Lock Codes
```

## Custom Cards

Lock Code Manager provides two custom cards that can be used independently or are automatically included by the strategies.

### Slot Card (`custom:lcm-slot-card`)

The slot card displays a single code slot with inline editing, real-time WebSocket updates,
and collapsible sections. This is the default card used by the strategies.

**Features:**

- Inline editing for name, PIN, and enabled toggle
- Real-time updates via WebSocket
- Collapsible conditions and lock status sections
- Status badges showing active/inactive/disabled state
- Per-lock sync status

**Configuration:**

| Option               | Required | Default              | Description                                                  |
| -------------------- | -------- | -------------------- | ------------------------------------------------------------ |
| `config_entry_id`    | Yes*     | -                    | Config entry ID for the LCM instance                         |
| `config_entry_title` | Yes*     | -                    | Config entry title (alternative to ID)                       |
| `slot`               | Yes      | -                    | Slot number to display                                       |
| `code_display`       | No       | `masked_with_reveal` | How to display codes (see below)                             |
| `show_conditions`    | No       | `true`               | Show the conditions section                                  |
| `show_lock_status`   | No       | `true`               | Show the lock status section                                 |
| `show_code_sensors`  | No       | `true`               | Show code sensors in lock status                             |
| `show_lock_sync`     | No       | `true`               | Show sync status per lock                                    |
| `collapsed_sections` | No       | `[]`                 | Sections collapsed by default: `conditions`, `lock_status`   |

*Either `config_entry_id` or `config_entry_title` is required, but not both.

**Example:**

```yaml
type: custom:lcm-slot-card
config_entry_id: 1234567890abcdef
slot: 1
code_display: masked_with_reveal
collapsed_sections:
  - lock_status
```

### Lock Codes Card (`custom:lcm-lock-codes-card`)

The lock codes card displays all code slots for a specific lock. It shows status badges,
supports inline editing for unmanaged slots, and provides click-to-navigate for LCM-managed
slots.

**Features:**

- Shows all slots on a lock with their current codes
- Status badges (Active, Inactive, Disabled, Empty)
- Sync status indicators
- Inline editing for slots not managed by LCM
- Click managed slots to navigate to their config entry
- Reveal button for masked codes

**Configuration:**

| Option           | Required | Default              | Description                                    |
| ---------------- | -------- | -------------------- | ---------------------------------------------- |
| `lock_entity_id` | Yes      | -                    | The entity ID of the lock to display codes for |
| `title`          | No       | Lock name            | Custom title for the card                      |
| `code_display`   | No       | `masked_with_reveal` | How to display codes (see below)               |

**Example:**

```yaml
type: custom:lcm-lock-codes-card
lock_entity_id: lock.front_door
title: Front Door Codes
code_display: masked_with_reveal
```

## Code Display Modes

Both cards support three code display modes:

| Mode                 | Description                                              |
| -------------------- | -------------------------------------------------------- |
| `masked`             | Codes are always hidden (shown as bullets)               |
| `unmasked`           | Codes are always visible                                 |
| `masked_with_reveal` | Codes are masked but can be revealed with a toggle button (default) |

## Legacy Mode

If you prefer the old entities-card based UI, set `use_slot_cards: false` in your strategy configuration:

```yaml
strategy:
  type: custom:lock-code-manager
  use_slot_cards: false
  include_code_slot_sensors: true
```

**Note:** When using legacy mode (`use_slot_cards: false`), installing the
[fold-entity-row](https://github.com/thomasloven/lovelace-fold-entity-row) card provides
a cleaner UI by collapsing related entities into expandable rows.
