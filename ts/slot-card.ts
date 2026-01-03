import { mdiEye, mdiEyeOff, mdiKey } from '@mdi/js';
import { LitElement, TemplateResult, css, html, nothing } from 'lit';

import { HomeAssistant } from './ha_type_stubs';
import { CodeDisplayMode, LockCodeManagerSlotCardConfig } from './types';

const DEFAULT_CODE_DISPLAY: CodeDisplayMode = 'masked_with_reveal';

interface SlotEntityData {
    active: boolean | null;
    enabled: boolean | null;
    name: string | null;
    pin: string | null;
}

/**
 * Streamlined slot card for Lock Code Manager.
 *
 * Phase 1: Core card with header, primary controls, and status section.
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

        .last-used {
            color: var(--secondary-text-color);
            font-size: 13px;
            margin-left: auto;
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

    private _hass?: HomeAssistant;
    private _config?: LockCodeManagerSlotCardConfig;
    private _revealed = false;

    set hass(hass: HomeAssistant) {
        this._hass = hass;
        this.requestUpdate();
    }

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
        this.requestUpdate();
    }

    protected render(): TemplateResult {
        if (!this._hass || !this._config) {
            return html`<ha-card><div class="message">Loading...</div></ha-card>`;
        }

        const slotData = this._getSlotData();
        if (!slotData) {
            return html`<ha-card>
                <div class="message error">Slot entities not found</div>
            </ha-card>`;
        }

        const { name, pin, enabled, active } = slotData;
        const mode = this._config.code_display ?? DEFAULT_CODE_DISPLAY;

        return html`
            <ha-card>
                ${this._renderHeader()}
                <div class="content">
                    ${this._renderPrimaryControls(name, pin, enabled, mode)}
                    ${this._renderStatus(enabled, active)}
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
            statusDetail = 'Code is set on all locks';
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
                <div class="status-detail">${statusDetail}</div>
            </div>
        `;
    }

    private _getSlotData(): SlotEntityData | null {
        if (!this._hass || !this._config) return null;

        const { slot } = this._config;
        const { states } = this._hass;

        // Collect matching LCM entities for this slot
        const entityPattern = /^(text|switch|binary_sensor)\.lcm_(.+)_(\d+)_(\w+)$/;
        const matchingEntities: Array<{
            domain: string;
            key: string;
            stateValue: string;
        }> = [];

        for (const entityId of Object.keys(states)) {
            const state = states[entityId];
            if (!state) {
                // Skip null/undefined states
            } else {
                const match = entityId.match(entityPattern);
                if (match) {
                    const [, domain, , slotStr, key] = match;
                    if (parseInt(slotStr, 10) === slot) {
                        matchingEntities.push({
                            domain,
                            key,
                            stateValue: state.state
                        });
                    }
                }
            }
        }

        // Process collected entities outside the loop
        return this._processMatchingEntities(matchingEntities);
    }

    private _processMatchingEntities(
        entities: Array<{ domain: string; key: string; stateValue: string }>
    ): SlotEntityData | null {
        let nameValue: string | null = null;
        let pinValue: string | null = null;
        let enabledValue: boolean | null = null;
        let activeValue: boolean | null = null;

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
            pin: pinValue
        };
    }

    private _toggleReveal(): void {
        this._revealed = !this._revealed;
        this.requestUpdate();
    }

    private _handleEnabledToggle(e: Event): void {
        const target = e.target as HTMLInputElement;
        const newState = target.checked;

        if (!this._hass || !this._config) return;

        const { slot } = this._config;
        const { states } = this._hass;

        // Find the enabled switch entity
        const enabledEntityId = Object.keys(states).find((entityId) => {
            const match = entityId.match(/^switch\.lcm_(.+)_(\d+)_enabled$/);
            return match && parseInt(match[2], 10) === slot;
        });

        if (enabledEntityId) {
            void this._hass.callService('switch', newState ? 'turn_on' : 'turn_off', {
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
