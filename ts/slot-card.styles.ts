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
    lcmRevealButtonStyles,
    lcmSectionStyles,
    lcmStatusIndicatorStyles
} from './shared-styles';

const slotCardComponentStyles = css`
    :host {
        display: block;
    }

    ha-card {
        overflow: hidden;
    }

    /* Header Section */
    .header {
        padding: 14px 16px 16px;
    }

    .header-top {
        align-items: center;
        display: flex;
        gap: 12px;
        justify-content: space-between;
        margin-bottom: 6px;
        min-height: 22px;
    }

    .slot-kicker {
        color: var(--secondary-text-color);
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 0.08em;
        text-transform: uppercase;
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

    .name {
        align-items: baseline;
        color: var(--primary-text-color);
        display: flex;
        font-size: 22px;
        font-weight: 600;
        gap: 6px;
        letter-spacing: -0.01em;
        line-height: 1.2;
    }

    .name .placeholder {
        color: var(--disabled-text-color);
        font-style: italic;
        font-weight: 500;
    }

    .name .pencil {
        --mdc-icon-button-size: 28px;
        --mdc-icon-size: 14px;
        color: var(--disabled-text-color);
    }

    /* Content Sections */
    .content {
        display: flex;
        flex-direction: column;
        gap: 16px;
        padding: 16px;
    }

    /* Hero row (PIN + Enable) — tinted always-visible band at the top of .content */
    .hero {
        align-items: center;
        background: var(--lcm-section-bg);
        border-top: 1px solid var(--lcm-border-color);
        display: flex;
        gap: 16px;
        padding: 18px 16px;
    }

    .hero-pin {
        align-items: center;
        display: flex;
        flex: 1;
        gap: 12px;
        min-width: 0;
    }

    .hero-pin-label {
        color: var(--secondary-text-color);
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 0.08em;
        text-transform: uppercase;
    }

    .hero-pin-value {
        color: var(--primary-text-color);
        cursor: pointer;
        font-family: var(--lcm-code-font);
        font-size: 22px;
        letter-spacing: 4px;
        min-height: 1.5em;
    }

    .hero-pin-value.masked {
        color: var(--secondary-text-color);
    }

    .hero-pin .reveal {
        --mdc-icon-button-size: 28px;
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
        background: rgba(var(--rgb-success-color, 67, 160, 71), 0.08);
    }

    .lcm-overlay.blocking {
        background: rgba(var(--rgb-warning-color, 255, 167, 38), 0.1);
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

    .manage-link {
        align-items: center;
        color: var(--secondary-text-color);
        cursor: pointer;
        display: inline-flex;
        font-size: 12px;
        gap: 6px;
        margin: 10px 0 4px;
        padding: 4px 0;
    }

    .manage-link:hover {
        color: var(--primary-text-color);
    }

    .manage-link ha-svg-icon {
        --mdc-icon-size: 14px;
    }

    .helpers-label {
        color: var(--secondary-text-color);
        font-size: 10px;
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

    .empty-state {
        background: var(--lcm-section-bg);
        border-radius: 8px;
        color: var(--secondary-text-color);
        font-size: 12px;
        line-height: 1.5;
        padding: 12px;
        text-align: center;
    }

    .add-link {
        color: var(--primary-color);
        cursor: pointer;
        display: inline-block;
        font-size: 12px;
        font-weight: 500;
        margin-top: 6px;
    }

    .entity-row-loading {
        color: var(--disabled-text-color);
        font-size: 12px;
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

    .lock-row:last-child {
        border-bottom: none;
        padding-bottom: 0;
    }

    .lock-row:first-child {
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

    .event-row:hover {
        background: rgba(var(--rgb-primary-text-color), 0.06);
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

    /* Action error banner */
    .action-error {
        align-items: center;
        background: var(--error-color, #db4437);
        color: white;
        display: flex;
        font-size: 14px;
        gap: 8px;
        justify-content: space-between;
        padding: 8px 16px;
    }

    .action-error-dismiss {
        background: none;
        border: none;
        color: white;
        cursor: pointer;
        font-size: 16px;
        opacity: 0.8;
        padding: 4px;
    }

    .action-error-dismiss:hover {
        opacity: 1;
    }

    /* Dialog styles */
    .dialog-content {
        min-width: 300px;
        padding: 0 8px;
    }

    .dialog-description {
        color: var(--secondary-text-color);
        font-size: 13px;
        line-height: 1.5;
        margin: 0 0 16px;
    }

    .dialog-content ha-entity-picker {
        display: block;
    }

    ha-button.destructive {
        --mdc-theme-primary: var(--warning-color, #ffa726);
    }

    /* Make dialog buttons more obviously interactive */
    ha-dialog ha-button {
        cursor: pointer;
    }

    ha-dialog ha-button:hover {
        opacity: 0.8;
    }
`;

export const slotCardStyles = [
    lcmCssVars,
    lcmSectionStyles,
    lcmStatusIndicatorStyles,
    lcmRevealButtonStyles,
    lcmCollapsibleStyles,
    lcmEditableStyles,
    slotCardComponentStyles
];
