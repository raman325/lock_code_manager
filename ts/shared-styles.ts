/**
 * Shared styles for Lock Code Manager frontend cards.
 *
 * This module provides CSS custom properties and shared style fragments
 * to ensure consistent styling between lock-data card and slot card.
 */
import { css } from 'lit';

/**
 * CSS custom properties for Lock Code Manager cards.
 * Import and include in static styles for any LCM card.
 */
export const lcmCssVars = css`
    :host {
        /* Section backgrounds */
        --lcm-section-bg: rgba(var(--rgb-primary-text-color), 0.03);
        --lcm-section-bg-hover: rgba(var(--rgb-primary-text-color), 0.06);

        /* Active states */
        --lcm-active-bg: rgba(var(--rgb-primary-color), 0.1);
        --lcm-active-bg-gradient: linear-gradient(
            135deg,
            rgba(var(--rgb-primary-color), 0.1),
            rgba(var(--rgb-primary-color), 0.04)
        );

        /* Status colors */
        --lcm-success-color: var(--success-color, #4caf50);
        --lcm-warning-color: var(--warning-color, #ff9800);
        --lcm-disabled-color: var(--disabled-text-color, #9e9e9e);

        /* Badge styling */
        --lcm-badge-radius: 999px;
        --lcm-badge-font-size: 10px;
        --lcm-badge-font-weight: 600;
        --lcm-badge-letter-spacing: 0.02em;
        --lcm-badge-padding: 2px 6px;

        /* Section header typography */
        --lcm-section-header-size: 11px;
        --lcm-section-header-weight: 600;
        --lcm-section-header-spacing: 0.05em;

        /* Code/PIN typography */
        --lcm-code-font: 'Roboto Mono', monospace;
        --lcm-code-font-size: 16px;
        --lcm-code-font-weight: 600;
        --lcm-code-letter-spacing: 1px;

        /* Border colors */
        --lcm-border-color: rgba(var(--rgb-primary-text-color), 0.06);
        --lcm-border-color-strong: rgba(var(--rgb-primary-text-color), 0.12);
    }
`;

/**
 * Shared status badge styles.
 * Classes: .lcm-badge, .lcm-badge.active, .lcm-badge.inactive, .lcm-badge.disabled, .lcm-badge.empty
 */
export const lcmBadgeStyles = css`
    .lcm-badge {
        border-radius: var(--lcm-badge-radius);
        font-size: var(--lcm-badge-font-size);
        font-weight: var(--lcm-badge-font-weight);
        letter-spacing: var(--lcm-badge-letter-spacing);
        padding: var(--lcm-badge-padding);
        text-transform: uppercase;
    }

    .lcm-badge.active {
        background: rgba(var(--rgb-primary-color), 0.16);
        color: var(--primary-color);
    }

    .lcm-badge.inactive {
        background: rgba(var(--rgb-primary-color), 0.12);
        color: var(--primary-color);
    }

    .lcm-badge.disabled {
        background: rgba(var(--rgb-disabled-text-color, 158, 158, 158), 0.15);
        color: var(--lcm-disabled-color);
    }

    .lcm-badge.empty {
        background: rgba(var(--rgb-primary-text-color), 0.08);
        color: var(--secondary-text-color);
    }

    .lcm-badge.managed {
        background: rgba(var(--rgb-primary-color), 0.16);
        color: var(--primary-color);
    }

    .lcm-badge.external {
        background: rgba(var(--rgb-primary-text-color), 0.08);
        color: var(--secondary-text-color);
    }
`;

/**
 * Shared status indicator styles (sync icons, status dots).
 * Classes: .lcm-sync-icon.synced, .lcm-sync-icon.pending, .lcm-sync-icon.unknown
 */
export const lcmStatusIndicatorStyles = css`
    .lcm-sync-icon {
        --mdc-icon-size: 18px;
    }

    .lcm-sync-icon.synced {
        color: var(--lcm-success-color);
    }

    .lcm-sync-icon.pending {
        color: var(--lcm-warning-color);
    }

    .lcm-sync-icon.unknown {
        color: var(--lcm-disabled-color);
    }

    .lcm-status-dot {
        border-radius: 50%;
        height: 12px;
        width: 12px;
    }

    .lcm-status-dot.active {
        background-color: var(--lcm-success-color);
    }

    .lcm-status-dot.inactive {
        background-color: var(--lcm-warning-color);
    }

    .lcm-status-dot.disabled {
        background-color: var(--lcm-disabled-color);
    }
`;

/**
 * Shared PIN/code display styles.
 * Classes: .lcm-code, .lcm-code.masked, .lcm-code.disabled, .lcm-code.no-code
 */
