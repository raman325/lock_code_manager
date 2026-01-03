import { mdiChevronDown, mdiChevronUp, mdiEye, mdiEyeOff, mdiKey } from '@mdi/js';
import { UnsubscribeFunc } from 'home-assistant-js-websocket';
import { LitElement, TemplateResult, css, html, nothing } from 'lit';
import { property, state } from 'lit/decorators.js';

import { HomeAssistant } from './ha_type_stubs';
import { CodeDisplayMode, LockCodeManagerSlotCardConfig, SlotCardData } from './types';

const DEFAULT_CODE_DISPLAY: CodeDisplayMode = 'masked_with_reveal';

/** Internal interface for lock sync status display */
interface LockSyncStatus {
    entityId: string;
    inSync: boolean | null;
    lockEntityId: string;
    name: string;
}

/**
 * Streamlined slot card for Lock Code Manager.
 *
 * Phase 3: Uses websocket subscription for real-time updates.
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

    @state() private _config?: LockCodeManagerSlotCardConfig;
    @state() private _revealed = false;
    @state() private _conditionsExpanded = false;
    @state() private _lockStatusExpanded = false;
    @state() private _data?: SlotCardData;
    @state() private _error?: string;

    private _hass?: HomeAssistant;
    private _unsub?: UnsubscribeFunc;
    private _subscribing = false;

    get hass(): HomeAssistant | undefined {
        return this._hass;
    }

    @property({ attribute: false })
    set hass(hass: HomeAssistant) {
        this._hass = hass;
        void this._subscribe();
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
        // If config changed, unsubscribe and resubscribe
        if (
            this._config?.config_entry_id !== config.config_entry_id ||
            this._config?.slot !== config.slot
        ) {
            this._unsubscribe();
            this._data = undefined;
        }
        this._config = config;

        // Set initial collapsed state from config
        const collapsed = config.collapsed_sections ?? ['conditions', 'lock_status'];
        this._conditionsExpanded = !collapsed.includes('conditions');
        this._lockStatusExpanded = !collapsed.includes('lock_status');
        void this._subscribe();
    }

    connectedCallback(): void {
        super.connectedCallback();
        void this._subscribe();
    }

    disconnectedCallback(): void {
        super.disconnectedCallback();
        this._unsubscribe();
    }

    protected render(): TemplateResult {
        if (!this._hass || !this._config) {
            return html`<ha-card><div class="message">Loading...</div></ha-card>`;
        }

        if (this._error) {
            return html`<ha-card>
                <div class="message error">${this._error}</div>
            </ha-card>`;
        }

        // Use websocket data if available
        if (this._data) {
            return this._renderFromData(this._data);
        }

        // Fallback to state-based approach (initial load before subscription)
        return html`<ha-card><div class="message">Connecting...</div></ha-card>`;
    }

    private _renderFromData(data: SlotCardData): TemplateResult {
        const mode = this._config?.code_display ?? DEFAULT_CODE_DISPLAY;
        const { name, pin, enabled, active, conditions, locks } = data;
        const pinLength = data.pin_length;
        const numberofUses = conditions.number_of_uses ?? null;

        // Transform locks to the expected format
        const lockStatuses = locks.map((lock) => {
            return {
                entityId: lock.entity_id,
                inSync: lock.in_sync,
                lockEntityId: lock.entity_id,
                name: lock.name
            };
        });

        return html`
            <ha-card>
                ${this._renderHeader()}
                <div class="content">
                    ${this._renderPrimaryControls(name, pin, pinLength, enabled, mode)}
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
        pinLength: number | undefined,
        enabled: boolean | null,
        mode: CodeDisplayMode
    ): TemplateResult {
        const shouldMask = mode === 'masked' || (mode === 'masked_with_reveal' && !this._revealed);
        // Use pin if revealed, otherwise show masked dots based on pin or pinLength
        const hasPin = pin !== null || pinLength !== undefined;
        const displayPin = pin
            ? shouldMask
                ? '•'.repeat(pin.length)
                : pin
            : pinLength !== undefined
              ? '•'.repeat(pinLength)
              : '—';

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
                        <span class="pin-value ${shouldMask && hasPin ? 'masked' : ''}">
                            ${displayPin}
                        </span>
                        ${mode === 'masked_with_reveal' && hasPin
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

    private _toggleReveal(): void {
        this._revealed = !this._revealed;
        // Resubscribe to get masked/unmasked PIN based on new reveal state
        this._unsubscribe();
        void this._subscribe();
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

    private _unsubscribe(): void {
        if (this._unsub) {
            this._unsub();
            this._unsub = undefined;
        }
    }

    private _shouldReveal(): boolean {
        const mode = this._config?.code_display ?? DEFAULT_CODE_DISPLAY;
        return mode === 'unmasked' || (mode === 'masked_with_reveal' && this._revealed);
    }

    private async _subscribe(): Promise<void> {
        if (!this._hass || !this._config || this._unsub || this._subscribing) {
            return;
        }
        if (!this._hass.connection?.subscribeMessage) {
            this._error = 'Websocket connection unavailable';
            return;
        }

        this._subscribing = true;
        try {
            this._unsub = await this._hass.connection.subscribeMessage<SlotCardData>(
                (event) => {
                    this._data = event;
                    this._error = undefined;
                    this.requestUpdate();
                },
                {
                    config_entry_id: this._config.config_entry_id,
                    reveal: this._shouldReveal(),
                    slot: this._config.slot,
                    type: 'lock_code_manager/subscribe_slot_data'
                }
            );
        } catch (err) {
            this._data = undefined;
            this._error = err instanceof Error ? err.message : 'Failed to subscribe';
            this.requestUpdate();
        } finally {
            this._subscribing = false;
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
