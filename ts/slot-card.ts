import {
    mdiCalendar,
    mdiCalendarClock,
    mdiCalendarRemove,
    mdiChevronDown,
    mdiChevronUp,
    mdiClock,
    mdiCog,
    mdiEye,
    mdiEyeOff,
    mdiKey,
    mdiLock,
    mdiPencil,
    mdiPlus,
    mdiPound,
    mdiToggleSwitch,
    mdiToggleSwitchOutline
} from '@mdi/js';
import { MessageBase } from 'home-assistant-js-websocket';
import { LitElement, TemplateResult, css, html, nothing } from 'lit';
import { property, state } from 'lit/decorators.js';

import { HomeAssistant } from './ha_type_stubs';
import {
    lcmCollapsibleStyles,
    lcmCssVars,
    lcmEditableStyles,
    lcmRevealButtonStyles,
    lcmSectionStyles,
    lcmStatusIndicatorStyles
} from './shared-styles';
import { LcmSubscriptionMixin } from './subscription-mixin';
import {
    CodeDisplayMode,
    ConditionEntityInfo,
    LockCodeManagerSlotCardConfig,
    SlotCardConditions,
    SlotCardData
} from './types';

const DEFAULT_CODE_DISPLAY: CodeDisplayMode = 'masked_with_reveal';

/** Internal interface for lock sync status display */
interface LockSyncStatus {
    /** Current code on the lock (actual or masked) */
    code: string | null;
    /** Code length when masked */
    codeLength?: number;
    entityId: string;
    inSync: boolean | null;
    /** Last synced timestamp (ISO) */
    lastSynced?: string;
    lockEntityId: string;
    name: string;
}

// Base class with subscription mixin
const LcmSlotCardBase = LcmSubscriptionMixin(LitElement);

/**
 * Streamlined slot card for Lock Code Manager.
 *
 * Phase 3: Uses websocket subscription for real-time updates.
 */
class LockCodeManagerSlotCard extends LcmSlotCardBase {
    static styles = [
        lcmCssVars,
        lcmSectionStyles,
        lcmStatusIndicatorStyles,
        lcmRevealButtonStyles,
        lcmCollapsibleStyles,
        lcmEditableStyles,
        css`
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
                gap: 12px;
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
            }

            .header-badges {
                align-items: center;
                display: flex;
                flex-shrink: 0;
                gap: 8px;
            }

            .header-badge {
                align-items: center;
                background: var(--lcm-section-bg);
                border-radius: 12px;
                color: var(--secondary-text-color);
                display: flex;
                font-size: 11px;
                gap: 4px;
                padding: 4px 8px;
            }

            .header-badge ha-svg-icon {
                --mdc-icon-size: 14px;
                flex-shrink: 0;
            }

            .header-badge.clickable {
                cursor: pointer;
                transition: background-color 0.2s;
            }

            .header-badge.clickable:hover {
                background: var(--lcm-section-bg-hover);
            }

            .header-last-used {
                align-items: center;
                background: var(--lcm-section-bg);
                border-radius: 12px;
                color: var(--secondary-text-color);
                display: flex;
                font-size: 11px;
                gap: 4px;
                padding: 4px 8px;
            }

            .header-last-used ha-svg-icon {
                --mdc-icon-size: 14px;
            }

            .header-last-used.clickable {
                cursor: pointer;
                transition: background-color 0.2s;
            }

            .header-last-used.clickable:hover {
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

            .no-conditions {
                color: var(--secondary-text-color);
                font-size: 13px;
                font-style: italic;
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

            /* Condition edit icons */
            .condition-edit-icon {
                --mdc-icon-size: 18px;
                color: var(--secondary-text-color);
                cursor: pointer;
                margin-left: auto;
                opacity: 0.6;
                transition: opacity 0.2s;
            }

            .condition-edit-icon:hover {
                opacity: 1;
            }

            .condition-entity-header .condition-edit-icon {
                margin-left: 8px;
            }

            /* Manage conditions row */
            .manage-conditions-row {
                align-items: center;
                background: var(--lcm-section-bg);
                border-radius: 8px;
                color: var(--primary-color);
                cursor: pointer;
                display: flex;
                gap: 8px;
                padding: 12px;
                transition: background-color 0.2s;
            }

            .manage-conditions-row:hover {
                background: var(--lcm-section-bg-hover);
            }

            .manage-conditions-row ha-svg-icon {
                --mdc-icon-size: 20px;
            }

            .manage-conditions-row span {
                font-size: 14px;
                font-weight: 500;
            }

            /* Add condition row */
            .add-condition-row {
                align-items: center;
                border-radius: 8px;
                color: var(--primary-color);
                cursor: pointer;
                display: flex;
                gap: 8px;
                margin-top: 8px;
                padding: 8px;
                transition: background-color 0.2s;
            }

            .add-condition-row:hover {
                background: var(--lcm-active-bg);
            }

            .add-condition-row ha-svg-icon {
                --mdc-icon-size: 18px;
            }

            .add-condition-row span {
                font-size: 13px;
            }

            /* Dialog styles */
            .dialog-content {
                display: flex;
                flex-direction: column;
                gap: 16px;
                min-width: 300px;
            }

            .dialog-content ha-entity-picker {
                display: block;
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
        `
    ];

