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
        background: var(--ha-card-background, var(--card-background-color, #fff));
        border-bottom: 1px solid var(--lcm-border-color);
        display: flex;
        flex-direction: column;
        gap: 12px;
        padding: 16px;
    }

    .header-top {
        align-items: center;
        display: flex;
        flex-wrap: wrap;
        gap: 8px 12px;
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

    .header-info {
        display: flex;
        flex: 1;
        flex-direction: column;
        gap: 2px;
        min-width: 0;
    }

    .header-title {
        color: var(--primary-text-color);
        font-size: 18px;
        font-weight: 500;
        white-space: nowrap;
    }

    .header-pills {
        align-items: center;
        display: flex;
        flex-wrap: wrap;
        gap: 4px;
    }

    .header-pill {
        align-items: center;
        background: var(--lcm-section-bg);
        border-radius: 12px;
        color: var(--secondary-text-color);
        display: flex;
        font-size: 11px;
        gap: 4px;
        max-width: 100%;
        overflow: hidden;
        padding: 4px 8px;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    .header-pill ha-svg-icon {
        --mdc-icon-size: 14px;
        flex-shrink: 0;
    }

    .header-pill.clickable {
        cursor: pointer;
        transition: background-color 0.2s;
    }

    .header-pill.clickable:hover {
        background: var(--lcm-section-bg-hover);
    }

    /* Content Sections */
    .content {
        display: flex;
        flex-direction: column;
        gap: 16px;
        padding: 16px;
    }

    /* Condition-specific icons (extend shared collapsible styles) */
    .condition-blocking-icons {
        align-items: center;
        display: flex;
        gap: 4px;
    }

    .condition-icon {
        --mdc-icon-size: 16px;
        color: var(--lcm-disabled-color);
    }

    .condition-icon.blocking {
        color: var(--lcm-warning-color);
    }

    .condition-row-icon {
        --mdc-icon-size: 18px;
        color: var(--lcm-disabled-color);
        flex-shrink: 0;
    }

    .condition-row-icon.blocking {
        color: var(--lcm-warning-color);
    }

    /* Primary Controls Section */
    .control-row {
        align-items: center;
        display: flex;
        gap: 16px;
        margin-bottom: 12px;
    }

    .control-row:last-child {
        margin-bottom: 0;
    }

    .control-label {
        color: var(--secondary-text-color);
        font-size: 14px;
        min-width: 60px;
    }

    .control-value {
        align-items: center;
        color: var(--primary-text-color);
        display: flex;
        flex: 1;
        font-family: var(--lcm-code-font);
        font-size: var(--lcm-code-font-size);
        font-weight: var(--lcm-code-font-weight);
        gap: 8px;
        min-height: 1.5em;
    }

    .placeholder {
        color: var(--secondary-text-color);
        font-style: italic;
    }

    .pin-field {
        align-items: center;
        display: flex;
        flex: 1;
        gap: 8px;
    }

    .pin-value {
        font-family: var(--lcm-code-font);
        font-size: var(--lcm-code-font-size);
        font-weight: var(--lcm-code-font-weight);
        letter-spacing: 2px;
        min-height: 1.5em;
    }

    .pin-value.masked {
        color: var(--secondary-text-color);
    }

    .pin-reveal {
        --mdc-icon-button-size: 32px;
        --mdc-icon-size: 18px;
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
        font-size: var(--lcm-code-font-size);
        font-weight: var(--lcm-code-font-weight);
        letter-spacing: 2px;
    }

    .enabled-row {
        align-items: center;
        display: flex;
        gap: 16px;
        justify-content: space-between;
    }

    .enabled-label {
        color: var(--secondary-text-color);
        font-size: 14px;
    }

    /* Status Section */
    .status-row {
        align-items: center;
        display: flex;
        gap: 12px;
    }

    .status-text {
        color: var(--primary-text-color);
        font-size: 14px;
        font-weight: 500;
    }

    .status-detail {
        color: var(--secondary-text-color);
        font-size: 13px;
        margin-left: 24px;
        margin-top: 4px;
    }

    /* Conditions Section */
    .condition-row {
        align-items: center;
        display: flex;
        gap: 12px;
        padding: 8px 0;
    }

    .condition-row:first-child {
        padding-top: 0;
    }

    .condition-row:last-child {
        padding-bottom: 0;
    }

    .condition-label {
        color: var(--secondary-text-color);
        font-size: 13px;
        min-width: 100px;
    }

    .condition-value {
        color: var(--primary-text-color);
        font-size: 14px;
    }

    /* Unified condition item (matches condition-entity structure) */
    .condition-item {
        display: flex;
        flex-direction: column;
        gap: 4px;
        padding: 8px 0;
    }

    .condition-item-header {
        align-items: center;
        display: flex;
        gap: 8px;
    }

    .condition-item-detail {
        font-size: 13px;
        margin-left: 28px;
    }

    /* Inline edit container for number of uses */
    .condition-edit-container {
        position: relative;
    }

    .condition-edit-container .edit-input {
        background: var(--card-background-color);
        border: 1px solid var(--primary-color);
        border-radius: 4px;
        color: var(--primary-text-color);
        font-size: 14px;
        padding: 4px 8px;
        width: 60px;
    }

    .condition-edit-container .edit-help {
        color: var(--secondary-text-color);
        font-size: 10px;
        left: 0;
        position: absolute;
        top: 100%;
        white-space: nowrap;
    }

    .no-conditions {
        color: var(--secondary-text-color);
        font-size: 13px;
        font-style: italic;
    }

    /* Condition helper entity rows */
    .condition-helpers {
        display: flex;
        flex-direction: column;
        gap: 8px;
        margin-top: 8px;
    }

    .condition-helper-row {
        align-items: center;
        cursor: pointer;
        display: flex;
        gap: 12px;
        padding: 4px 0;
    }

    .condition-helper-info {
        display: flex;
        flex-direction: column;
        min-width: 0;
    }

    .condition-helper-name {
        color: var(--primary-text-color);
        font-size: 14px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    .condition-helper-state {
        color: var(--secondary-text-color);
        font-size: 12px;
        text-transform: capitalize;
    }

    /* Unified condition entity styles */
    .condition-entity {
        display: flex;
        flex-direction: column;
        gap: 4px;
        padding: 8px 0;
    }

    .condition-entity.clickable {
        border-radius: 8px;
        cursor: pointer;
        margin: 8px -8px 0;
        padding: 8px;
        transition: background-color 0.2s;
    }

    .condition-entity.clickable:hover {
        background: var(--lcm-active-bg);
    }

    .condition-entity:first-child {
        padding-top: 0;
    }

    .condition-entity.clickable:first-child {
        margin-top: 0;
    }

    .condition-entity-header {
        align-items: center;
        display: flex;
        gap: 6px;
    }

    .condition-entity-icon {
        --mdc-icon-size: 18px;
        flex-shrink: 0;
    }

    .condition-entity-icon.active {
        color: var(--lcm-success-color);
    }

    .condition-entity-icon.inactive {
        color: var(--lcm-warning-color);
    }

    .condition-entity-status {
        color: var(--primary-text-color);
        font-size: 14px;
        font-weight: 500;
    }

    .condition-entity-domain {
        background: var(--lcm-section-bg);
        border-radius: 4px;
        color: var(--secondary-text-color);
        font-size: 10px;
        font-weight: 500;
        letter-spacing: 0.03em;
        margin-left: auto;
        padding: 2px 6px;
        text-transform: uppercase;
    }

    .condition-entity-name {
        color: var(--secondary-text-color);
        font-size: 13px;
        margin-left: 24px;
    }

    .condition-context {
        color: var(--secondary-text-color);
        font-size: 12px;
        margin-left: 24px;
    }

    .condition-context-label {
        font-weight: 500;
        margin-right: 4px;
    }

    .condition-context-next {
        border-top: 1px solid var(--lcm-border-color);
        margin-top: 4px;
        opacity: 0.8;
        padding-top: 4px;
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

    .lock-status-text {
        color: var(--secondary-text-color);
        font-size: 12px;
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

    /* Condition action icons */
    .condition-action-icons {
        align-items: center;
        display: flex;
        gap: 4px;
        margin-left: auto;
    }

    .condition-edit-icon,
    .condition-delete-icon {
        --mdc-icon-size: 18px;
        color: var(--secondary-text-color);
        cursor: pointer;
        opacity: 0.6;
        transition: opacity 0.2s;
    }

    .condition-edit-icon:hover {
        opacity: 1;
    }

    .condition-delete-icon:hover {
        color: var(--error-color);
        opacity: 1;
    }

    .condition-entity-header .condition-action-icons {
        margin-left: 8px;
    }

    /* Add condition links - compact inline style */
    .add-condition-links {
        align-items: center;
        color: var(--secondary-text-color);
        display: flex;
        flex-wrap: wrap;
        font-size: 13px;
        gap: 4px;
        padding: 4px 0;
    }

    .add-condition-link {
        color: var(--primary-color);
        cursor: pointer;
        text-decoration: none;
    }

    .add-condition-link:hover {
        text-decoration: underline;
    }

    .add-condition-separator {
        color: var(--divider-color);
        margin: 0 4px;
    }

    /* Empty conditions header row */
    .empty-conditions-header {
        align-items: center;
        background: var(--lcm-section-bg);
        border-radius: 8px;
        display: flex;
        gap: 8px;
        padding: 12px;
    }

    .empty-conditions-title {
        color: var(--secondary-text-color);
        font-size: 11px;
        font-weight: 500;
        letter-spacing: 0.05em;
        text-transform: uppercase;
    }

    .empty-conditions-badge {
        background: var(--lcm-section-bg-hover);
        border-radius: 10px;
        color: var(--secondary-text-color);
        font-size: 11px;
        padding: 2px 8px;
    }

    .empty-conditions-spacer {
        flex: 1;
    }

    .empty-conditions-actions {
        align-items: center;
        display: flex;
        gap: 4px;
    }

    .empty-conditions-btn {
        --mdc-icon-size: 18px;
        align-items: center;
        background: transparent;
        border: 1px solid var(--divider-color);
        border-radius: 14px;
        color: var(--primary-color);
        cursor: pointer;
        display: flex;
        font-size: 11px;
        gap: 2px;
        padding: 4px 8px;
        transition:
            background-color 0.2s,
            border-color 0.2s;
    }

    .empty-conditions-btn:hover {
        background: var(--lcm-active-bg);
        border-color: var(--primary-color);
    }

    .empty-conditions-btn ha-svg-icon {
        --mdc-icon-size: 14px;
    }

    /* Dialog styles */
    .dialog-content {
        display: flex;
        flex-direction: column;
        gap: 16px;
        min-width: 300px;
    }

    .entity-select {
        background: var(--card-background-color);
        border: 1px solid var(--divider-color);
        border-radius: 4px;
        color: var(--primary-text-color);
        font-size: 14px;
        padding: 8px;
        width: 100%;
    }

    .dialog-section {
        display: flex;
        flex-direction: column;
        gap: 8px;
    }

    .dialog-section-header {
        color: var(--primary-text-color);
        font-size: 14px;
        font-weight: 500;
    }

    .dialog-section-description {
        color: var(--secondary-text-color);
        font-size: 12px;
    }

    .dialog-checkbox-row {
        align-items: center;
        display: flex;
        gap: 8px;
    }

    .dialog-checkbox-row label {
        color: var(--primary-text-color);
        cursor: pointer;
        font-size: 14px;
    }

    .dialog-number-input {
        margin-top: 8px;
    }

    .dialog-number-input input {
        background: var(--input-background-color, var(--card-background-color));
        border: 1px solid var(--divider-color);
        border-radius: 4px;
        color: var(--primary-text-color);
        font-size: 14px;
        padding: 8px 12px;
        width: 100px;
    }

    .dialog-clear-button {
        background: none;
        border: 1px solid var(--divider-color);
        border-radius: 4px;
        color: var(--error-color);
        cursor: pointer;
        font-size: 13px;
        margin-top: 8px;
        padding: 6px 12px;
    }

    .dialog-clear-button:hover {
        background: var(--error-color);
        color: white;
    }

    /* Confirmation dialog styles */
    .confirm-dialog-content {
        color: var(--primary-text-color);
        font-size: 14px;
        line-height: 1.5;
        padding: 8px 0;
    }

    ha-button.destructive {
        --mdc-theme-primary: var(--error-color);
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