export const lcmCodeStyles = css`
    .lcm-code {
        color: var(--primary-text-color);
        font-family: var(--lcm-code-font);
        font-size: var(--lcm-code-font-size);
        font-weight: var(--lcm-code-font-weight);
        letter-spacing: var(--lcm-code-letter-spacing);
    }

    .lcm-code.masked {
        color: var(--secondary-text-color);
    }

    .lcm-code.disabled {
        color: var(--secondary-text-color);
        text-decoration: line-through;
    }

    .lcm-code.no-code {
        color: var(--disabled-text-color);
        font-family: inherit;
        font-size: 12px;
        font-style: italic;
        font-weight: 400;
        letter-spacing: normal;
    }
`;

/**
 * Shared section styles.
 */
export const lcmSectionStyles = css`
    .lcm-section {
        background: var(--lcm-section-bg);
        border-radius: 12px;
        padding: 16px;
    }

    .lcm-section-header {
        color: var(--secondary-text-color);
        font-size: var(--lcm-section-header-size);
        font-weight: var(--lcm-section-header-weight);
        letter-spacing: var(--lcm-section-header-spacing);
        margin-bottom: 12px;
        text-transform: uppercase;
    }
`;

/**
 * Shared reveal button styles.
 */
export const lcmRevealButtonStyles = css`
    .lcm-reveal-button {
        --mdc-icon-button-size: 28px;
        --mdc-icon-size: 16px;
        color: var(--secondary-text-color);
    }
`;

/**
 * Shared collapsible section styles.
 */
export const lcmCollapsibleStyles = css`
    .collapsible-section {
        background: var(--lcm-section-bg);
        border-radius: 12px;
        overflow: hidden;
    }

    .collapsible-header {
        align-items: center;
        cursor: pointer;
        display: flex;
        justify-content: space-between;
        padding: 12px 16px;
        user-select: none;
    }

    .collapsible-header:hover {
        background: var(--lcm-section-bg-hover);
    }

    .collapsible-title {
        align-items: center;
        color: var(--secondary-text-color);
        display: flex;
        font-size: var(--lcm-section-header-size);
        font-weight: var(--lcm-section-header-weight);
        gap: 8px;
        letter-spacing: var(--lcm-section-header-spacing);
        text-transform: uppercase;
    }

    .collapsible-badge {
        background: var(--lcm-active-bg);
        border-radius: 10px;
        color: var(--primary-color);
        font-size: var(--lcm-badge-font-size);
        padding: 2px 8px;
    }

    .collapsible-badge.primary {
        background: var(--primary-color);
        color: var(--text-primary-color, #fff);
    }

    .collapsible-badge.warning {
        background: var(--warning-color, #ffa600);
        color: var(--text-primary-color, #fff);
    }

    .collapsible-chevron {
        --mdc-icon-size: 20px;
        color: var(--secondary-text-color);
        transition: transform 0.2s ease;
    }

    .collapsible-content {
        max-height: 0;
        opacity: 0;
        overflow: hidden;
        padding: 0 16px;
        transition:
            max-height 0.3s ease,
            opacity 0.2s ease,
            padding 0.3s ease;
    }

    .collapsible-content.expanded {
        max-height: 500px;
        opacity: 1;
        padding: 0 16px 16px;
    }
`;

/**
 * Shared editable field styles for inline editing.
 */
export const lcmEditableStyles = css`
    .editable {
        border-radius: 4px;
        cursor: pointer;
        margin: -4px -8px;
        padding: 4px 8px;
        transition: background-color 0.2s;
    }

    .editable:hover {
        background: var(--lcm-active-bg);
    }

    .edit-input {
        background: var(--card-background-color, #fff);
        border: 1px solid var(--primary-color);
        border-radius: 4px;
        color: var(--primary-text-color);
        font-family: inherit;
        font-size: inherit;
        outline: none;
        padding: 4px 8px;
        width: 100%;
    }

    .edit-input:focus {
        box-shadow: 0 0 0 1px var(--primary-color);
    }

    .edit-help {
        color: var(--secondary-text-color);
        font-size: var(--lcm-section-header-size);
        margin-top: 4px;
    }
`;

/**
 * Combined shared styles - import this for all common styles.
 */
export const lcmSharedStyles = css`
    ${lcmCssVars}
    ${lcmBadgeStyles}
    ${lcmStatusIndicatorStyles}
    ${lcmCodeStyles}
    ${lcmSectionStyles}
    ${lcmRevealButtonStyles}
    ${lcmCollapsibleStyles}
    ${lcmEditableStyles}
`;
