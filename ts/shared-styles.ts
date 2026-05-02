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
        --lcm-error-color: var(--error-color, #f44336);
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
    /* Base badge — used by identity tags (Managed/Unmanaged/Empty).
       Compact uppercase for category labels. */
    .lcm-badge {
        border-radius: var(--lcm-badge-radius);
        font-size: var(--lcm-badge-font-size);
        font-weight: var(--lcm-badge-font-weight);
        letter-spacing: var(--lcm-badge-letter-spacing);
        padding: var(--lcm-badge-padding);
        text-transform: uppercase;
    }

    /* State badges (active/inactive/disabled) align visually with the slot
       card's .state-chip: pill shape, sentence-case, optional colored dot
       prefix, 16% color tint. Same colors as the slot card so a state reads
       the same regardless of which card you're on. */
    .lcm-badge.active,
    .lcm-badge.inactive,
    .lcm-badge.disabled {
        align-items: center;
        border-radius: 12px;
        display: inline-flex;
        font-size: 10px;
        font-weight: 600;
        gap: 5px;
        letter-spacing: normal;
        padding: 3px 8px;
        text-transform: none;
    }
    .lcm-badge .dot {
        border-radius: 50%;
        flex-shrink: 0;
        height: 5px;
        width: 5px;
    }

    .lcm-badge.active {
        background: rgba(var(--rgb-success-color, 67, 160, 71), 0.16);
        color: var(--success-color, #43a047);
    }
    .lcm-badge.active .dot {
        background: var(--success-color, #43a047);
    }

    .lcm-badge.inactive {
        background: rgba(var(--rgb-warning-color, 255, 167, 38), 0.16);
        color: var(--warning-color, #ffa726);
    }
    .lcm-badge.inactive .dot {
        background: var(--warning-color, #ffa726);
    }

    .lcm-badge.disabled {
        background: rgba(var(--rgb-disabled-color, 117, 117, 117), 0.2);
        color: var(--secondary-text-color);
    }
    .lcm-badge.disabled .dot {
        background: var(--disabled-color, #757575);
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
 * Classes: .lcm-sync-icon.synced, .lcm-sync-icon.pending, .lcm-sync-icon.syncing,
 *          .lcm-sync-icon.suspended, .lcm-sync-icon.unknown
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

    .lcm-sync-icon.syncing {
        color: var(--lcm-warning-color);
    }

    .lcm-sync-icon.suspended {
        color: var(--lcm-error-color);
    }

    .lcm-sync-icon.unknown {
        color: var(--lcm-disabled-color);
    }
`;

/**
 * Shared PIN/code display styles.
 * Classes: .lcm-code, .lcm-code.masked, .lcm-code.off, .lcm-code.pending, .lcm-code.no-code
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

    /* Slot disabled by user — PIN exists in config but is intentionally not on
       the lock. Heavily dimmed dots in a muted pill, no strikethrough. */
    .lcm-code.off {
        background: var(--lcm-section-bg, rgba(127, 127, 127, 0.05));
        border-radius: 6px;
        color: var(--disabled-text-color);
        padding: 2px 8px;
    }

    /* Slot enabled but lock doesn't have the code yet (out-of-sync, syncing, etc.).
       Dim dots with a clock-icon prefix. No strikethrough. */
    .lcm-code.pending {
        align-items: center;
        color: var(--secondary-text-color);
        display: inline-flex;
        gap: 4px;
    }

    .lcm-code.pending .lcm-code-pending-icon {
        --mdc-icon-size: 12px;
        color: var(--secondary-text-color);
        flex-shrink: 0;
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
        /* 32px hit target — bumped from 28px to be a comfortable middle
           between the WCAG 2.5.5 AA minimum (24px) and the AAA
           recommendation (44px). */
        --mdc-icon-button-size: 32px;
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
        align-items: center;
        background: var(--lcm-active-bg);
        border-radius: 10px;
        color: var(--primary-color);
        display: inline-flex;
        font-size: var(--lcm-badge-font-size);
        gap: 4px;
        padding: 2px 8px;
    }

    /* Icon prefix on a collapsible badge — sized down to 12px so it pairs
       with the 10px badge text without dominating it. Color inherits from
       the badge color so success/warning modifiers carry through. */
    .collapsible-badge-icon {
        --mdc-icon-size: 12px;
        color: inherit;
        flex-shrink: 0;
    }

    .collapsible-badge.primary {
        background: var(--primary-color);
        color: var(--text-primary-color, #fff);
    }

    .collapsible-badge.warning {
        background: var(--warning-color, #ffa600);
        color: var(--text-primary-color, #fff);
    }

    /* Success modifier — used for the "allowing" condition summary so that
       allowing reads as green and blocking reads as warning everywhere
       across the cards. 16% follows the canonical chip/badge opacity stop. */
    .collapsible-badge.success {
        background: rgba(var(--rgb-success-color, 67, 160, 71), 0.16);
        color: var(--success-color, #43a047);
    }

    .collapsible-badge.muted {
        background: var(--lcm-section-bg);
        color: var(--secondary-text-color);
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
        /* 1000px ceiling — was 500px, which clipped when many helpers +
           a calendar entity row stacked. A grid-rows transition to auto
           is the proper fix but more invasive; bumping the ceiling is
           the lower-risk shim. */
        max-height: 1000px;
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
        text-decoration: underline dashed;
        text-decoration-color: var(--secondary-text-color);
        text-underline-offset: 3px;
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
 * Visually-hidden utility — content is removed from the visual flow but stays
 * in the accessibility tree for screen readers. Used for things like the
 * summary table caption and pending-state labels where the icon carries the
 * meaning visually but a sighted-only label would be inaccessible.
 */
export const lcmVisuallyHiddenStyles = css`
    .visually-hidden {
        border: 0;
        clip: rect(0 0 0 0);
        height: 1px;
        margin: -1px;
        overflow: hidden;
        padding: 0;
        position: absolute;
        width: 1px;
    }
`;

/**
 * `prefers-reduced-motion: reduce` opt-out for transitions used by the cards.
 * Users who request reduced motion get an instant state change instead of a
 * fade/slide. Defined once here so both cards inherit by composing
 * `lcmReducedMotionStyles` (or `lcmSharedStyles`).
 */
export const lcmReducedMotionStyles = css`
    @media (prefers-reduced-motion: reduce) {
        .collapsible-content,
        .collapsible-chevron,
        .slot-chip.clickable,
        .editable,
        .hero-name-value.editable,
        .hero-pin-value.editable,
        .lcm-code.editable,
        .event-row {
            transition: none !important;
        }
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
    ${lcmVisuallyHiddenStyles}
    ${lcmReducedMotionStyles}
`;