    // Note: _revealed, _unsub, _subscribing provided by LcmSubscriptionMixin
    @state() _config?: LockCodeManagerSlotCardConfig;
    @state() _data?: SlotCardData;
    @state() _error?: string;
    @state() private _actionError?: string;
    @state() private _conditionsExpanded = false;
    @state() private _editingField: 'name' | 'pin' | 'numberOfUses' | null = null;
    @state() private _lockStatusExpanded = false;

    // Condition dialog state
    @state() private _showConditionDialog = false;
    @state() private _dialogMode: 'entity' | 'uses' | 'both' = 'both';
    @state() private _dialogEntityId: string | null = null;
    @state() private _dialogNumberOfUses: number | null = null;
    @state() private _dialogEnableUses = false;
    @state() private _dialogSaving = false;

    _hass?: HomeAssistant;

    get hass(): HomeAssistant | undefined {
        return this._hass;
    }

    @property({ attribute: false })
    set hass(hass: HomeAssistant) {
        this._hass = hass;
        void this._subscribe();
    }

    static getConfigElement(): HTMLElement {
        return document.createElement('lcm-slot-editor');
    }

    static getStubConfig(): Partial<LockCodeManagerSlotCardConfig> {
        return { config_entry_id: '', slot: 1 };
    }

    setConfig(config: LockCodeManagerSlotCardConfig): void {
        if (!config.config_entry_id && !config.config_entry_title) {
            throw new Error('config_entry_id or config_entry_title is required');
        }
        if (typeof config.slot !== 'number' || config.slot < 1 || config.slot > 9999) {
            throw new Error('slot must be a number between 1 and 9999');
        }
        // If config changed, unsubscribe and resubscribe
        if (
            this._config?.config_entry_id !== config.config_entry_id ||
            this._config?.config_entry_title !== config.config_entry_title ||
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

    // Mixin abstract method implementations
    protected _getDefaultCodeDisplay(): CodeDisplayMode {
        return DEFAULT_CODE_DISPLAY;
    }

    protected _buildSubscribeMessage(): MessageBase {
        if (!this._config) {
            throw new Error('Config not set');
        }
        const msg: MessageBase & {
            config_entry_id?: string;
            config_entry_title?: string;
            reveal: boolean;
            slot: number;
        } = {
            reveal: this._shouldReveal(),
            slot: this._config.slot,
            type: 'lock_code_manager/subscribe_code_slot'
        };
        if (this._config.config_entry_id) {
            msg.config_entry_id = this._config.config_entry_id;
        } else if (this._config.config_entry_title) {
            msg.config_entry_title = this._config.config_entry_title;
        }
        return msg;
    }

    protected _handleSubscriptionData(data: unknown): void {
        this._data = data as SlotCardData;
    }

    // connectedCallback and disconnectedCallback provided by mixin

    protected updated(changedProperties: Map<string, unknown>): void {
        super.updated(changedProperties);

        // Focus the appropriate input when entering edit mode
        if (this._editingField) {
            const selectors: Record<string, string> = {
                name: '.control-row .edit-input.name-edit-input',
                numberOfUses: '.condition-row .edit-input[type="number"]',
                pin: '.pin-edit-input'
            };
            const input = this.shadowRoot?.querySelector<HTMLInputElement>(
                selectors[this._editingField]
            );
            if (input && this.shadowRoot?.activeElement !== input) {
                input.focus();
                input.select();
            }
        }
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

        // Transform locks to the expected format
        const lockStatuses = locks.map((lock) => {
            return {
                code: lock.code,
                codeLength: lock.code_length,
                entityId: lock.entity_id,
                inSync: lock.in_sync,
                lastSynced: lock.last_synced,
                lockEntityId: lock.entity_id,
                name: lock.name
            };
        });

        const showLockStatus = this._config.show_lock_status !== false;

        // Only show conditions section if at least one condition is configured
        const hasConditions =
            (conditions.number_of_uses !== undefined && conditions.number_of_uses !== null) ||
            conditions.condition_entity !== undefined ||
            conditions.calendar !== undefined;
        const showConditions = this._config.show_conditions !== false && hasConditions;
        // Show "Manage Conditions" row when no conditions exist
        const showManageConditions = this._config.show_conditions !== false && !hasConditions;

        return html`
            <ha-card>
                ${this._actionError
                    ? html`<div class="action-error">
                          <span>${this._actionError}</span>
                          <button
                              class="action-error-dismiss"
                              @click=${this._dismissActionError}
                              aria-label="Dismiss error"
                          >
                              ✕
                          </button>
                      </div>`
                    : nothing}
                ${this._renderHeader()}
                <div class="content">
                    ${this._renderPrimaryControls(name, pin, pinLength, enabled, mode)}
                    ${this._renderStatus(enabled, active)}
                    ${showManageConditions ? this._renderManageConditionsRow() : nothing}
                    ${showConditions ? this._renderConditionsSection(conditions) : nothing}
                    ${showLockStatus ? this._renderLockStatusSection(lockStatuses) : nothing}
                </div>
                ${this._showConditionDialog ? this._renderConditionDialog() : nothing}
            </ha-card>
        `;
    }

    private _renderHeader(): TemplateResult {
        const lockCount = this._data?.locks?.length ?? 0;
        const lastUsed = this._data?.last_used;
        const eventEntityId = this._data?.event_entity_id;
        const eventEntityState = eventEntityId ? this._hass?.states[eventEntityId] : undefined;
        const showLastUsed = eventEntityState && eventEntityState.state !== 'unavailable';

        return html`
            <div class="header">
                <div class="header-top">
                    <div class="header-icon">
                        <ha-svg-icon .path=${mdiKey}></ha-svg-icon>
                    </div>
                    <div class="header-info">
                        <span class="header-title">Code Slot ${this._config?.slot}</span>
                    </div>
                    ${lockCount > 0
                        ? html`<div class="header-badges">
                              <span
                                  class="header-badge clickable"
                                  title=${this._data?.locks?.map((l) => l.name).join(', ') ?? ''}
                                  @click=${this._toggleLockStatus}
                              >
                                  <ha-svg-icon .path=${mdiLock}></ha-svg-icon>
                                  ${lockCount}
                              </span>
                          </div>`
                        : nothing}
                </div>
                ${showLastUsed
                    ? html`<div
                          class="header-last-used ${lastUsed ? 'clickable' : ''}"
                          title=${lastUsed
                              ? this._data?.last_used_lock
                                  ? `Used on ${this._data.last_used_lock} - Click for details`
                                  : 'Click for PIN usage details'
                              : 'This PIN has never been used'}
                          @click=${() => lastUsed && this._navigateToEventHistory()}
                      >
                          <ha-svg-icon .path=${mdiClock}></ha-svg-icon>
                          ${lastUsed
                              ? html`${this._data?.last_used_lock ?? 'Used'}
                                    <ha-relative-time
                                        .hass=${this._hass}
                                        .datetime=${lastUsed}
                                    ></ha-relative-time>`
                              : 'Never used'}
                      </div>`
                    : nothing}
            </div>
        `;
    }

    private _navigateToEventHistory(): void {
        const eventEntityId = this._data?.event_entity_id;
        if (!eventEntityId) return;
        // Open the more-info dialog for this event entity
        const event = new CustomEvent('hass-more-info', {
            bubbles: true,
            composed: true,
            detail: { entityId: eventEntityId }
        });
        this.dispatchEvent(event);
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
              : null;

        return html`
            <div class="lcm-section">
                <div class="lcm-section-header">Primary Controls</div>

                <div class="control-row">
                    <span class="control-label">Name</span>
                    <div style="flex: 1;">
                        ${this._editingField === 'name'
                            ? html`<input
                                      class="edit-input name-edit-input"
                                      type="text"
                                      .value=${name ?? ''}
                                      @blur=${this._handleEditBlur}
                                      @keydown=${this._handleEditKeydown}
                                  />
                                  <div class="edit-help">Enter to save, Esc to cancel</div>`
                            : html`<span
                                  class="control-value editable"
                                  @click=${() => this._startEditing('name')}
                              >
                                  ${name || html`<em class="placeholder">&lt;No Name&gt;</em>`}
                              </span>`}
                    </div>
                </div>

                <div class="control-row">
                    <span class="control-label">PIN</span>
                    <div style="flex: 1;">
                        <div class="pin-field">
                            ${this._editingField === 'pin'
                                ? html`<input
                                      class="edit-input pin-edit-input"
                                      type="text"
                                      inputmode="numeric"
                                      pattern="[0-9]*"
                                      .value=${pin ?? ''}
                                      @blur=${this._handleEditBlur}
                                      @keydown=${this._handleEditKeydown}
                                  />`
                                : html`<span
                                      class="pin-value editable ${shouldMask && hasPin
                                          ? 'masked'
                                          : ''}"
                                      @click=${() => this._startEditing('pin')}
                                  >
                                      ${displayPin ??
                                      html`<em class="placeholder">&lt;No PIN&gt;</em>`}
                                  </span>`}
                            ${mode === 'masked_with_reveal' &&
                            hasPin &&
                            this._editingField !== 'pin'
                                ? html`<ha-icon-button
                                      class="pin-reveal"
                                      .path=${this._revealed ? mdiEyeOff : mdiEye}
                                      @click=${this._toggleReveal}
                                      .label=${this._revealed ? 'Hide PIN' : 'Reveal PIN'}
                                  ></ha-icon-button>`
                                : nothing}
                        </div>
                        ${this._editingField === 'pin'
                            ? html`<div class="edit-help">Enter to save, Esc to cancel</div>`
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
            <div class="lcm-section">
                <div class="lcm-section-header">Status</div>
                <div class="status-row">
                    <span class="lcm-status-dot ${statusClass}"></span>
                    <span class="status-text">${statusText}</span>
                </div>
                ${statusDetail ? html`<div class="status-detail">${statusDetail}</div>` : nothing}
            </div>
        `;
    }

    private _renderConditionsSection(conditions: SlotCardConditions): TemplateResult {
        const { number_of_uses, condition_entity } = conditions;
        const hasNumberOfUses = number_of_uses !== undefined && number_of_uses !== null;
        const hasConditionEntity = condition_entity !== undefined;

        const usesBlocking = hasNumberOfUses && number_of_uses === 0;
        const entityBlocking = hasConditionEntity && condition_entity.state !== 'on';

        const hasConditions = hasNumberOfUses || hasConditionEntity;
        const totalConditions = (hasNumberOfUses ? 1 : 0) + (hasConditionEntity ? 1 : 0);
        const blockingConditions = (usesBlocking ? 1 : 0) + (entityBlocking ? 1 : 0);

        // Show ✓ when all conditions pass (muted), ✗ when blocking (prominent)
        const passingConditions = totalConditions - blockingConditions;
        const allPassing = blockingConditions === 0;
        const headerExtra = hasConditions
            ? html`<span class="collapsible-badge ${allPassing ? 'muted' : 'warning'}"
                      >${allPassing ? '✓' : '✗'} ${passingConditions}/${totalConditions}</span
                  >
                  <span class="condition-blocking-icons">
                      ${hasNumberOfUses
                          ? html`<ha-svg-icon
                                class="condition-icon ${usesBlocking ? 'blocking' : ''}"
                                .path=${mdiPound}
                                title="${usesBlocking
                                    ? 'No uses remaining'
                                    : `${number_of_uses} uses remaining`}"
                            ></ha-svg-icon>`
                          : nothing}
                      ${hasConditionEntity
                          ? html`<ha-svg-icon
                                class="condition-icon ${entityBlocking ? 'blocking' : ''}"
                                .path=${this._getConditionEntityIcon(
                                    condition_entity.domain,
                                    !entityBlocking
                                )}
                                title="${entityBlocking
                                    ? 'Condition blocking access'
                                    : 'Condition allowing access'}"
                            ></ha-svg-icon>`
                          : nothing}
                  </span>`
            : undefined;

