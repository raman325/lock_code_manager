# Frontend Unification Plan

This document outlines the plan to unify and improve the Lock Code Manager frontend cards.

## Current State

### Cards in Use

1. **Lock Data Card** (`lock-code-manager-lock-data`)
   - Shows all slots for a specific lock
   - Grid of slot chips with status badges
   - Summary table at bottom
   - Heavy use of borders on chips

2. **Slot Card** (`lock-code-manager-slot`) - NEW
   - Shows detailed view of a single slot
   - Collapsible sections for conditions and lock status
   - Inline editing for name/PIN
   - Clean, borderless design

3. **Markdown/Entities Cards** (Dashboard Strategy)
   - Auto-generated via strategy
   - Uses HA native cards
   - Less integrated look

## Goals

1. Replace markdown/entities cards with custom slot cards
2. Align styling between lock data card and slot card
3. Improve visual clarity for disabled state
4. Fix badge layout to prevent text squeezing

---

## Phase 1: Style Unification

### 1.1 Remove Borders from Lock Data Card Chips

**Current:**

```css
.slot-chip {
    border: 2px solid rgba(var(--rgb-primary-text-color), 0.06);
    border-radius: 12px;
}

.slot-chip.active.managed {
    border-color: var(--primary-color);
}

.slot-chip.inactive.managed,
.slot-chip.disabled.managed {
    border-color: var(--primary-color);
    border-style: dotted;
}
```

**Proposed:**

```css
.slot-chip {
    background: rgba(var(--rgb-primary-text-color), 0.03);
    border-radius: 12px;
    /* No border */
}

.slot-chip.active.managed {
    background: linear-gradient(
        135deg,
        rgba(var(--rgb-primary-color), 0.08),
        rgba(var(--rgb-primary-color), 0.03)
    );
}

.slot-chip.inactive.managed,
.slot-chip.disabled.managed {
    background: rgba(var(--rgb-primary-color), 0.04);
    /* Use visual indicators other than border */
}
```

### 1.2 Remove Gap Between Header and Content

**Current:**

```css
:host {
    display: flex;
    flex-direction: column;
    gap: 8px;  /* Creates visible gap */
}
```

**Proposed:**

```css
:host {
    display: block;
}

ha-card {
    /* Merge with header visually */
    border-radius: 0 0 var(--ha-card-border-radius, 12px) var(--ha-card-border-radius, 12px);
}

.header-card {
    border-radius: var(--ha-card-border-radius, 12px) var(--ha-card-border-radius, 12px) 0 0;
    margin-bottom: 0;
}
```

**Alternative:** Consolidate into single `ha-card` with header section inside (like slot card).

### 1.3 Fix Badge Layout - Slot Label on Own Line

**Current layout:** `[Slot 1] [Active] [Managed]` - all on one line

**Problem:** Long slot numbers or names squeeze the text

**Proposed layout:**

```text
Slot 1
[Active] [Managed]
```

**CSS Changes:**

```css
.slot-top {
    flex-direction: column;
    align-items: flex-start;
    gap: 6px;
}

.slot-label {
    /* Full width, own line */
    width: 100%;
}

.slot-badges {
    /* Below slot label */
    flex-wrap: wrap;
}
```

---

## Phase 2: Improved Disabled Styling

The current disabled state uses:

- `opacity: 0.75` - subtle, easy to miss
- Dotted border - relies on border which we're removing

### Proposed Disabled Indicators

#### Option A: Strikethrough + Faded (Recommended)

```css
.slot-chip.disabled {
    opacity: 0.6;
}

.slot-chip.disabled .slot-name {
    text-decoration: line-through;
    color: var(--secondary-text-color);
}

.slot-chip.disabled .slot-code {
    text-decoration: line-through;
    opacity: 0.5;
}
```

**Pros:** Very clear that content is "crossed out"/disabled
**Cons:** May look harsh

#### Option B: Grayed Out with Disabled Icon

