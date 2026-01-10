import {
    mdiCalendar,
    mdiCalendarRemove,
    mdiChevronDown,
    mdiChevronUp,
    mdiClock,
    mdiEye,
    mdiEyeOff,
    mdiKey,
    mdiLock,
    mdiPound
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
                font-family: var(--lcm-code-font);
                font-size: var(--lcm-code-font-size);
                font-weight: var(--lcm-code-font-weight);
                letter-spacing: 2px;
            }

            .pin-value.masked {
                color: var(--secondary-text-color);
            }

            .pin-reveal {
                --mdc-icon-button-size: 32px;
                --mdc-icon-size: 18px;
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

            /* Calendar condition */
            .calendar-condition {
                display: flex;
                flex-direction: column;
                gap: 4px;
                padding: 8px 0;
            }

            .calendar-condition.clickable {
                border-radius: 8px;
                cursor: pointer;
                margin: 8px -8px 0;
                padding: 8px;
                transition: background-color 0.2s;
            }

            .calendar-condition.clickable:hover {
                background: var(--lcm-active-bg);
            }

            .calendar-condition:first-child {
                padding-top: 0;
            }

            .calendar-condition.clickable:first-child {
                margin-top: 0;
            }

            .calendar-header {
                align-items: center;
                display: flex;
                gap: 8px;
            }

            .calendar-status {
                align-items: center;
                display: flex;
                gap: 6px;
            }

            .calendar-status-icon {
                --mdc-icon-size: 18px;
            }

            .calendar-status-icon.active {
                color: var(--lcm-disabled-color);
            }

            .calendar-status-icon.inactive {
                color: var(--lcm-warning-color);
            }

            .calendar-status-text {
                color: var(--primary-text-color);
                font-size: 14px;
                font-weight: 500;
            }

            .calendar-event-summary {
                color: var(--primary-text-color);
                font-size: 13px;
                margin-left: 22px;
            }

            .calendar-event-time {
                color: var(--secondary-text-color);
                font-size: 12px;
                margin-left: 22px;
            }

            .calendar-next-event {
                border-top: 1px solid var(--lcm-border-color);
                color: var(--secondary-text-color);
                font-size: 12px;
                margin-left: 22px;
                margin-top: 8px;
                padding-top: 8px;
            }

            .calendar-next-event-label {
                font-weight: 500;
                text-transform: uppercase;
                font-size: 10px;
                letter-spacing: 0.05em;
                margin-bottom: 2px;
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
        `
    ];

    // Note: _revealed, _unsub, _subscribing provided by LcmSubscriptionMixin
    @state() _config?: LockCodeManagerSlotCardConfig;
    @state() _data?: SlotCardData;
    @state() _error?: string;
    @state() private _conditionsExpanded = false;
    @state() private _editingField: 'name' | 'pin' | 'numberOfUses' | null = null;
    @state() private _lockStatusExpanded = false;

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
            conditions.calendar !== undefined;
        const showConditions = this._config.show_conditions !== false && hasConditions;

        return html`
            <ha-card>
                ${this._renderHeader()}
                <div class="content">
                    ${this._renderPrimaryControls(name, pin, pinLength, enabled, mode)}
                    ${this._renderStatus(enabled, active)}
                    ${showConditions ? this._renderConditionsSection(conditions) : nothing}
                    ${showLockStatus ? this._renderLockStatusSection(lockStatuses) : nothing}
                </div>
            </ha-card>
        `;
    }

    private _renderHeader(): TemplateResult {
        const lockCount = this._data?.locks?.length ?? 0;
        const lastUsed = this._data?.last_used;

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
                <div
                    class="header-last-used ${lastUsed ? 'clickable' : ''}"
                    title=${lastUsed
                        ? this._data?.last_used_lock
                            ? `Used on ${this._data.last_used_lock} - Click to view history`
                            : 'Click to view PIN usage history'
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
                </div>
            </div>
        `;
    }

    private _navigateToEventHistory(): void {
        const eventEntityId = this._data?.event_entity_id;
        if (!eventEntityId) return;
        // Navigate to entity history
        const url = `/history?entity_id=${encodeURIComponent(eventEntityId)}`;
        history.pushState(null, '', url);
        window.dispatchEvent(new CustomEvent('location-changed'));
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
                                  class="control-value editable ${name ? '' : 'unnamed'}"
                                  @click=${() => this._startEditing('name')}
                              >
                                  ${name ?? 'Unnamed'}
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
                                      ${displayPin}
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
        const { number_of_uses, calendar, calendar_next } = conditions;
        const hasNumberOfUses = number_of_uses !== undefined && number_of_uses !== null;
        const hasCalendar = calendar !== undefined;
        const usesBlocking = hasNumberOfUses && number_of_uses === 0;
        const calendarBlocking = hasCalendar && calendar.active === false;
        const hasConditions = hasNumberOfUses || hasCalendar;
        const totalConditions = (hasNumberOfUses ? 1 : 0) + (hasCalendar ? 1 : 0);
        const blockingConditions = (usesBlocking ? 1 : 0) + (calendarBlocking ? 1 : 0);

        const headerExtra = hasConditions
            ? html`<span class="collapsible-badge ${blockingConditions > 0 ? 'warning' : 'primary'}"
                      >${blockingConditions}/${totalConditions}</span
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
                      ${hasCalendar
                          ? html`<ha-svg-icon
                                class="condition-icon ${calendarBlocking ? 'blocking' : ''}"
                                .path=${calendarBlocking ? mdiCalendarRemove : mdiCalendar}
                                title="${calendarBlocking
                                    ? 'Calendar blocking access'
                                    : 'Calendar allowing access'}"
                            ></ha-svg-icon>`
                          : nothing}
                  </span>`
            : undefined;

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
                  </div>`
                : nothing}
            ${hasCalendar
                ? this._renderCalendarCondition(
                      calendar,
                      calendar_next,
                      conditions.calendar_entity_id
                  )
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

    private _renderCalendarCondition(
        calendar: NonNullable<SlotCardConditions['calendar']>,
        nextEvent?: SlotCardConditions['calendar_next'],
        calendarEntityId?: string | null
    ): TemplateResult {
        const isActive = calendar.active;
        const statusIcon = isActive ? mdiCalendar : mdiCalendarRemove;
        const statusText = isActive ? 'Access allowed' : 'Access blocked';
        const statusClass = isActive ? 'active' : 'inactive';
        const isClickable = !!calendarEntityId;

        return html`
            <div
                class="calendar-condition ${isClickable ? 'clickable' : ''}"
                @click=${isClickable ? () => this._navigateToCalendar(calendarEntityId) : nothing}
            >
                <div class="calendar-header">
                    <div class="calendar-status">
                        <ha-svg-icon
                            class="calendar-status-icon ${statusClass}"
                            .path=${statusIcon}
                        ></ha-svg-icon>
                        <span class="calendar-status-text">${statusText}</span>
                    </div>
                </div>
                ${isActive && calendar.summary
                    ? html`<div class="calendar-event-summary">${calendar.summary}</div>`
                    : nothing}
                ${isActive && calendar.end_time
                    ? html`<div class="calendar-event-time">
                          Ends
                          <ha-relative-time
                              .hass=${this._hass}
                              .datetime=${calendar.end_time}
                          ></ha-relative-time>
                      </div>`
                    : nothing}
                ${!isActive && nextEvent
                    ? html`<div class="calendar-next-event">
                          <div class="calendar-next-event-label">Next access</div>
                          <div>
                              <ha-relative-time
                                  .hass=${this._hass}
                                  .datetime=${nextEvent.start_time}
                              ></ha-relative-time
                              >${nextEvent.summary ? ` — ${nextEvent.summary}` : ''}
                          </div>
                      </div>`
                    : nothing}
            </div>
        `;
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
            // eslint-disable-next-line no-console
            console.error(`Failed service call: switch.${service} on ${enabledEntityId}`, err);
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
        if (!entityId) return;

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
            // eslint-disable-next-line no-console
            console.error(`Failed service call: ${config.service} on ${entityId}`, err);
        }
    }

    private _navigateToCalendar(calendarEntityId: string): void {
        // Navigate to Home Assistant calendar view with this entity
        // The calendar dashboard URL format is /calendar?entity_id=calendar.xxx
        const url = `/calendar?entity_id=${encodeURIComponent(calendarEntityId)}`;
        history.pushState(null, '', url);
        window.dispatchEvent(new CustomEvent('location-changed'));
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