        // Determine what conditions can still be added
        const canAddUses = !hasNumberOfUses;
        const canAddEntity = !hasConditionEntity;
        const canAddMore = canAddUses || canAddEntity;

        const content = html`
            ${hasNumberOfUses
                ? html`<div class="condition-row">
                      <ha-svg-icon
                          class="condition-row-icon ${usesBlocking ? 'blocking' : ''}"
                          .path=${mdiPound}
                      ></ha-svg-icon>
                      <span class="condition-label">Uses remaining</span>
                      <div style="flex: 1;">
                          ${this._editingField === 'numberOfUses'
                              ? html`<input
                                        class="edit-input"
                                        type="number"
                                        inputmode="numeric"
                                        min="0"
                                        .value=${String(number_of_uses ?? 0)}
                                        @blur=${this._handleEditBlur}
                                        @keydown=${this._handleEditKeydown}
                                    />
                                    <div class="edit-help">Enter to save, Esc to cancel</div>`
                              : html`<span
                                    class="condition-value editable"
                                    @click=${() => this._startEditing('numberOfUses')}
                                    >${number_of_uses}</span
                                >`}
                      </div>
                      <ha-svg-icon
                          class="condition-edit-icon"
                          .path=${mdiPencil}
                          title="Edit number of uses"
                          @click=${(e: Event) => {
                              e.stopPropagation();
                              this._openConditionDialog('uses');
                          }}
                      ></ha-svg-icon>
                  </div>`
                : nothing}
            ${hasConditionEntity ? this._renderConditionEntity(condition_entity, true) : nothing}
            ${canAddMore
                ? html`<div
                      class="add-condition-row"
                      @click=${() =>
                          this._openConditionDialog(
                              canAddUses && canAddEntity ? 'both' : canAddUses ? 'uses' : 'entity'
                          )}
                  >
                      <ha-svg-icon .path=${mdiPlus}></ha-svg-icon>
                      <span>Add condition</span>
                  </div>`
                : nothing}
        `;