```css
.slot-chip.disabled {
    background: rgba(var(--rgb-primary-text-color), 0.04);
    opacity: 0.7;
}

.slot-chip.disabled::before {
    content: '';
    /* Add a small "disabled" icon overlay or badge */
}

.slot-chip.disabled .slot-status {
    background: rgba(var(--rgb-disabled-text-color), 0.15);
    color: var(--disabled-text-color);
}
```

**Pros:** Subtle but clear
**Cons:** Relies on color which may not be accessible

#### Option C: Left Border Indicator (Minimal Border Usage)

```css
.slot-chip.disabled {
    border-left: 3px solid var(--disabled-text-color);
    opacity: 0.7;
    padding-left: 12px;  /* Account for border */
}
```

**Pros:** Clear visual marker without full border
**Cons:** Inconsistent with "no borders" goal

#### Option D: Diagonal Stripe Pattern

```css
.slot-chip.disabled {
    background: repeating-linear-gradient(
        -45deg,
        rgba(var(--rgb-primary-text-color), 0.02),
        rgba(var(--rgb-primary-text-color), 0.02) 2px,
        transparent 2px,
        transparent 6px
    );
    opacity: 0.7;
}
```

**Pros:** Very obvious "disabled" pattern
**Cons:** May be too busy

### Recommendation

Use **Option A (Strikethrough)** combined with:

- Reduced opacity (0.6-0.7)
- Muted status badge colors
- Secondary text color for name

This provides clear visual feedback that the slot is disabled without relying on borders.

---

## Phase 3: Replace Markdown/Entities Cards

### Current Strategy Output

The dashboard strategy generates:

- Markdown cards for headers
- Entity cards for slot controls
- Separate cards for each slot

### Proposed Changes

Replace entity cards with slot cards:

```typescript
// In generate-view.ts or strategy

// Instead of:
{
    type: 'entities',
    entities: [...slotEntities]
}

// Generate:
{
    type: 'custom:lock-code-manager-slot',
    config_entry_id: configEntry.entryId,
    slot: slotNum
}
```

### Benefits

1. Consistent styling across dashboard
2. Inline editing capabilities
3. Real-time websocket updates
4. Collapsible sections reduce clutter

### Migration Path

1. Add slot card to strategy as option
2. Default to slot cards for new installs
3. Provide migration tool for existing dashboards
4. Eventually deprecate entities card approach

---

## Phase 4: Shared Style Constants

Create shared CSS custom properties or a shared styles module:

```typescript
// ts/shared-styles.ts
import { css } from 'lit';

export const lcmCardStyles = css`
    /* Card backgrounds */
    --lcm-card-bg: var(--ha-card-background, var(--card-background-color, #fff));
    --lcm-section-bg: rgba(var(--rgb-primary-text-color), 0.03);

    /* Status colors */
    --lcm-active-bg: rgba(var(--rgb-primary-color), 0.08);
    --lcm-inactive-bg: rgba(var(--rgb-primary-color), 0.04);
    --lcm-disabled-bg: rgba(var(--rgb-primary-text-color), 0.04);

    /* Badge styles */
    --lcm-badge-radius: 999px;
    --lcm-badge-padding: 2px 6px;
    --lcm-badge-font-size: 10px;

    /* Typography */
    --lcm-label-size: 11px;
    --lcm-code-font: 'Roboto Mono', monospace;
`;

export const lcmBadgeStyles = css`
    .status-badge {
        border-radius: var(--lcm-badge-radius);
        font-size: var(--lcm-badge-font-size);
        font-weight: 600;
        letter-spacing: 0.02em;
        padding: var(--lcm-badge-padding);
        text-transform: uppercase;
    }

    .status-badge.active {
        background: rgba(var(--rgb-success-color, 76, 175, 80), 0.16);
        color: var(--success-color, #4caf50);
    }

    .status-badge.inactive {
        background: rgba(var(--rgb-warning-color, 255, 152, 0), 0.12);
        color: var(--warning-color, #ff9800);
    }

    .status-badge.disabled {
        background: rgba(var(--rgb-disabled-text-color, 158, 158, 158), 0.15);
        color: var(--disabled-text-color, #9e9e9e);
    }
`;
```

---

## Implementation Order

### Milestone 1: Lock Data Card Restyling

- [ ] Remove chip borders
- [ ] Fix badge layout (slot label on own line)
- [ ] Improve disabled styling (strikethrough + opacity)
- [ ] Remove header/content gap

### Milestone 2: Strategy Integration

- [ ] Add slot card generation to strategy
- [ ] Create two different "sub" strategies which can be configured with an optional strategy_mode parameter (or some other
      parameter name that maes sense): one "classic" which is the same as the release before custom cards were
      introduced - no custom cards, just slots using the markdown and entities card. Second can be called modern
      (default): custom cards for slots, lock data included by default with PINs masked. In the modern view, everything
      is opt out, so we display all information (lock level slot sensor data on the slot card, masked_with_reveal, etc.)
      with configuration options to disable things. For users who have any "include" configuration parameters in their strategy
      definition, fallback to the classic strategy so the configuration options continue to work, but send a one time
      persistent notification announcing this change so the user knows to take a look. This allows us to make changes to
      defaults without breaking any existing dashboards. We should document these changes in the Breaking Changes
      section of the PR template just in case.
- [ ] Update documentation

### Milestone 3: Shared Styles

- [ ] Extract common styles to shared module
- [ ] Update both cards to use shared styles
- [ ] Ensure consistent look across cards

### Milestone 4: Testing & Polish

- [ ] Visual regression testing
- [ ] Dark mode verification
- [ ] Accessibility review (color contrast)
- [ ] Documentation updates

---

## Visual Mockups

### Lock Data Card - Before vs After

**Before:**

```text
┌─────────────────────────────────────────┐
│ 🔒 Front Door Lock – User Codes         │
└─────────────────────────────────────────┘
     ↑ gap ↑
┌─────────────────────────────────────────┐
│ ┌──────────────┐  ┌──────────────┐      │
│ │ Slot 1 [Act] │  │ Slot 2 [Dis] │      │
│ │ ──────────── │  │ ╌╌╌╌╌╌╌╌╌╌╌ │      │  ← dotted border
│ │ John         │  │ Jane         │      │
│ │ 1234         │  │ 5678         │      │
│ └──────────────┘  └──────────────┘      │
└─────────────────────────────────────────┘
```

**After:**

```text
┌─────────────────────────────────────────┐
│ 🔒 Front Door Lock – User Codes         │
├─────────────────────────────────────────┤  ← no gap
│ ┌──────────────┐  ┌──────────────┐      │
│ │ Slot 1       │  │ Slot 2       │      │  ← label on own line
│ │ [Active]     │  │ [Disabled]   │      │  ← badges below
│ │              │  │              │      │
│ │ John         │  │ ~~Jane~~     │      │  ← strikethrough
│ │ 1234         │  │ ~~5678~~     │      │
│ └──────────────┘  └──────────────┘      │  ← no borders, just bg
└─────────────────────────────────────────┘
```

---

## Open Questions

1. **Badge color for disabled:** Should it use warning color (orange) or disabled color (gray)?
   - Current: Warning orange
   - Proposed: Gray (more consistent with "disabled" semantics)

2. **Strikethrough on code:** Is it too harsh? Alternative: just fade without strikethrough

3. **Single card vs header+content:** Should we merge into single `ha-card` like slot card?

4. **Summary table styling:** Keep current or simplify?

---

## References

- Slot card implementation: `ts/slot-card.ts`
- Lock data card implementation: `ts/lock-code-data-card.ts`
- Dashboard strategy: `ts/dashboard-strategy.ts`
- View strategy: `ts/view-strategy.ts`
