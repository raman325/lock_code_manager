import { mdiChevronDown, mdiChevronUp, mdiEye, mdiEyeOff, mdiKey } from '@mdi/js';
import { LitElement, TemplateResult, css, html, nothing } from 'lit';
import { property, state } from 'lit/decorators.js';

import { HomeAssistant } from './ha_type_stubs';
import { CodeDisplayMode, LockCodeManagerSlotCardConfig } from './types';

const DEFAULT_CODE_DISPLAY: CodeDisplayMode = 'masked_with_reveal';

interface SlotEntityData {
    active: boolean | null;
    enabled: boolean | null;
    name: string | null;
    numberofUses: number | null;
    pin: string | null;
}

interface LockSyncStatus {
    entityId: string;
    inSync: boolean | null;
    lockEntityId: string;
    name: string;
}

/**
 * Streamlined slot card for Lock Code Manager.
 *
 * Phase 2: Adds collapsible conditions and lock status sections.
 * Uses existing LCM entities via Home Assistant state.
 */
class LockCodeManagerSlotCard extends LitElement {
    static styles = css`
        :host {
            display: block;
        }

        ha-card {
            overflow: hidden;
        }

        /* Header Section */
        .header {
            align-items: center;
            background: var(--ha-card-background, var(--card-background-color, #fff));
            border-bottom: 1px solid var(--ha-card-border-color, var(--divider-color, #e0e0e0));
            display: flex;
            gap: 12px;
            padding: 16px;
        }

        .header-icon {
            align-items: center;
            background: rgba(var(--rgb-primary-color), 0.1);
            border-radius: 50%;
            color: var(--primary-color);
            display: flex;
            height: 40px;
            justify-content: center;
            width: 40px;
        }

        .header-icon ha-svg-icon {
            --mdc-icon-size: 24px;
        }

        .header-title {
            color: var(--primary-text-color);
            font-size: 1.25em;
            font-weight: 500;
        }

        /* Content Sections */
        .content {
            display: flex;
            flex-direction: column;
            gap: 16px;
            padding: 16px;
        }

        .section {
            background: rgba(var(--rgb-primary-text-color), 0.03);
            border-radius: 12px;
            padding: 16px;
        }

        .section-header {
            color: var(--secondary-text-color);
            font-size: 11px;
            font-weight: 600;
            letter-spacing: 0.05em;
            margin-bottom: 12px;
            text-transform: uppercase;
        }

        /* Collapsible Section */
        .collapsible-section {
            background: rgba(var(--rgb-primary-text-color), 0.03);
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
            background: rgba(var(--rgb-primary-text-color), 0.03);
        }

        .collapsible-title {
            align-items: center;
            color: var(--secondary-text-color);
            display: flex;
            font-size: 11px;
            font-weight: 600;
            gap: 8px;
            letter-spacing: 0.05em;
            text-transform: uppercase;
        }

        .collapsible-badge {
            background: rgba(var(--rgb-primary-color), 0.1);
            border-radius: 10px;
            color: var(--primary-color);
            font-size: 10px;
            padding: 2px 8px;
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
            font-size: 14px;
            gap: 8px;
        }

        .control-value.unnamed {
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
            font-family: 'Roboto Mono', monospace;
            font-size: 16px;
            font-weight: 600;
            letter-spacing: 2px;
        }

        .pin-value.masked {
            color: var(--secondary-text-color);
        }

        .pin-reveal {
            --mdc-icon-button-size: 32px;
            --mdc-icon-size: 18px;
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

        .status-indicator {
            border-radius: 50%;
            height: 12px;
            width: 12px;
        }

        .status-indicator.active {
            background-color: var(--success-color, #4caf50);
        }

        .status-indicator.inactive {
            background-color: var(--warning-color, #ff9800);
        }

        .status-indicator.disabled {
            background-color: var(--disabled-text-color, #9e9e9e);
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

        .no-conditions {
            color: var(--secondary-text-color);
            font-size: 13px;
            font-style: italic;
        }

        /* Lock Status Section */
        .lock-row {
            align-items: center;
            border-bottom: 1px solid rgba(var(--rgb-primary-text-color), 0.06);
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

        .lock-sync-icon {
            --mdc-icon-size: 18px;
        }

        .lock-sync-icon.synced {
            color: var(--success-color, #4caf50);
        }

        .lock-sync-icon.pending {
            color: var(--warning-color, #ff9800);
        }

        .lock-sync-icon.unknown {
            color: var(--disabled-text-color, #9e9e9e);
        }

        .lock-name {
            color: var(--primary-text-color);
            flex: 1;
            font-size: 14px;
        }

        .lock-status-text {
            color: var(--secondary-text-color);
            font-size: 12px;
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
    `;

