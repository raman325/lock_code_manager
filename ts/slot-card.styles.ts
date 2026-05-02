/**
 * Styles for the LCM slot card component.
 *
 * Separated from slot-card.ts for readability. Composes shared styles
 * from shared-styles.ts with slot-card-specific layout rules.
 */
import { css } from 'lit';

import {
    lcmCollapsibleStyles,
    lcmCssVars,
    lcmEditableStyles,
    lcmReducedMotionStyles,
    lcmRevealButtonStyles,
    lcmSectionStyles,
    lcmStatusIndicatorStyles,
    lcmVisuallyHiddenStyles
} from './shared-styles';

const slotCardComponentStyles = css`
    :host {
        display: block;
    }

    ha-card {
        overflow: hidden;
    }

    /* Lit's reset is minimal — neutralize default heading margins so the
       newly-promoted h2/h3 elements (card title, collapsible section
       titles) keep the same visual rhythm as the prior <span>/<div>. */
    .header-title,
    .collapsible-title {
        margin: 0;
    }

    /* Helper and lock lists use ul/li for screen reader item-count
       announcements. Reset default list chrome so they render visually
       identical to the prior <div> wrappers. */
    .helpers-list,
    .lock-list {
        list-style: none;
        margin: 0;
        padding: 0;
    }
    .helpers-list > li,
    .lock-list > li {
        display: block;
    }

    /* Card-level state tinting — color the exception, not the norm. Active
       slots get no special tint (the default ha-card background); inactive
       (blocked by condition) and disabled-by-user states get a subtle tint
       so the exception reads at a glance. Opacity stops follow the
       3-stop system: 6% backgrounds / 12% surfaces / 16% chips. */
    ha-card.slot-card-state-inactive {
        background: rgba(var(--rgb-warning-color, 255, 167, 38), 0.06);
    }
    ha-card.slot-card-state-disabled {
        background: rgba(var(--rgb-primary-text-color), 0.06);
        opacity: 0.9;
    }

    /* Header Section — matches the lock card pattern: icon bubble + title + state chip on the right. */
    .header {
        align-items: center;
        border-bottom: 1px solid var(--lcm-border-color);
        display: flex;
        gap: 12px;
        padding: 16px;
    }

    .header-top {
        align-items: center;
        display: flex;
        flex: 1;
        gap: 12px;
        min-width: 0;
    }

    .header-icon {
        align-items: center;
        background: var(--lcm-active-bg);
        border-radius: 50%;
        color: var(--primary-color);
        display: flex;
        flex-shrink: 0;
        height: 40px;
        justify-content: center;
        width: 40px;
    }
    .header-icon ha-svg-icon {
        --mdc-icon-size: 24px;
    }

    .header-title {
        color: var(--primary-text-color);
        flex: 1;
        font-size: 18px;
        font-weight: 500;
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    .state-chip {
        align-items: center;
        border-radius: 12px;
        display: inline-flex;
        flex-shrink: 0;
        font-size: 11px;
        font-weight: 600;
        gap: 6px;
        max-width: 60%;
        overflow: hidden;
        padding: 4px 10px;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    .state-chip .dot {
        border-radius: 50%;
        flex-shrink: 0;
        height: 6px;
        width: 6px;
    }

    .state-chip.active {
        background: rgba(var(--rgb-success-color, 67, 160, 71), 0.16);
        color: var(--success-color, #43a047);
    }

    .state-chip.active .dot {
        background: var(--success-color, #43a047);
    }

    .state-chip.inactive {
        background: rgba(var(--rgb-warning-color, 255, 167, 38), 0.16);
        color: var(--warning-color, #ffa726);
    }

    .state-chip.inactive .dot {
        background: var(--warning-color, #ffa726);
    }

    .state-chip.disabled {
        background: rgba(var(--rgb-disabled-color, 117, 117, 117), 0.2);
        color: var(--secondary-text-color);
    }

    .state-chip.disabled .dot {
        background: var(--disabled-color, #757575);
    }

    /* Content Sections */
    .content {
        display: flex;
        flex-direction: column;
        gap: 12px;
        padding: 12px 16px 16px;
    }

    /* Hero band — Name row on top, PIN + Enable row below. The name acts as
       the visual anchor of the card (matching the PIN's weight) and the
       form-like row labels are dropped — typography self-describes. The PIN
       row keeps its label since "••••" isn't self-evident. */
    .hero {
        background: var(--lcm-section-bg);
        border-radius: 12px;
        display: flex;
        flex-direction: column;
        gap: 10px;
        padding: 12px 16px;
    }

    .hero-row {
        align-items: center;
        display: flex;
        gap: 16px;
    }

    .hero-field {
        align-items: center;
        display: flex;
        flex: 1;
        gap: 12px;
        min-width: 0;
    }

    /* Only the PIN row uses .hero-field-label now; the name and switch
       self-describe. No min-width column-alignment is needed. */
    .hero-field-label {
        color: var(--secondary-text-color);
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 0.08em;
        text-transform: uppercase;
    }

    .hero-name-value {
        color: var(--primary-text-color);
        font-size: 22px;
        font-weight: 600;
    }

    .hero-name-value.editable {
        border-radius: 4px;
        cursor: pointer;
        margin: 0 -4px;
        padding: 0 4px;
        text-decoration: none;
        transition: background 0.15s ease;
    }
    .hero-name-value.editable:hover {
        background: var(--lcm-section-bg-hover, rgba(127, 127, 127, 0.08));
    }
    .hero-name-value:focus-visible {
        background: var(--lcm-section-bg-hover, rgba(127, 127, 127, 0.08));
        outline: 2px solid var(--primary-color);
        outline-offset: 2px;
    }

    .hero-pin {
        align-items: center;
        display: flex;
        flex: 1;
        gap: 12px;
        min-width: 0;
    }

    .hero-pin-value {
        color: var(--primary-text-color);
        cursor: pointer;
        font-family: var(--lcm-code-font);
        font-size: 22px;
        letter-spacing: 4px;
    }

    .hero-pin-value.masked {
        color: var(--secondary-text-color);
    }

    /* Override the shared .editable dashed-underline affordance for the PIN.
       At 22px monospace with letter-spacing, the dashed underline renders as
       broken dashes that look like a bug. Use a subtle hover background instead. */
    .hero-pin-value.editable {
        border-radius: 4px;
        margin: 0 -4px;
        padding: 0 4px;
        text-decoration: none;
        transition: background 0.15s ease;
    }

    .hero-pin-value.editable:hover {
        background: var(--lcm-section-bg-hover, rgba(127, 127, 127, 0.08));
    }

    .hero-pin-value:focus-visible {
        background: var(--lcm-section-bg-hover, rgba(127, 127, 127, 0.08));
        border-radius: 4px;
        outline: 2px solid var(--primary-color);
        outline-offset: 2px;
    }

    .hero-pin .reveal {
        /* 32px hit target — matches .lcm-reveal-button. */
        --mdc-icon-button-size: 32px;
        --mdc-icon-size: 16px;
        color: var(--secondary-text-color);
    }

    .hero-toggle {
        align-items: center;
        display: flex;
        gap: 10px;
    }

    .hero-toggle-label {
        color: var(--secondary-text-color);
        font-size: 12px;
    }

    .placeholder {
        color: var(--secondary-text-color);
        font-style: italic;
    }

    /* Name-specific edit input (extends shared editable styles) */
    .name-edit-input {
        font-family: var(--lcm-code-font);
        font-size: var(--lcm-code-font-size);
        font-weight: var(--lcm-code-font-weight);
    }

    /* PIN-specific edit input (extends shared editable styles) */
    .pin-edit-input {
        font-family: var(--lcm-code-font);
        font-size: 22px;
        font-weight: var(--lcm-code-font-weight);
        letter-spacing: 4px;
    }

    /* Conditions section — body styles for the collapsible.
       The body composes an HA entity row (rendered via loadCardHelpers) inside
       a tinted block, with an LCM overlay strip below it that explains what
       the entity's state means for *this slot*. A Manage / Add link sits
       directly under the block, and helpers (config-level extras) render as
       plain HA entity rows under a small HELPERS sub-label. */

    .condition-block {
        background: var(--lcm-section-bg);
        border-radius: 8px;
        overflow: hidden;
    }

    .lcm-overlay {
        align-items: center;
        border-top: 1px solid var(--lcm-border-color);
        display: flex;
        gap: 10px;
        padding: 8px 12px;
    }

    .lcm-overlay.allowing {
        background: rgba(var(--rgb-success-color, 67, 160, 71), 0.06);
    }

    .lcm-overlay.blocking {
        background: rgba(var(--rgb-warning-color, 255, 167, 38), 0.06);
    }

    .lcm-overlay-status {
        flex-shrink: 0;
        font-size: 12px;
        font-weight: 600;
    }

    .lcm-overlay.allowing .lcm-overlay-status {
        color: var(--success-color, #43a047);
    }

    .lcm-overlay.blocking .lcm-overlay-status {
        color: var(--warning-color, #ffa726);
    }

    .lcm-overlay-context {
        color: var(--secondary-text-color);
        flex: 1;
        font-size: 12px;
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    /* Remove condition link — sits below the condition block. Warning color
       since it's destructive (clears the condition); user can re-Add via
       the empty-state button if they want to switch entities. */
    .remove-link {
        align-items: center;
        color: var(--warning-color, #ffa726);
        cursor: pointer;
        display: inline-flex;
        font-size: 12px;
        gap: 6px;
        margin: 10px 0 4px;
        padding: 4px 0;
    }

    .remove-link:hover {
        color: var(--error-color, #ef5350);
    }

    .remove-link:focus-visible {
        border-radius: 4px;
        outline: 2px solid var(--warning-color, #ffa726);
        outline-offset: 2px;
    }

    .remove-link ha-svg-icon {
        --mdc-icon-size: 14px;
    }

    /* Add condition button — sits in the headerExtra slot of the Conditions
       section (collapsed row) when no condition is set. Replaces the muted
       "none" badge with a directly-actionable button. */
    .add-condition-btn {
        align-items: center;
        background: var(--lcm-section-bg-hover, rgba(127, 127, 127, 0.08));
        border: none;
        border-radius: 12px;
        color: var(--primary-color);
        cursor: pointer;
        display: inline-flex;
        font-family: inherit;
        font-size: 12px;
        font-weight: 600;
        gap: 4px;
        padding: 4px 10px;
    }
    .add-condition-btn:hover {
        background: var(--primary-color);
        color: var(--text-primary-color, #fff);
    }
    .add-condition-btn ha-svg-icon {
        --mdc-icon-size: 14px;
    }
    .add-condition-btn:focus-visible {
        outline: 2px solid var(--primary-color);
        outline-offset: 2px;
    }

    /* Static (non-collapsible) variant of .collapsible-header used when the
       Conditions section has no content to expand to — no chevron, no hover,
       no toggle handler. Same outer chrome (.collapsible-section) so it
       visually aligns with sibling sections. */
    .collapsible-header.static {
        cursor: default;
    }
    .collapsible-header.static:hover {
        background: transparent;
    }

    .helpers-label {
        color: var(--secondary-text-color);
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 0.08em;
        margin: 14px 0 4px 4px;
        text-transform: uppercase;
    }

    .helpers-list {
        background: var(--lcm-section-bg);
        border-radius: 8px;
        overflow: hidden;
    }

    .helpers-list > * + * {
        border-top: 1px solid var(--lcm-border-color);
    }

    .entity-row-loading {
        color: var(--disabled-text-color);
        font-size: 12px;
        padding: 10px 12px;
    }

    .entity-row-error {
        color: var(--disabled-text-color);
        font-size: 12px;
        font-style: italic;
        padding: 10px 12px;
    }

    .no-conditions {
        color: var(--secondary-text-color);
        font-size: 13px;
        font-style: italic;
    }

    /* Lock Status Section */
    .lock-row {
        align-items: center;
        border-bottom: 1px solid var(--lcm-border-color);
        display: flex;
        gap: 12px;
        padding: 10px 0;
    }

    .lock-list > li:last-child .lock-row {
        border-bottom: none;
        padding-bottom: 0;
    }

    .lock-list > li:first-child .lock-row {
        padding-top: 0;
    }

    .lock-name {
        color: var(--primary-text-color);
        cursor: pointer;
        flex: 1;
        font-size: 14px;
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    .lock-name:hover {
        color: var(--primary-color);
        text-decoration: underline;
    }

    .lock-name:focus-visible {
        border-radius: 4px;
        color: var(--primary-color);
        outline: 2px solid var(--primary-color);
        outline-offset: 2px;
    }

    /* Collapsible-header focus ring — the section already has rounded
       corners, so use an inset outline so it doesn't bleed past them. */
    .collapsible-header:focus-visible {
        outline: 2px solid var(--primary-color);
        outline-offset: -2px;
    }

    .lock-info {
        display: flex;
        flex: 1;
        flex-direction: column;
        gap: 2px;
        min-width: 0;
    }

    .lock-synced-time {
        color: var(--secondary-text-color);
        font-size: 11px;
    }

    .lock-synced-time.synced {
        color: var(--lcm-success-color);
    }

    .lock-synced-time.pending {
        color: var(--lcm-warning-color);
    }

    .lock-synced-time.suspended {
        color: var(--lcm-error-color);
        font-weight: 500;
    }

    .lock-synced-time.syncing {
        color: var(--lcm-warning-color);
    }

    .lock-code-field {
        align-items: center;
        display: flex;
        gap: 4px;
    }

    .lock-code-value {
        color: var(--primary-text-color);
        font-family: var(--lcm-code-font);
        font-size: 13px;
        font-weight: 500;
        letter-spacing: var(--lcm-code-letter-spacing);
    }

    /* Event row — single click-through row at the bottom of the card that
       opens the more-info dialog for the slot's PIN-used event entity. Sits
       below the content flex container so the gap above provides natural
       separation (no top border needed). */
    .event-row {
        align-items: center;
        cursor: pointer;
        display: flex;
        gap: 10px;
        padding: 12px 16px;
    }

    /* Static (non-interactive) event row — used when there's no usage
       history to navigate to. Drops the cursor and hover affordances so
       the row doesn't read as clickable. */
    .event-row.event-row-static {
        cursor: default;
    }
    .event-row.event-row-static:hover {
        background: transparent;
    }

    .event-row:not(.event-row-static):hover {
        background: rgba(var(--rgb-primary-text-color), 0.06);
    }

    .event-row:focus-visible {
        background: rgba(var(--rgb-primary-text-color), 0.06);
        outline: 2px solid var(--primary-color);
        outline-offset: -2px;
    }

    .event-icon {
        --mdc-icon-size: 14px;
        color: var(--secondary-text-color);
        flex-shrink: 0;
    }

    .event-name {
        color: var(--primary-text-color);
        font-size: 13px;
        font-weight: 500;
    }

    .event-meta {
        color: var(--secondary-text-color);
        flex: 1;
        font-size: 12px;
    }

    .event-arrow {
        --mdc-icon-size: 14px;
        color: var(--disabled-text-color);
    }

    /* Message states */
    .message {
        color: var(--secondary-text-color);
        font-style: italic;
        padding: 16px;
        text-align: center;
    }

    .error {
        color: var(--error-color);
    }

    /* Action error banner — font-weight 600 promotes the text to "bold"
       per WCAG 1.4.3, which lowers the contrast threshold from 4.5:1 to
       3:1. White-on-#db4437 is ~4.21:1, which fails AA for normal text
       but passes the bold threshold. */
    .action-error {
        align-items: center;
        background: var(--error-color, #db4437);
        color: white;
        display: flex;
        font-size: 14px;
        font-weight: 600;
        gap: 8px;
        justify-content: space-between;
        padding: 8px 16px;
    }

    .action-error-dismiss {
        /* 28x28 minimum hit target via padding — pads from the previous
           4px to 6px so the button is comfortably tappable on touch
           devices. */
        background: none;
        border: none;
        color: white;
        cursor: pointer;
        font-size: 16px;
        line-height: 1;
        min-height: 28px;
        min-width: 28px;
        opacity: 0.8;
        padding: 6px 8px;
    }

    .action-error-dismiss:hover {
        opacity: 1;
    }

    /* Manage Condition dialog — buttons render inline in the body since
       ha-button slot rendering is unreliable in the standalone card context. */
    .dialog-content {
        display: flex;
        flex-direction: column;
        gap: 16px;
        padding: 0 8px;
        min-width: 320px;
    }

    .dialog-description {
        color: var(--secondary-text-color);
        font-size: 13px;
        line-height: 1.5;
        margin: 0;
    }

    .dialog-content ha-entity-picker {
        display: block;
    }

    .dialog-saving {
        align-self: center;
        color: var(--secondary-text-color);
        font-size: 12px;
        font-style: italic;
    }
`;

export const slotCardStyles = [
    lcmCssVars,
    lcmSectionStyles,
    lcmStatusIndicatorStyles,
    lcmRevealButtonStyles,
    lcmCollapsibleStyles,
    lcmEditableStyles,
    lcmVisuallyHiddenStyles,
    lcmReducedMotionStyles,
    slotCardComponentStyles
];
