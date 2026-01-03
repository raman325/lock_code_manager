# Streamlined Slot Card Design

## Overview

Replace the current markdown header + entities card combo with a unified custom card
that provides better UX and visual consistency with the lock-code-data card.

## Current Structure (Problems)

```text
┌─ Markdown Card ─────────────────────┐
│ ## Code Slot 1                      │
└─────────────────────────────────────┘
┌─ Entities Card ─────────────────────┐
│ Name          [text input]          │
│ Enabled       [toggle]              │
│ PIN           [text input]          │
│ ─────────────────────────────────── │
│ PIN active    [binary sensor]       │
│ PIN last used [event timestamp]     │
│ ▶ Conditions (fold-entity-row)      │
│ ▶ Locks in sync (fold-entity-row)   │
│ ▶ Code sensors (fold-entity-row)    │
└─────────────────────────────────────┘
```

**Issues:**

- Two separate cards creates visual disconnect
- Entities card has limited styling options
- Status information (active, in-sync) isn't visually prominent
- Requires fold-entity-row for collapsible sections
- No unified loading/error states

## Proposed Design

### Visual Layout

```text
┌─────────────────────────────────────────────────────────────┐
│ 🔑 Code Slot 1                                              │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  ┌─ Primary Controls ─────────────────────────────────────┐ │
│  │                                                         │ │
│  │  Name     [John Smith                    ]              │ │
│  │                                                         │ │
│  │  PIN      [• • • •   ] 👁    Enabled  [═══●]            │ │
│  │                                                         │ │
│  └─────────────────────────────────────────────────────────┘ │
│                                                             │
│  ┌─ Status ───────────────────────────────────────────────┐ │
│  │                                                         │ │
│  │  ●  Active                    Last used: 2 hours ago   │ │
│  │     Code is set on all locks                           │ │
│  │                                                         │ │
│  └─────────────────────────────────────────────────────────┘ │
│                                                             │
│  ┌─ Conditions ─────────────────────────────────── ▼ ────┐ │
│  │  Number of uses    [  5  ]                             │ │
│  │  Calendar          Home Schedule                       │ │
│  └─────────────────────────────────────────────────────────┘ │
│                                                             │
│  ┌─ Lock Status ───────────────────────────────── ▼ ────┐ │
│  │                                                         │ │
│  │  Front Door Lock                                        │ │
│  │  ┌────────────────────────────────────────────────────┐ │ │
│  │  │  ✓ Synced     Code: ••••     Last sync: 5 min ago  │ │ │
│  │  └────────────────────────────────────────────────────┘ │ │
│  │                                                         │ │
│  │  Back Door Lock                                         │ │
│  │  ┌────────────────────────────────────────────────────┐ │ │
│  │  │  ⚠ Pending    Code: ––––     Syncing...            │ │ │
│  │  └────────────────────────────────────────────────────┘ │ │
│  │                                                         │ │
│  └─────────────────────────────────────────────────────────┘ │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### Component Sections

#### 1. Header (Always Visible)

- Slot number with key icon
- Matches lock-code-data card header style
- Border separates from content

#### 2. Primary Controls Section

- **Name**: Inline text input with label
- **PIN**: Masked by default, reveal button, inline with enabled toggle
- **Enabled**: Toggle switch, prominent position

#### 3. Status Section

- **Active indicator**: Green dot when active, yellow when inactive (conditions blocking),
  gray when disabled
- **Status text**: Human-readable explanation
  - "Active - Code is set on all locks"
  - "Inactive - Blocked by schedule"
  - "Disabled by user"
  - "Syncing..."
- **Last used**: Relative timestamp from pin_used event

#### 4. Conditions Section (Collapsible)

- Collapsed by default if no conditions configured
- Shows condition count in header when collapsed
- Contains:
  - Number of uses (if configured)
  - Calendar entity (if configured)

#### 5. Lock Status Section (Collapsible)

- Shows per-lock sync status
- Each lock displays:
  - Lock name
  - Sync status icon (✓ synced, ⚠ pending, ✗ error)
  - Current code (masked or revealed based on setting)
  - Optional: Code sensor entity (if include_code_slot_sensors)

### State Mapping

| `active` | `enabled` | Display |
| -------- | --------- | ------- |
| true | true | Green "Active" |
| false | true | Yellow "Inactive" (conditions blocking) |
| false | false | Gray "Disabled" |
| undefined | undefined | Loading/Unknown |

### Data Requirements

The card needs a new websocket command to get slot-specific data:

```typescript
interface SlotCardData {
  slot_num: number;
  name: string;
  pin: string | null;         // Actual or masked
  pin_length?: number;        // When masked
  enabled: boolean;
  active: boolean;

  // Per-lock status
  locks: {
    entity_id: string;
    name: string;
    in_sync: boolean;
    code: string | null;
    code_length?: number;
    last_synced?: string;     // ISO timestamp
  }[];

  // Conditions
  conditions: {
    number_of_uses?: number;
    calendar_entity_id?: string;
  };

  // Event data
  last_used?: string;         // ISO timestamp
}
```

### Configuration

```yaml
type: custom:lock-code-manager-slot
config_entry_id: abc123
slot: 1
# Optional
show_code_sensors: false      # Default: false
code_display: masked_with_reveal  # masked | unmasked | masked_with_reveal (consistent with lock-data card)
collapsed_sections:           # Default: ['conditions', 'lock_status']
  - conditions
  - lock_status
```

### CSS Variables (Theme Support)

```css
--lcm-slot-active-color: var(--success-color, #4caf50)
--lcm-slot-inactive-color: var(--warning-color, #ff9800)
--lcm-slot-disabled-color: var(--disabled-text-color)
--lcm-slot-synced-color: var(--success-color)
--lcm-slot-pending-color: var(--warning-color)
--lcm-slot-error-color: var(--error-color)
```

### Interactions

1. **Name field**: Direct inline editing, auto-saves on blur/enter
2. **PIN field**: Click to edit, masked by default, reveal button toggles
3. **Enabled toggle**: Immediate state change with optimistic update
4. **Section headers**: Click to expand/collapse
5. **Lock rows**: Click to expand detailed view (optional)

### Error States

- **Loading**: Skeleton placeholders
- **Connection lost**: Banner with retry button
- **Entity unavailable**: Grayed out with "Unavailable" text
- **Sync failed**: Red indicator on affected lock

### Responsive Behavior

- **Wide (>600px)**: Two-column layout for status section
- **Narrow (<600px)**: Single column, stacked layout
- **Mobile**: Touch-friendly toggle sizes, larger tap targets

## Implementation Phases

### Phase 1: Core Card

- Header and primary controls
- Status section with active/enabled indicators
- Basic styling matching lock-code-data card

### Phase 2: Collapsible Sections

- Conditions section
- Lock status section
- Animation for expand/collapse

### Phase 3: Websocket Integration

- New `subscribe_slot_data` command
- Real-time updates for all fields
- Optimistic updates for user actions

### Phase 4: Advanced Features

- Inline editing for name/PIN
- Direct toggle for enabled switch
- Code reveal functionality