    @property({ attribute: false }) hass?: HomeAssistant;
    @state() private _config?: LockCodeManagerSlotCardConfig;
    @state() private _revealed = false;
    @state() private _conditionsExpanded = false;
    @state() private _lockStatusExpanded = false;

    static getConfigElement(): HTMLElement {
        // TODO: Create editor component
        return document.createElement('div');
    }

    static getStubConfig(): Partial<LockCodeManagerSlotCardConfig> {
        return { config_entry_id: '', slot: 1 };
    }

    setConfig(config: LockCodeManagerSlotCardConfig): void {
        if (!config.config_entry_id) {
            throw new Error('config_entry_id is required');
        }
        if (!config.slot) {
            throw new Error('slot is required');
        }
        this._config = config;

        // Set initial collapsed state from config
        const collapsed = config.collapsed_sections ?? ['conditions', 'lock_status'];
        this._conditionsExpanded = !collapsed.includes('conditions');
        this._lockStatusExpanded = !collapsed.includes('lock_status');
    }

    protected render(): TemplateResult {
        if (!this.hass || !this._config) {
            return html`<ha-card><div class="message">Loading...</div></ha-card>`;
        }

        const slotData = this._getSlotData();
        if (!slotData) {
            return html`<ha-card>
                <div class="message error">Slot entities not found</div>
            </ha-card>`;
        }

        const { name, pin, enabled, active, numberofUses } = slotData;
        const mode = this._config.code_display ?? DEFAULT_CODE_DISPLAY;
        const lockStatuses = this._getLockSyncStatuses();

        return html`
            <ha-card>
                ${this._renderHeader()}
                <div class="content">
                    ${this._renderPrimaryControls(name, pin, enabled, mode)}
                    ${this._renderStatus(enabled, active)}
                    ${this._renderConditionsSection(numberofUses)}
                    ${this._renderLockStatusSection(lockStatuses)}
                </div>
            </ha-card>
        `;
    }

    private _renderHeader(): TemplateResult {
        return html`
            <div class="header">
                <div class="header-icon">
                    <ha-svg-icon .path=${mdiKey}></ha-svg-icon>
                </div>
                <span class="header-title">Code Slot ${this._config?.slot}</span>
            </div>
        `;
    }

    private _renderPrimaryControls(
        name: string | null,
        pin: string | null,
        enabled: boolean | null,
        mode: CodeDisplayMode
    ): TemplateResult {
        const shouldMask = mode === 'masked' || (mode === 'masked_with_reveal' && !this._revealed);
        const displayPin = pin ? (shouldMask ? '•'.repeat(pin.length) : pin) : '—';

        return html`
            <div class="section">
                <div class="section-header">Primary Controls</div>

                <div class="control-row">
                    <span class="control-label">Name</span>
                    <span class="control-value ${name ? '' : 'unnamed'}">
                        ${name ?? 'Unnamed'}
                    </span>
                </div>

                <div class="control-row">
                    <span class="control-label">PIN</span>
                    <div class="pin-field">
                        <span class="pin-value ${shouldMask && pin ? 'masked' : ''}">
                            ${displayPin}
                        </span>
                        ${mode === 'masked_with_reveal' && pin
                            ? html`<ha-icon-button
                                  class="pin-reveal"
                                  .path=${this._revealed ? mdiEyeOff : mdiEye}
                                  @click=${this._toggleReveal}
                                  .label=${this._revealed ? 'Hide PIN' : 'Reveal PIN'}
                              ></ha-icon-button>`
                            : nothing}
                    </div>
                </div>

                <div class="enabled-row">
                    <span class="enabled-label">Enabled</span>
                    <ha-switch
                        .checked=${enabled === true}
                        .disabled=${enabled === null}
                        @change=${this._handleEnabledToggle}
                    ></ha-switch>
                </div>
            </div>
        `;
    }

