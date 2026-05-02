/**
 * Styles for the LCM lock-codes card component.
 *
 * Separated from lock-codes-card.ts for readability. Composes shared
 * styles from shared-styles.ts with lock-codes-card-specific layout rules.
 */
import { css } from 'lit';

import { lcmBadgeStyles, lcmCodeStyles, lcmCssVars, lcmRevealButtonStyles } from './shared-styles';

const lockCodesCardComponentStyles = css`
    :host {
        display: block;
    }

    ha-card {
        padding: 0;
    }

    .card-header {
        align-items: center;
        border-bottom: 1px solid var(--lcm-border-color);
        display: flex;
        gap: 12px;
        padding: 16px;
    }

    .header-icon {
        align-items: center;
        background: var(--lcm-active-bg);
        border-radius: 50%;
        color: var(--primary-color);
        display: flex;
        height: 40px;
        justify-content: center;
        width: 40px;
    }

    .header-icon ha-icon {
        --mdc-icon-size: 24px;
    }

    .card-header-title {
        color: var(--primary-text-color);
        font-size: 18px;
        font-weight: 500;
    }

    .card-content {
        padding: 16px;
    }

    .slots-grid {
        display: grid;
        gap: 10px;
        grid-template-columns: repeat(2, 1fr);
    }

    @media (max-width: 400px) {
        .slots-grid {
            grid-template-columns: 1fr;
        }
    }

    .slot-chip {
        background: var(--lcm-section-bg);
        border-radius: 12px;
        display: flex;
        flex-direction: column;
        gap: 6px;
        min-width: 0;
        overflow: hidden;
        padding: 12px 12px 14px;
        position: relative;
    }

    /* Active Lock Code Manager Managed: Primary blue with tinted background */
    .slot-chip.active.managed {
        background: var(--lcm-active-bg-gradient);
    }

    /* Active Unmanaged (not Lock Code Manager): Neutral gray, plain background */
    .slot-chip.active.unmanaged {
        background: linear-gradient(
            135deg,
            rgba(var(--rgb-primary-text-color), 0.06),
            rgba(var(--rgb-primary-text-color), 0.02)
        );
    }

    /* Inactive Lock Code Manager Managed: Muted blue, slightly faded */
    .slot-chip.inactive.managed {
        background: rgba(var(--rgb-primary-color), 0.05);
        opacity: 0.85;
    }

    /* Disabled Lock Code Manager Managed: Very muted, clear disabled state */
    .slot-chip.disabled.managed {
        background: rgba(var(--rgb-primary-text-color), 0.04);
        opacity: 0.65;
    }

    .slot-chip.empty {
        background: var(--lcm-section-bg);
        opacity: 0.7;
    }

    .slot-chip.full-width {
        grid-column: 1 / -1;
        justify-self: center;
        max-width: 360px;
        width: 100%;
    }

    .slot-chip.clickable {
        cursor: pointer;
        transition:
            transform 0.1s ease,
            box-shadow 0.2s ease;
    }

    .slot-chip.clickable:hover {
        box-shadow: 0 2px 8px rgba(var(--rgb-primary-color), 0.25);
        transform: translateY(-1px);
    }

    .slot-chip.clickable:active {
        transform: translateY(0);
    }

    .slot-top {
        align-items: flex-start;
        display: flex;
        flex-direction: column;
        gap: 6px;
    }

    .slot-badges {
        align-items: center;
        display: inline-flex;
        flex-wrap: wrap;
        gap: 6px;
    }

    .slot-label {
        color: var(--secondary-text-color);
        font-size: var(--lcm-section-header-size);
        font-weight: 500;
        letter-spacing: 0.03em;
        text-transform: uppercase;
        width: 100%;
    }

    .slot-name {
        color: var(--primary-text-color);
        font-size: 15px;
        font-weight: 500;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    .slot-name.unnamed {
        color: var(--secondary-text-color);
        font-weight: 400;
    }

    /* Wrapper around the slot name; lays out the optional pending icon
       alongside the name with a small gap. Inline-flex so the row only takes
       the space it needs and the chip's column layout still controls width. */
    .slot-name-row {
        align-items: center;
        display: inline-flex;
        gap: 4px;
        max-width: 100%;
        min-width: 0;
    }

    /* Slot disabled by user — name shown in a muted pill, no strikethrough.
       Mirrors the .lcm-code.off treatment for visual consistency. */
    .slot-chip.disabled .slot-name {
        background: var(--lcm-section-bg, rgba(127, 127, 127, 0.05));
        border-radius: 6px;
        color: var(--disabled-text-color);
        padding: 2px 8px;
    }

    /* Slot enabled but lock doesn't have the code yet — clock-icon prefix on
       the name. Mirrors the .lcm-code.pending treatment. The disabled rule's
       pill background takes precedence if a slot is somehow both. */
    .slot-chip.pending .slot-name-pending-icon {
        --mdc-icon-size: 12px;
        color: var(--secondary-text-color);
        flex-shrink: 0;
    }

    .slot-chip.pending .slot-name {
        color: var(--secondary-text-color);
    }

    .slot-code-row {
        align-items: center;
        display: flex;
        gap: 8px;
        justify-content: space-between;
    }

    .slot-code-actions {
        display: inline-flex;
    }

    /* Editable code for unmanaged slots */
    .slot-code-edit {
        display: flex;
        flex-direction: column;
        gap: 4px;
        width: 100%;
    }

    .slot-code-edit-row {
        align-items: center;
        display: flex;
        gap: 8px;
    }

    .slot-code-input {
        background: var(--card-background-color, #fff);
        border: 1px solid var(--primary-color);
        border-radius: 6px;
        color: var(--primary-text-color);
        flex: 1;
        font-family: var(--lcm-code-font);
        font-size: 14px;
        font-weight: 500;
        letter-spacing: var(--lcm-code-letter-spacing);
        min-width: 0;
        outline: none;
        padding: 6px 10px;
    }

    .slot-code-input:focus {
        box-shadow: 0 0 0 1px var(--primary-color);
    }

    .slot-code-input::placeholder {
        color: var(--secondary-text-color);
        font-weight: 400;
        letter-spacing: normal;
    }

    .slot-code-edit-buttons {
        display: flex;
        gap: 4px;
    }

    .slot-code-edit-buttons ha-icon-button {
        --mdc-icon-button-size: 32px;
        --mdc-icon-size: 18px;
    }

    .slot-edit-help {
        color: var(--secondary-text-color);
        font-size: 10px;
    }

    /* Editable code display (click to edit) */
    .lcm-code.editable {
        border-radius: 4px;
        cursor: pointer;
        margin: -2px -4px;
        padding: 2px 4px;
        transition: background-color 0.2s;
    }

    .lcm-code.editable:hover {
        background: var(--lcm-active-bg);
    }

    .empty-summary {
        align-items: center;
        background: var(--lcm-section-bg);
        border: 1px dashed var(--lcm-border-color-strong);
        border-radius: 10px;
        color: var(--secondary-text-color);
        display: flex;
        font-size: 12px;
        gap: 8px;
        grid-column: 1 / -1;
        padding: 8px 12px;
    }

    .empty-summary ha-icon {
        --mdc-icon-size: 16px;
        color: var(--secondary-text-color);
    }

    .empty-summary-label {
        color: var(--secondary-text-color);
        font-size: var(--lcm-section-header-size);
        font-weight: 600;
        letter-spacing: 0.04em;
        text-transform: uppercase;
    }

    .empty-summary-range {
        color: var(--primary-text-color);
        font-size: 13px;
        font-weight: 500;
    }

    .message {
        color: var(--secondary-text-color);
        font-style: italic;
    }

    /* Summary table */
    .summary-table {
        border-collapse: collapse;
        font-size: 12px;
        margin-top: 16px;
        table-layout: fixed;
        width: 100%;
    }

    .summary-table th,
    .summary-table td {
        overflow: hidden;
        padding: 6px 4px;
        text-align: center;
        text-overflow: ellipsis;
    }

    .summary-table th {
        background: rgba(var(--rgb-primary-text-color), 0.04);
        color: var(--secondary-text-color);
        font-size: 10px;
        font-weight: 600;
        letter-spacing: 0.04em;
        text-transform: uppercase;
    }

    .summary-table th:first-child {
        border-radius: 6px 0 0 0;
        text-align: left;
    }

    .summary-table th:last-child {
        border-radius: 0 6px 0 0;
    }

    .summary-table td {
        border-top: 1px solid var(--lcm-border-color);
        color: var(--primary-text-color);
        font-weight: 500;
    }

    .summary-table td:first-child {
        color: var(--secondary-text-color);
        font-size: var(--lcm-section-header-size);
        font-weight: 600;
        letter-spacing: 0.03em;
        text-align: left;
        text-transform: uppercase;
    }

    .summary-table tr:last-child td:first-child {
        border-radius: 0 0 0 6px;
    }

    .summary-table tr:last-child td:last-child {
        border-radius: 0 0 6px 0;
    }

    .summary-table .total-row td {
        background: rgba(var(--rgb-primary-text-color), 0.02);
        border-top: 2px solid var(--lcm-border-color-strong);
        font-weight: 600;
    }

    .summary-cell-zero {
        color: var(--disabled-text-color) !important;
        font-weight: 400 !important;
    }

    ha-card.suspended {
        border: 1px solid var(--lcm-error-color);
        opacity: 0.85;
    }

    .suspended-banner {
        align-items: center;
        background: rgba(244, 67, 54, 0.08);
        border-bottom: 1px solid var(--lcm-border-color);
        color: var(--lcm-error-color);
        display: flex;
        font-size: 12px;
        font-weight: 500;
        gap: 8px;
        padding: 8px 16px;
    }

    .suspended-banner ha-icon {
        --mdc-icon-size: 16px;
    }
`;

export const lockCodesCardStyles = [
    lcmCssVars,
    lcmBadgeStyles,
    lcmCodeStyles,
    lcmRevealButtonStyles,
    lockCodesCardComponentStyles
];