        return this._renderCollapsible(
            'Conditions',
            this._conditionsExpanded,
            this._toggleConditions,
            content,
            headerExtra
        );
    }

    /**
     * Get the appropriate icon for a condition entity based on its domain.
     * Uses icons consistent with Home Assistant core.
     */
    private _getConditionEntityIcon(domain: string, isActive: boolean): string {
        switch (domain) {
            case 'calendar':
                return isActive ? mdiCalendar : mdiCalendarRemove;
            case 'binary_sensor':
                // HA core uses mdi:eye for generic binary sensors
                return mdiEye;
            case 'switch':
                // HA core uses mdi:toggle-switch / mdi:toggle-switch-outline
                return isActive ? mdiToggleSwitch : mdiToggleSwitchOutline;
            case 'schedule':
                // HA core uses mdi:calendar-clock for schedule entities
                return mdiCalendarClock;
            case 'input_boolean':
                // HA core uses mdi:toggle-switch-outline for input_boolean
                return isActive ? mdiToggleSwitch : mdiToggleSwitchOutline;
            default:
                return mdiEye;
        }
    }

    /**
     * Format a schedule date for display (today, tomorrow, or weekday).
     * Returns empty string for today, "tomorrow " or "Mon " etc for other days.
     */
    private _formatScheduleDate(date: Date): string {
        const now = new Date();
        const isToday = date.toDateString() === now.toDateString();
        if (isToday) return '';

        const tomorrow = new Date(now);
        tomorrow.setDate(tomorrow.getDate() + 1);
        const isTomorrow = date.toDateString() === tomorrow.toDateString();
        if (isTomorrow) return 'tomorrow ';

        return `${date.toLocaleDateString([], { weekday: 'short' })} `;
    }

    /**
     * Get a human-readable domain label for display in the UI.
     */
    private _getDomainLabel(domain: string): string {
        const labels: Record<string, string> = {
            binary_sensor: 'Sensor',
            calendar: 'Calendar',
            input_boolean: 'Toggle',
            schedule: 'Schedule',
            switch: 'Switch'
        };
        return labels[domain] ?? domain;
    }

    /**
     * Render a unified condition entity display.
     * Consistent structure across all domain types with domain-specific context.
     */
    private _renderConditionEntity(entity: ConditionEntityInfo, showEdit = false): TemplateResult {
        const isActive = entity.state === 'on';
        const statusIcon = this._getConditionEntityIcon(entity.domain, isActive);
        const statusText = isActive ? 'Not blocking' : 'Blocking access';
        const statusClass = isActive ? 'active' : 'inactive';
        const displayName = entity.friendly_name ?? entity.condition_entity_id;
        const domainLabel = this._getDomainLabel(entity.domain);

        // Build context lines based on domain
        let contextLines: TemplateResult | typeof nothing = nothing;

        if (entity.domain === 'calendar') {
            if (isActive && entity.calendar) {
                // Active calendar: show current event + next event preview
                contextLines = html`
                    ${entity.calendar.summary
                        ? html`<div class="condition-context">
                              <span class="condition-context-label">Event:</span>${entity.calendar
                                  .summary}
                          </div>`
                        : nothing}
                    ${entity.calendar.start_time
                        ? html`<div class="condition-context">
                              <span class="condition-context-label">Started:</span>
                              <ha-relative-time
                                  .hass=${this._hass}
                                  .datetime=${entity.calendar.start_time}
                              ></ha-relative-time>
                          </div>`
                        : nothing}
                    ${entity.calendar.end_time
                        ? html`<div class="condition-context">
                              <span class="condition-context-label">Ends:</span>
                              <ha-relative-time
                                  .hass=${this._hass}
                                  .datetime=${entity.calendar.end_time}
                              ></ha-relative-time>
                          </div>`
                        : nothing}
                    ${entity.calendar_next
                        ? html`<div class="condition-context condition-context-next">
                              <span class="condition-context-label">Next:</span>${entity
                                  .calendar_next.summary ?? 'Event'}
                              starts
                              <ha-relative-time
                                  .hass=${this._hass}
                                  .datetime=${entity.calendar_next.start_time}
                              ></ha-relative-time>
                          </div>`
                        : nothing}
                `;
            } else if (!isActive && entity.calendar_next) {
                // Inactive calendar: show next event details
                contextLines = html`
                    <div class="condition-context">
                        <span class="condition-context-label">Next:</span>${entity.calendar_next
                            .summary ?? 'Event'}
                    </div>
                    <div class="condition-context">
                        <span class="condition-context-label">Starts:</span>
                        <ha-relative-time
                            .hass=${this._hass}
                            .datetime=${entity.calendar_next.start_time}
                        ></ha-relative-time>
                    </div>
                `;
            }
        } else if (entity.domain === 'schedule' && entity.schedule?.next_event) {
            // Schedule: show timing info consistently
            const nextEvent = new Date(entity.schedule.next_event);
            const timeStr = nextEvent.toLocaleTimeString([], {
                hour: 'numeric',
                minute: '2-digit'
            });
            const dateStr = this._formatScheduleDate(nextEvent);

            if (isActive) {
                // Active schedule: show when it ends
                contextLines = html`
                    <div class="condition-context">
                        <span class="condition-context-label">Ends:</span>
                        ${dateStr}${timeStr}
                    </div>
                `;
            } else {
                // Inactive schedule: show when it starts
                contextLines = html`
                    <div class="condition-context">
                        <span class="condition-context-label">Starts:</span>
                        ${dateStr}${timeStr}
                    </div>
                `;
            }
        }
        // input_boolean, switch, binary_sensor: no extra context needed

        return html`
            <div
                class="condition-entity clickable"
                @click=${() => this._openEntityMoreInfo(entity.condition_entity_id)}
                title="Click to view ${displayName}"
            >
                <div class="condition-entity-header">
                    <ha-svg-icon
                        class="condition-entity-icon ${statusClass}"
                        .path=${statusIcon}
                    ></ha-svg-icon>
                    <span class="condition-entity-status">${statusText}</span>
                    <span class="condition-entity-domain">${domainLabel}</span>
                    ${showEdit
                        ? html`<ha-svg-icon
                              class="condition-edit-icon"
                              .path=${mdiPencil}
                              title="Edit condition entity"
                              @click=${(e: Event) => {
                                  e.stopPropagation();
                                  this._openConditionDialog('entity');
                              }}
                          ></ha-svg-icon>`
                        : nothing}
                </div>
                <div class="condition-entity-name">${displayName}</div>
                ${contextLines}
            </div>
        `;
    }

    /**
     * Open the more-info dialog for an entity.
     */
    private _openEntityMoreInfo(entityId: string): void {
        const event = new CustomEvent('hass-more-info', {
            bubbles: true,
            composed: true,
            detail: { entityId }
        });
        this.dispatchEvent(event);
    }

    private _renderLockStatusSection(lockStatuses: LockSyncStatus[]): TemplateResult {
        const syncedCount = lockStatuses.filter((l) => l.inSync === true).length;
        const totalCount = lockStatuses.length;

        const headerExtra =
            totalCount > 0
                ? html`<span class="collapsible-badge">${syncedCount}/${totalCount}</span>`
                : undefined;

        const content =
            lockStatuses.length > 0
                ? html`${lockStatuses.map((lock) => this._renderLockRow(lock))}`
                : html`<div class="no-conditions">No locks found</div>`;

        return this._renderCollapsible(
            'Lock Status',
            this._lockStatusExpanded,
            this._toggleLockStatus,
            content,
            headerExtra
        );
    }

    private _renderLockRow(lock: LockSyncStatus): TemplateResult {
        const showSync = this._config?.show_lock_sync !== false;
        const showCodeSensors = this._config?.show_code_sensors !== false;
        const mode = this._config?.code_display ?? DEFAULT_CODE_DISPLAY;
        const showRevealButton = showCodeSensors && mode === 'masked_with_reveal';

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

        // Format code display
        const codeDisplay = this._formatLockCode(lock);

        return html`
            <div class="lock-row">
                ${showSync
                    ? html`<ha-icon
                          class="lcm-sync-icon ${iconClass}"
                          icon="mdi:check-circle"
                      ></ha-icon>`
                    : nothing}
                <div class="lock-info">
                    <span
                        class="lock-name"
                        title="View lock codes"
                        @click=${() => this._navigateToLock(lock.lockEntityId)}
                    >
                        ${lock.name}
                    </span>
                    ${showSync && lock.lastSynced
                        ? html`<span class="lock-synced-time">
                              Last synced to lock
                              <ha-relative-time
                                  .hass=${this._hass}
                                  .datetime=${lock.lastSynced}
                              ></ha-relative-time>
                          </span>`
                        : showSync
                          ? html`<span class="lock-synced-time">${statusText}</span>`
                          : nothing}
                </div>
                ${showCodeSensors && codeDisplay
                    ? html`<div class="lock-code-field">
                          <span class="lock-code-value">${codeDisplay}</span>
                          ${showRevealButton
                              ? html`<ha-icon-button
                                    class="lcm-reveal-button"
                                    .path=${this._revealed ? mdiEyeOff : mdiEye}
                                    @click=${this._toggleReveal}
                                    .label=${this._revealed ? 'Hide codes' : 'Reveal codes'}
                                ></ha-icon-button>`
                              : nothing}
                      </div>`
                    : nothing}
            </div>
        `;
    }

    private _formatLockCode(lock: LockSyncStatus): string | null {
        const mode = this._config?.code_display ?? DEFAULT_CODE_DISPLAY;
        const shouldMask = mode === 'masked' || (mode === 'masked_with_reveal' && !this._revealed);

        if (lock.code !== null && lock.code !== '') {
            return shouldMask ? '•'.repeat(String(lock.code).length) : String(lock.code);
        }
        if (lock.codeLength) {
            return '•'.repeat(lock.codeLength);
        }
        return null;
    }

    // _toggleReveal inherited from mixin

    // Render helper for collapsible sections
    private _renderCollapsible(
        title: string,
        expanded: boolean,
        onToggle: () => void,
        content: TemplateResult,
        headerExtra?: TemplateResult
    ): TemplateResult {
        return html`
            <div class="collapsible-section">
                <div class="collapsible-header" @click=${onToggle}>
                    <div class="collapsible-title">${title} ${headerExtra ?? nothing}</div>
                    <ha-svg-icon
                        class="collapsible-chevron"
                        .path=${expanded ? mdiChevronUp : mdiChevronDown}
                    ></ha-svg-icon>
                </div>
                <div class="collapsible-content ${expanded ? 'expanded' : ''}">${content}</div>
            </div>
        `;
    }

    private _toggleConditions(): void {
        this._conditionsExpanded = !this._conditionsExpanded;
    }

    private _toggleLockStatus(): void {
        this._lockStatusExpanded = !this._lockStatusExpanded;
    }

    private _renderManageConditionsRow(): TemplateResult {
        return html`
            <div class="manage-conditions-row" @click=${() => this._openConditionDialog('both')}>
                <ha-svg-icon .path=${mdiCog}></ha-svg-icon>
                <span>Manage Conditions</span>
            </div>
        `;
    }

    private _openConditionDialog(mode: 'entity' | 'uses' | 'both'): void {
        this._dialogMode = mode;
        const conditions = this._data?.conditions;

        // Initialize dialog state from current values
        this._dialogEntityId = conditions?.condition_entity?.condition_entity_id ?? null;
        const currentUses = conditions?.number_of_uses;
        this._dialogEnableUses = currentUses !== undefined && currentUses !== null;
        // Default to 5 if adding new
        this._dialogNumberOfUses = currentUses ?? 5;

        this._showConditionDialog = true;
    }

    private _closeConditionDialog(): void {
        this._showConditionDialog = false;
        this._dialogSaving = false;
    }

    private _renderConditionDialog(): TemplateResult {
        const showEntitySection = this._dialogMode === 'entity' || this._dialogMode === 'both';
        const showUsesSection = this._dialogMode === 'uses' || this._dialogMode === 'both';
        const hasExistingEntity = this._data?.conditions?.condition_entity !== undefined;
        const hasExistingUses =
            this._data?.conditions?.number_of_uses !== undefined &&
            this._data?.conditions?.number_of_uses !== null;

        const dialogTitle =
            this._dialogMode === 'entity'
                ? 'Edit Condition Entity'
                : this._dialogMode === 'uses'
                  ? 'Edit Number of Uses'
                  : 'Manage Conditions';

        return html`
            <ha-dialog open @closed=${this._closeConditionDialog} .heading=${dialogTitle}>
                <div class="dialog-content">
                    ${showEntitySection
                        ? html`
                              <div class="dialog-section">
                                  <div class="dialog-section-header">Condition Entity</div>
                                  <div class="dialog-section-description">
                                      PIN is active only when this entity is "on"
                                  </div>
                                  <ha-entity-picker
                                      .hass=${this._hass}
                                      .value=${this._dialogEntityId ?? ''}
                                      .includeDomains=${[
                                          'calendar',
                                          'schedule',
                                          'binary_sensor',
                                          'switch',
                                          'input_boolean'
                                      ]}
                                      .label=${'Select entity'}
                                      @value-changed=${(e: CustomEvent) => {
                                          this._dialogEntityId = e.detail.value || null;
                                      }}
                                  ></ha-entity-picker>
                                  ${hasExistingEntity
                                      ? html`<button
                                            class="dialog-clear-button"
                                            @click=${() => {
                                                this._dialogEntityId = null;
                                            }}
                                        >
                                            Clear entity
                                        </button>`
                                      : nothing}
                              </div>
                          `
                        : nothing}
                    ${showUsesSection
                        ? html`
                              <div class="dialog-section">
                                  <div class="dialog-section-header">Number of Uses</div>
                                  <div class="dialog-section-description">
                                      Limit how many times this PIN can be used
                                  </div>
                                  <div class="dialog-checkbox-row">
                                      <ha-checkbox
                                          .checked=${this._dialogEnableUses}
                                          @change=${(e: Event) => {
                                              this._dialogEnableUses = (
                                                  e.target as HTMLInputElement
                                              ).checked;
                                          }}
                                      ></ha-checkbox>
                                      <label
                                          @click=${() => {
                                              this._dialogEnableUses = !this._dialogEnableUses;
                                          }}
                                      >
                                          Enable use tracking
                                      </label>
                                  </div>
                                  ${this._dialogEnableUses
                                      ? html`<div class="dialog-number-input">
                                            <label>Initial uses:</label>
                                            <input
                                                type="number"
                                                min="1"
                                                .value=${String(this._dialogNumberOfUses ?? 5)}
                                                @input=${(e: Event) => {
                                                    const val = parseInt(
                                                        (e.target as HTMLInputElement).value,
                                                        10
                                                    );
                                                    if (!isNaN(val) && val > 0) {
                                                        this._dialogNumberOfUses = val;
                                                    }
                                                }}
                                            />
                                        </div>`
                                      : hasExistingUses
                                        ? html`<div
                                              class="dialog-section-description"
                                              style="color: var(--warning-color);"
                                          >
                                              Use tracking will be removed
                                          </div>`
                                        : nothing}
                              </div>
                          `
                        : nothing}
                </div>
                <mwc-button slot="secondaryAction" @click=${this._closeConditionDialog}>
                    Cancel
                </mwc-button>
                <mwc-button
                    slot="primaryAction"
                    @click=${this._saveConditionChanges}
                    .disabled=${this._dialogSaving}
                >
                    ${this._dialogSaving ? 'Saving...' : 'Save'}
                </mwc-button>
            </ha-dialog>
        `;
    }

    private async _saveConditionChanges(): Promise<void> {
        if (!this._hass || !this._config) return;

        this._dialogSaving = true;

        try {
            const msg: MessageBase & Record<string, unknown> = {
                slot: this._config.slot,
                type: 'lock_code_manager/update_slot_condition'
            };

            // Add config entry identifier
            if (this._config.config_entry_id) {
                msg.config_entry_id = this._config.config_entry_id;
            } else if (this._config.config_entry_title) {
                msg.config_entry_title = this._config.config_entry_title;
            }

            // Add entity_id if in entity or both mode
            if (this._dialogMode === 'entity' || this._dialogMode === 'both') {
                msg.entity_id = this._dialogEntityId;
            }

            // Add number_of_uses if in uses or both mode
            if (this._dialogMode === 'uses' || this._dialogMode === 'both') {
                msg.number_of_uses = this._dialogEnableUses ? this._dialogNumberOfUses : null;
            }

            await this._hass.callWS(msg);
            this._closeConditionDialog();
        } catch (err) {
            this._setActionError(
                `Failed to update conditions: ${err instanceof Error ? err.message : 'Unknown error'}`
            );
            this._dialogSaving = false;
        }
    }

    private async _handleEnabledToggle(e: Event): Promise<void> {
        const target = e.target as HTMLInputElement;
        const newState = target.checked;

        if (!this._hass) return;
        const enabledEntityId = this._data?.entities?.enabled ?? undefined;
        if (!enabledEntityId) return;

        const service = newState ? 'turn_on' : 'turn_off';
        try {
            await this._hass.callService('switch', service, {
                entity_id: enabledEntityId
            });
        } catch (err) {
            this._setActionError(
                `Failed to ${newState ? 'enable' : 'disable'} slot: ${err instanceof Error ? err.message : 'Unknown error'}`
            );
        }
    }

    // Consolidated edit handlers for name, pin, and numberOfUses fields
    private _startEditing(field: 'name' | 'pin' | 'numberOfUses'): void {
        // Special handling for PIN: reveal first to show current value
        if (field === 'pin' && !this._revealed) {
            this._revealed = true;
            this._unsubscribe();
            void this._subscribe().then(() => {
                this._editingField = 'pin';
            });
        } else {
            this._editingField = field;
        }
    }

    private _handleEditBlur(e: Event): void {
        const target = e.target as HTMLInputElement;
        this._saveEditValue(target.value);
        this._editingField = null;
    }

    private _handleEditKeydown(e: KeyboardEvent): void {
        if (e.key === 'Enter') {
            const target = e.target as HTMLInputElement;
            this._saveEditValue(target.value);
            this._editingField = null;
        } else if (e.key === 'Escape') {
            this._editingField = null;
        }
    }

    private async _saveEditValue(rawValue: string): Promise<void> {
        if (!this._hass || !this._editingField) return;

        const fieldConfig: Record<
            'name' | 'pin' | 'numberOfUses',
            {
                entityKey: keyof NonNullable<SlotCardData['entities']>;
                service: string;
                serviceData: (v: string) => Record<string, unknown>;
            }
        > = {
            name: {
                entityKey: 'name',
                service: 'text.set_value',
                serviceData: (v) => {
                    return { value: v.trim() };
                }
            },
            numberOfUses: {
                entityKey: 'number_of_uses',
                service: 'number.set_value',
                serviceData: (v) => {
                    const num = parseInt(v, 10);
                    return !isNaN(num) && num >= 0 ? { value: num } : {};
                }
            },
            pin: {
                entityKey: 'pin',
                service: 'text.set_value',
                serviceData: (v) => {
                    return { value: v.trim() };
                }
            }
        };

        const config = fieldConfig[this._editingField];
        const entityId = this._data?.entities?.[config.entityKey];
        const fieldLabel =
            this._editingField === 'numberOfUses' ? 'number of uses' : this._editingField;

        if (!entityId) {
            this._setActionError(`Cannot update ${fieldLabel}: entity is unavailable`);
            return;
        }

        // Check if entity exists and is available
        const entityState = this._hass.states[entityId];
        if (!entityState || entityState.state === 'unavailable') {
            this._setActionError(`Cannot update ${fieldLabel}: entity is unavailable or disabled`);
            return;
        }

        const serviceData = config.serviceData(rawValue);
        // Skip if invalid value (e.g., non-numeric for numberOfUses)
        if (Object.keys(serviceData).length === 0) return;

        const [domain, service] = config.service.split('.');
        try {
            await this._hass.callService(domain, service, {
                entity_id: entityId,
                ...serviceData
            });
        } catch (err) {
            this._setActionError(
                `Failed to update ${fieldLabel}: ${err instanceof Error ? err.message : 'Unknown error'}`
            );
        }
    }

    private _setActionError(message: string): void {
        this._actionError = message;
        // Auto-dismiss after 5 seconds
        setTimeout(() => {
            this._actionError = undefined;
        }, 5000);
    }

    private _dismissActionError(): void {
        this._actionError = undefined;
    }

    private _navigateToLock(lockEntityId: string): void {
        // Open the more-info dialog for this lock entity
        const event = new CustomEvent('hass-more-info', {
            bubbles: true,
            composed: true,
            detail: { entityId: lockEntityId }
        });
        this.dispatchEvent(event);
    }

    // _unsubscribe, _shouldReveal, _subscribe inherited from mixin
}

customElements.define('lcm-slot', LockCodeManagerSlotCard);

declare global {
    interface Window {
        customCards?: Array<{ description: string; name: string; type: string }>;
    }
}

window.customCards = window.customCards || [];
window.customCards.push({
    description: 'Displays and controls a Lock Code Manager code slot',
    name: 'LCM Slot Card',
    type: 'custom:lcm-slot'
});