    private _renderStatus(enabled: boolean | null, active: boolean | null): TemplateResult {
        let statusClass: string;
        let statusText: string;
        let statusDetail: string;

        if (enabled === false) {
            statusClass = 'disabled';
            statusText = 'Disabled';
            statusDetail = 'Slot is disabled by user';
        } else if (active === true) {
            statusClass = 'active';
            statusText = 'Active';
            statusDetail = '';
        } else if (active === false) {
            statusClass = 'inactive';
            statusText = 'Inactive';
            statusDetail = 'Blocked by conditions';
        } else {
            statusClass = 'disabled';
            statusText = 'Unknown';
            statusDetail = 'Status unavailable';
        }

        return html`
            <div class="section">
                <div class="section-header">Status</div>
                <div class="status-row">
                    <span class="status-indicator ${statusClass}"></span>
                    <span class="status-text">${statusText}</span>
                </div>
                ${statusDetail ? html`<div class="status-detail">${statusDetail}</div>` : nothing}
            </div>
        `;
    }

    private _renderConditionsSection(numberofUses: number | null): TemplateResult {
        const hasConditions = numberofUses !== null;
        const conditionCount = hasConditions ? 1 : 0;

        return html`
            <div class="collapsible-section">
                <div class="collapsible-header" @click=${this._toggleConditions}>
                    <div class="collapsible-title">
                        Conditions
                        ${conditionCount > 0
                            ? html`<span class="collapsible-badge">${conditionCount}</span>`
                            : nothing}
                    </div>
                    <ha-svg-icon
                        class="collapsible-chevron"
                        .path=${this._conditionsExpanded ? mdiChevronUp : mdiChevronDown}
                    ></ha-svg-icon>
                </div>
                <div class="collapsible-content ${this._conditionsExpanded ? 'expanded' : ''}">
                    ${hasConditions
                        ? html`
                              ${numberofUses !== null
                                  ? html`<div class="condition-row">
                                        <span class="condition-label">Uses remaining</span>
                                        <span class="condition-value">${numberofUses}</span>
                                    </div>`
                                  : nothing}
                          `
                        : html`<div class="no-conditions">
                              No conditions configured for this slot
                          </div>`}
                </div>
            </div>
        `;
    }

    private _renderLockStatusSection(lockStatuses: LockSyncStatus[]): TemplateResult {
        const syncedCount = lockStatuses.filter((l) => l.inSync === true).length;
        const totalCount = lockStatuses.length;

        return html`
            <div class="collapsible-section">
                <div class="collapsible-header" @click=${this._toggleLockStatus}>
                    <div class="collapsible-title">
                        Lock Status
                        ${totalCount > 0
                            ? html`<span class="collapsible-badge"
                                  >${syncedCount}/${totalCount}</span
                              >`
                            : nothing}
                    </div>
                    <ha-svg-icon
                        class="collapsible-chevron"
                        .path=${this._lockStatusExpanded ? mdiChevronUp : mdiChevronDown}
                    ></ha-svg-icon>
                </div>
                <div class="collapsible-content ${this._lockStatusExpanded ? 'expanded' : ''}">
                    ${lockStatuses.length > 0
                        ? lockStatuses.map((lock) => this._renderLockRow(lock))
                        : html`<div class="no-conditions">No locks found</div>`}
                </div>
            </div>
        `;
    }

    private _renderLockRow(lock: LockSyncStatus): TemplateResult {
        let iconClass: string;
        let statusText: string;

        if (lock.inSync === true) {
            iconClass = 'synced';
            statusText = 'Synced';
        } else if (lock.inSync === false) {
            iconClass = 'pending';
            statusText = 'Pending';
        } else {
            iconClass = 'unknown';
            statusText = 'Unknown';
        }

        return html`
            <div class="lock-row">
                <ha-icon class="lock-sync-icon ${iconClass}" icon="mdi:check-circle"></ha-icon>
                <span class="lock-name">${lock.name}</span>
                <span class="lock-status-text">${statusText}</span>
            </div>
        `;
    }

    private _getSlotData(): SlotEntityData | null {
        if (!this.hass || !this._config) return null;

        const { slot } = this._config;
        const { states } = this.hass;

        // Collect matching LCM entities for this slot
        const slotEntityPattern = /^(text|switch|binary_sensor|number)\.lcm_(.+)_(\d+)_(\w+)$/;
        const matchingEntities: Array<{
            domain: string;
            key: string;
            stateValue: string;
        }> = [];

        for (const entityId of Object.keys(states)) {
            const entityState = states[entityId];
            if (!entityState) {
                // Skip null/undefined states
            } else {
                const match = entityId.match(slotEntityPattern);
                if (match) {
                    const [, domain, , slotStr, key] = match;
                    if (parseInt(slotStr, 10) === slot) {
                        matchingEntities.push({
                            domain,
                            key,
                            stateValue: entityState.state
                        });
                    }
                }
            }
        }

        return this._processMatchingEntities(matchingEntities);
    }

    private _processMatchingEntities(
        entities: Array<{ domain: string; key: string; stateValue: string }>
    ): SlotEntityData | null {
        let nameValue: string | null = null;
        let pinValue: string | null = null;
        let enabledValue: boolean | null = null;
        let activeValue: boolean | null = null;
        let numberofUsesValue: number | null = null;

        for (const { domain, key, stateValue } of entities) {
            const isValidTextState =
                stateValue && stateValue !== 'unknown' && stateValue !== 'unavailable';

            if (key === 'name' && domain === 'text' && isValidTextState) {
                nameValue = stateValue;
            } else if (key === 'pin' && domain === 'text' && isValidTextState) {
                pinValue = stateValue;
            } else if (key === 'enabled' && domain === 'switch') {
                enabledValue = stateValue === 'on';
            } else if (key === 'active' && domain === 'binary_sensor') {
                activeValue = stateValue === 'on';
            } else if (key === 'number_of_uses' && domain === 'number' && isValidTextState) {
                const parsed = parseFloat(stateValue);
                if (!isNaN(parsed)) {
                    numberofUsesValue = parsed;
                }
            }
        }

        // Return null only if we found no entities at all
        if (
            nameValue === null &&
            pinValue === null &&
            enabledValue === null &&
            activeValue === null
        ) {
            return null;
        }

        return {
            active: activeValue,
            enabled: enabledValue,
            name: nameValue,
            numberofUses: numberofUsesValue,
            pin: pinValue
        };
    }

    private _getLockSyncStatuses(): LockSyncStatus[] {
        if (!this.hass || !this._config) return [];

        const { slot } = this._config;
        const { states } = this.hass;
        const lockStatuses: LockSyncStatus[] = [];

        // Pattern for in_sync entities: binary_sensor.{lock_device}_code_slot_{slot}_in_sync
        // Examples: binary_sensor.test_1_code_slot_1_in_sync, binary_sensor.front_door_code_slot_2_in_sync
        const inSyncPattern = new RegExp(`^binary_sensor\\.(.+)_code_slot_${slot}_in_sync$`);

        for (const entityId of Object.keys(states)) {
            const match = entityId.match(inSyncPattern);
            if (match) {
                const [, lockDeviceName] = match;
                const entityState = states[entityId];
                if (entityState) {
                    // Try to get the friendly name from state attributes
                    const friendlyName =
                        entityState.attributes?.friendly_name ??
                        `${lockDeviceName.replace(/_/g, ' ')} Lock`;

                    lockStatuses.push({
                        entityId,
                        inSync:
                            entityState.state === 'on'
                                ? true
                                : entityState.state === 'off'
                                  ? false
                                  : null,
                        lockEntityId: lockDeviceName,
                        name: friendlyName.replace(/ code slot \d+ in sync$/i, '').trim()
                    });
                }
            }
        }

        return lockStatuses;
    }

    private _toggleReveal(): void {
        this._revealed = !this._revealed;
    }

    private _toggleConditions(): void {
        this._conditionsExpanded = !this._conditionsExpanded;
    }

    private _toggleLockStatus(): void {
        this._lockStatusExpanded = !this._lockStatusExpanded;
    }

    private _handleEnabledToggle(e: Event): void {
        const target = e.target as HTMLInputElement;
        const newState = target.checked;

        if (!this.hass || !this._config) return;

        const { slot } = this._config;
        const { states } = this.hass;

        // Find the enabled switch entity
        const enabledEntityId = Object.keys(states).find((entityId) => {
            const match = entityId.match(/^switch\.lcm_(.+)_(\d+)_enabled$/);
            return match && parseInt(match[2], 10) === slot;
        });

        if (enabledEntityId) {
            void this.hass.callService('switch', newState ? 'turn_on' : 'turn_off', {
                entity_id: enabledEntityId
            });
        }
    }
}

customElements.define('lock-code-manager-slot', LockCodeManagerSlotCard);

declare global {
    interface Window {
        customCards?: Array<{ description: string; name: string; type: string }>;
    }
}

window.customCards = window.customCards || [];
window.customCards.push({
    description: 'Displays and controls a Lock Code Manager code slot',
    name: 'Lock Code Manager Slot',
    type: 'custom:lock-code-manager-slot'
});
