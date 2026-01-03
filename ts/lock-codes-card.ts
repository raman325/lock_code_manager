import { mdiCheck, mdiClose, mdiEye, mdiEyeOff } from '@mdi/js';
import { MessageBase } from 'home-assistant-js-websocket';
import { LitElement, TemplateResult, css, html, nothing } from 'lit';
import { property, state } from 'lit/decorators.js';

import { HomeAssistant } from './ha_type_stubs';
import { lcmBadgeStyles, lcmCodeStyles, lcmCssVars, lcmRevealButtonStyles } from './shared-styles';
import { LcmSubscriptionMixin } from './subscription-mixin';
import {
    CodeDisplayMode,
    LockCodesCardConfig,
    LockCoordinatorData,
    LockCoordinatorSlotData
} from './types';

const DEFAULT_TITLE = 'Lock Codes';
const DEFAULT_CODE_DISPLAY: CodeDisplayMode = 'masked_with_reveal';

// Base class with subscription mixin
const LockCodesCardBase = LcmSubscriptionMixin(LitElement);

interface SlotGroup {
    /** For empty groups, the range string like "4-10" or "4, 6, 8" */
    rangeLabel?: string;
    slots: LockCoordinatorSlotData[];
    type: 'active' | 'empty';
}

class LockCodesCard extends LockCodesCardBase {
    static styles = [
        lcmCssVars,
        lcmBadgeStyles,
        lcmCodeStyles,
        lcmRevealButtonStyles,
        css`
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
                grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
            }

            .slot-chip {
                background: var(--lcm-section-bg);
                border-radius: 12px;
                display: flex;
                flex-direction: column;
                gap: 6px;
                padding: 12px 12px 14px;
                position: relative;
            }

            /* Active LCM Managed: Primary blue with tinted background */
            .slot-chip.active.managed {
                background: var(--lcm-active-bg-gradient);
            }

            /* Active Unmanaged (not LCM): Neutral gray, plain background */
            .slot-chip.active.unmanaged {
                background: linear-gradient(
                    135deg,
                    rgba(var(--rgb-primary-text-color), 0.06),
                    rgba(var(--rgb-primary-text-color), 0.02)
                );
            }

            /* Inactive LCM Managed: Muted blue, slightly faded */
            .slot-chip.inactive.managed {
                background: rgba(var(--rgb-primary-color), 0.05);
                opacity: 0.85;
            }

            /* Disabled LCM Managed: Very muted, clear disabled state */
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

            /* Disabled slot: strikethrough name */
            .slot-chip.disabled .slot-name {
                color: var(--secondary-text-color);
                text-decoration: line-through;
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
                width: 100%;
            }

            .summary-table th,
            .summary-table td {
                padding: 6px 8px;
                text-align: center;
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
        `
    ];

    // Note: _revealed, _unsub, _subscribing provided by LcmSubscriptionMixin
    @property({ attribute: false }) _hass?: HomeAssistant;

    /** Slot currently being edited (for unmanaged slots) */
    @state() private _editingSlot: number | string | null = null;
    /** Current edit value */
    @state() private _editValue = '';
    /** Whether we're saving (to prevent double-submit) */
    @state() private _saving = false;

    _config?: LockCodesCardConfig;
    _data?: LockCoordinatorData;
    _error?: string;

    set hass(hass: HomeAssistant) {
        this._hass = hass;
        void this._subscribe();
    }

    static getConfigElement(): HTMLElement {
        return document.createElement('lcm-lock-codes-card-editor');
    }

    static getStubConfig(): Partial<LockCodesCardConfig> {
        return { lock_entity_id: '' };
    }

    setConfig(config: LockCodesCardConfig): void {
        if (!config.lock_entity_id) {
            throw new Error('lock_entity_id is required');
        }
        if (this._config?.lock_entity_id && this._config.lock_entity_id !== config.lock_entity_id) {
            this._unsubscribe();
            this._data = undefined;
        }
        this._config = config;
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
        return {
            lock_entity_id: this._config.lock_entity_id,
            reveal: this._shouldReveal(),
            type: 'lock_code_manager/subscribe_lock_slot_data'
        };
    }

    protected _handleSubscriptionData(data: unknown): void {
        this._data = data as LockCoordinatorData;
    }

    // connectedCallback and disconnectedCallback provided by mixin

    protected render(): TemplateResult {
        const hassLockName =
            this._hass?.states[this._config?.lock_entity_id ?? '']?.attributes?.friendly_name;
        const lockName =
            this._data?.lock_name ?? hassLockName ?? this._config?.lock_entity_id ?? '';
        const headerTitle = this._config?.title ?? lockName ?? DEFAULT_TITLE;

        return html`
            <ha-card>
                <div class="card-header">
                    <div class="header-icon">
                        <ha-icon icon="mdi:lock-smart"></ha-icon>
                    </div>
                    <span class="card-header-title">${headerTitle}</span>
                </div>
                <div class="card-content">
                    ${this._error
                        ? html`<div class="message">${this._error}</div>`
                        : this._renderSlots()}
                    ${this._renderSummaryTable()}
                </div>
            </ha-card>
        `;
    }

    // Editing methods for unmanaged slots
    private _startEditing(e: Event, slot: LockCoordinatorSlotData): void {
        e.stopPropagation();
        // For editing, we need the actual code - trigger reveal if masked
        if (!this._revealed) {
            this._revealed = true;
            this._unsubscribe();
            void this._subscribe();
        }
        // Get the current code value (if any)
        const currentCode = slot.code !== null ? String(slot.code) : '';
        this._editValue = currentCode;
        this._editingSlot = slot.slot;
        // Focus input after render
        this.updateComplete.then(() => {
            const input = this.shadowRoot?.querySelector<HTMLInputElement>('.slot-code-input');
            input?.focus();
            input?.select();
        });
    }

    private _handleEditInput(e: Event): void {
        const input = e.target as HTMLInputElement;
        this._editValue = input.value;
    }

    private _handleEditKeydown(e: KeyboardEvent): void {
        if (e.key === 'Enter' && this._editingSlot !== null) {
            void this._saveCode(this._editingSlot);
        } else if (e.key === 'Escape') {
            this._cancelEdit();
        }
    }

    private _cancelEdit(): void {
        this._editingSlot = null;
        this._editValue = '';
    }

    private async _saveCode(slot: number | string): Promise<void> {
        if (!this._hass || !this._config || this._saving) return;

        this._saving = true;
        const usercode = this._editValue.trim();

        try {
            await this._hass.connection.sendMessagePromise({
                code_slot: typeof slot === 'string' ? parseInt(slot, 10) : slot,
                lock_entity_id: this._config.lock_entity_id,
                type: 'lock_code_manager/set_lock_usercode',
                usercode: usercode || undefined
            });
            // Success - exit edit mode
            this._editingSlot = null;
            this._editValue = '';
        } catch (err) {
            // eslint-disable-next-line no-console
            console.error('Failed to set usercode:', err);
        } finally {
            this._saving = false;
        }
    }

    private _navigateToSlot(configEntryId: string | undefined): void {
        if (!configEntryId) return;
        // Navigate to the LCM config entry page
        const url = `/config/integrations/integration/lock_code_manager#config_entry=${configEntryId}`;
        history.pushState(null, '', url);
        window.dispatchEvent(new CustomEvent('location-changed'));
    }

    // _toggleReveal, _unsubscribe, _shouldReveal, _subscribe inherited from mixin

    private _renderSlots(): TemplateResult {
        const slots = this._data?.slots ?? [];
        if (slots.length === 0) {
            return html`<div class="message">No codes reported</div>`;
        }

        const groups = this._groupSlots(slots);
        return html`
            <div class="slots-grid">
                ${groups.map((group) =>
                    group.type === 'active'
                        ? group.slots.map((slot) =>
                              this._renderSlotChip(slot, group.slots.length === 1)
                          )
                        : this._renderEmptySummary(group)
                )}
            </div>
        `;
    }

    private _groupSlots(slots: LockCoordinatorSlotData[]): SlotGroup[] {
        const groups: SlotGroup[] = [];
        let currentEmpty: LockCoordinatorSlotData[] = [];
        let currentActive: LockCoordinatorSlotData[] = [];

        const flushEmpty = (): void => {
            if (currentEmpty.length > 0) {
                groups.push({
                    rangeLabel: this._formatSlotRange(currentEmpty),
                    slots: currentEmpty,
                    type: 'empty'
                });
                currentEmpty = [];
            }
        };

        const flushActive = (): void => {
            if (currentActive.length > 0) {
                groups.push({ slots: currentActive, type: 'active' });
                currentActive = [];
            }
        };

        for (const slot of slots) {
            // Expand slots that:
            // - Have active code on lock, OR
            // - Are managed by LCM AND have either configured code or explicit state
            const hasConfiguredCode = !!(slot.configured_code || slot.configured_code_length);
            const hasManagedState =
                slot.managed === true && (slot.enabled !== undefined || slot.active !== undefined);
            const shouldExpand =
                this._hasCode(slot) ||
                (slot.managed === true && (hasConfiguredCode || hasManagedState));
            if (shouldExpand) {
                flushEmpty();
                currentActive.push(slot);
            } else {
                flushActive();
                currentEmpty.push(slot);
            }
        }
        flushActive();
        flushEmpty();

        return groups;
    }

    private _formatSlotRange(slots: LockCoordinatorSlotData[]): string {
        if (slots.length === 0) return '';
        if (slots.length === 1) return `${slots[0].slot}`;

        const nums = slots.map((s) => Number(s.slot)).filter((n) => !isNaN(n));
        if (nums.length !== slots.length) {
            // Non-numeric slots, just list them
            return `${slots.map((s) => s.slot).join(', ')}`;
        }

        // Find consecutive ranges
        const ranges: string[] = [];
        const [startValue] = nums;
        let start = startValue;
        let end = startValue;

        for (let i = 1; i < nums.length; i++) {
            if (nums[i] === end + 1) {
                end = nums[i];
            } else {
                ranges.push(start === end ? `${start}` : `${start} – ${end}`);
                start = nums[i];
                end = nums[i];
            }
        }
        ranges.push(start === end ? `${start}` : `${start} – ${end}`);

        return `${ranges.join(', ')}`;
    }

    private _renderSlotChip(slot: LockCoordinatorSlotData, isAlone: boolean): TemplateResult {
        const hasCode = this._hasCode(slot);
        const slotName = slot.name?.trim();
        const { managed } = slot;
        const hasConfiguredCode = !!(slot.configured_code || slot.configured_code_length);
        const mode = this._config?.code_display ?? DEFAULT_CODE_DISPLAY;
        const showName = !!slotName || managed !== false;
        const isClickable = managed === true && !!slot.config_entry_id;

        // Determine state for LCM-managed slots:
        // - Active: active=true (code on lock, conditions met)
        // - Inactive: enabled=true + active=false (enabled but conditions blocking)
        // - Disabled: enabled=false (user explicitly disabled)
        // - Empty: no code and no configured_code (unmanaged empty slot)
        let stateClass: string;
        let statusText: string;
        let statusClass: string;

        if (slot.active === true) {
            // Binary sensor ON = active (code on lock, conditions met)
            stateClass = 'active';
            statusText = 'Active';
            statusClass = 'active';
        } else if (slot.enabled === true && slot.active === false) {
            // Enabled switch ON but binary sensor OFF = inactive (conditions blocking)
            stateClass = 'inactive';
            statusText = 'Inactive';
            statusClass = 'inactive';
        } else if (slot.enabled === false) {
            // Enabled switch OFF = disabled by user
            stateClass = 'disabled';
            statusText = 'Disabled';
            statusClass = 'disabled';
        } else if (hasCode) {
            // Fallback: has code on lock but no LCM state info
            stateClass = 'active';
            statusText = 'Active';
            statusClass = 'active';
        } else if (hasConfiguredCode) {
            // Fallback: has configured code but unknown state
            stateClass = 'inactive';
            statusText = 'Inactive';
            statusClass = 'inactive';
        } else {
            stateClass = 'empty';
            statusText = 'Empty';
            statusClass = 'empty';
        }

        // Determine managed class for styling
        const managedClass = managed === true ? 'managed' : managed === false ? 'unmanaged' : '';
        const clickableClass = isClickable ? 'clickable' : '';

        return html`
            <div
                class="slot-chip ${stateClass} ${managedClass} ${clickableClass} ${isAlone
                    ? 'full-width'
                    : ''}"
                title=${isClickable ? 'Click to manage this slot' : nothing}
                @click=${isClickable ? () => this._navigateToSlot(slot.config_entry_id) : nothing}
            >
                <div class="slot-top">
                    <span class="slot-label">Slot ${slot.slot}</span>
                    <div class="slot-badges">
                        <span class="lcm-badge ${statusClass}"> ${statusText} </span>
                        ${managed === undefined
                            ? nothing
                            : html`<span class="lcm-badge ${managed ? 'managed' : 'external'}">
                                  ${managed ? 'Managed' : 'Unmanaged'}
                              </span>`}
                    </div>
                </div>
                ${showName
                    ? html`<span class="slot-name ${slotName ? '' : 'unnamed'}">
                          ${slotName ?? 'Unnamed'}
                      </span>`
                    : nothing}
                ${this._renderCodeSection(slot, hasCode, mode)}
            </div>
        `;
    }

    private _renderCodeSection(
        slot: LockCoordinatorSlotData,
        hasCode: boolean,
        mode: CodeDisplayMode
    ): TemplateResult {
        const isEditing = this._editingSlot === slot.slot;
        const isUnmanaged = slot.managed !== true;

        // Editing mode for unmanaged slots
        if (isEditing && isUnmanaged) {
            return html`
                <div class="slot-code-edit" @click=${(e: Event) => e.stopPropagation()}>
                    <div class="slot-code-edit-row">
                        <input
                            class="slot-code-input"
                            type="text"
                            inputmode="numeric"
                            pattern="[0-9]*"
                            placeholder="Enter PIN or leave empty to clear"
                            .value=${this._editValue}
                            @input=${this._handleEditInput}
                            @keydown=${this._handleEditKeydown}
                            ?disabled=${this._saving}
                        />
                        <div class="slot-code-edit-buttons">
                            <ha-icon-button
                                .path=${mdiCheck}
                                @click=${() => this._saveCode(slot.slot)}
                                .label=${'Save'}
                                ?disabled=${this._saving}
                            ></ha-icon-button>
                            <ha-icon-button
                                .path=${mdiClose}
                                @click=${this._cancelEdit}
                                .label=${'Cancel'}
                                ?disabled=${this._saving}
                            ></ha-icon-button>
                        </div>
                    </div>
                    <span class="slot-edit-help">
                        ${this._saving ? 'Saving...' : 'Enter to save, Esc to cancel'}
                    </span>
                </div>
            `;
        }

        // Normal display mode
        const isEditable = isUnmanaged && !isEditing;
        const editableClass = isEditable ? 'editable' : '';

        return html`
            <div class="slot-code-row">
                <span
                    class="lcm-code ${this._getCodeClass(slot)} ${editableClass}"
                    title=${isEditable ? 'Click to edit' : nothing}
                    @click=${isEditable ? (e: Event) => this._startEditing(e, slot) : nothing}
                >
                    ${this._formatCode(slot)}
                </span>
                ${mode === 'masked_with_reveal' && hasCode
                    ? html`<span class="slot-code-actions">
                          <ha-icon-button
                              class="lcm-reveal-button"
                              .path=${this._revealed ? mdiEyeOff : mdiEye}
                              @click=${this._toggleReveal}
                              .label=${this._revealed ? 'Hide codes' : 'Reveal codes'}
                          ></ha-icon-button>
                      </span>`
                    : nothing}
            </div>
        `;
    }

    private _getCodeClass(slot: LockCoordinatorSlotData): string {
        const mode = this._config?.code_display ?? DEFAULT_CODE_DISPLAY;
        const shouldMask = mode === 'masked' || (mode === 'masked_with_reveal' && !this._revealed);

        // Active code on the lock
        if (slot.code !== null && slot.code !== '') return '';
        if (slot.code_length) return 'masked';

        // No active code - check for configured code (disabled LCM slot)
        if (slot.configured_code) {
            // We have the actual code - choose class based on display mode
            return shouldMask ? 'disabled masked' : 'disabled';
        }
        if (slot.configured_code_length) {
            // Only have length (always masked)
            return 'disabled masked';
        }

        return 'no-code';
    }

    private _formatCode(slot: LockCoordinatorSlotData): string {
        const mode = this._config?.code_display ?? DEFAULT_CODE_DISPLAY;
        const shouldMask = mode === 'masked' || (mode === 'masked_with_reveal' && !this._revealed);

        // Active code on the lock
        if (slot.code !== null && slot.code !== '') {
            return shouldMask ? '•'.repeat(String(slot.code).length) : String(slot.code);
        }
        if (slot.code_length) {
            return '•'.repeat(slot.code_length);
        }

        // Disabled LCM slot: show configured code (respect masking)
        if (slot.configured_code) {
            return shouldMask ? '•'.repeat(slot.configured_code.length) : slot.configured_code;
        }
        if (slot.configured_code_length) {
            return '•'.repeat(slot.configured_code_length);
        }

        return '—';
    }

    private _renderEmptySummary(group: SlotGroup): TemplateResult {
        return html`<div class="empty-summary">
            <ha-icon icon="mdi:minus-circle-outline"></ha-icon>
            <span class="empty-summary-label">Empty slots</span>
            <span class="empty-summary-range">${group.rangeLabel}</span>
        </div>`;
    }

    private _renderSummaryTable(): TemplateResult {
        const slots = this._data?.slots ?? [];
        if (slots.length === 0) {
            return html``;
        }

        // Count by state and managed/unmanaged
        // All slots must be counted - total should equal slots.length
        let managedActive = 0;
        let managedInactive = 0;
        let managedDisabled = 0;
        let unmanagedActive = 0;
        // Empty unmanaged slots (no code, collapsed in UI)
        let unmanagedInactive = 0;

        for (const slot of slots) {
            const hasCode = this._hasCode(slot);
            const isManaged = slot.managed === true;
            const hasConfiguredCode = !!(slot.configured_code || slot.configured_code_length);

            if (isManaged) {
                // Managed slot - use LCM state fields
                if (slot.active === true) {
                    managedActive += 1;
                } else if (slot.enabled === true && slot.active === false) {
                    // Enabled but conditions blocking
                    managedInactive += 1;
                } else if (slot.enabled === false) {
                    // User explicitly disabled via LCM switch
                    managedDisabled += 1;
                } else if (hasCode) {
                    // Fallback: has code on lock but no state info - treat as active
                    managedActive += 1;
                } else if (hasConfiguredCode) {
                    // Fallback: configured code but unknown state - treat as inactive
                    managedInactive += 1;
                } else {
                    // Fallback: no code and no state info - treat as inactive
                    managedInactive += 1;
                }
            } else if (hasCode) {
                // Unmanaged slot with code = active
                unmanagedActive += 1;
            } else {
                // Unmanaged slot without code = inactive (collapsed in UI)
                unmanagedInactive += 1;
            }
        }

        const managedTotal = managedActive + managedInactive + managedDisabled;
        const unmanagedTotal = unmanagedActive + unmanagedInactive;
        const totalActive = managedActive + unmanagedActive;
        const totalInactive = managedInactive + unmanagedInactive;
        const totalDisabled = managedDisabled;
        // Should always equal managedTotal + unmanagedTotal
        const grandTotal = slots.length;

        const cellClass = (val: number): string => (val === 0 ? 'summary-cell-zero' : '');

        return html`
            <table class="summary-table">
                <thead>
                    <tr>
                        <th></th>
                        <th>Active</th>
                        <th>Inactive</th>
                        <th>Disabled</th>
                        <th>Total</th>
                    </tr>
                </thead>
                <tbody>
                    <tr>
                        <td>Managed</td>
                        <td class="${cellClass(managedActive)}">${managedActive}</td>
                        <td class="${cellClass(managedInactive)}">${managedInactive}</td>
                        <td class="${cellClass(managedDisabled)}">${managedDisabled}</td>
                        <td>${managedTotal}</td>
                    </tr>
                    <tr>
                        <td>Unmanaged</td>
                        <td class="${cellClass(unmanagedActive)}">${unmanagedActive}</td>
                        <td class="${cellClass(unmanagedInactive)}">${unmanagedInactive}</td>
                        <td class="${cellClass(0)}">–</td>
                        <td>${unmanagedTotal}</td>
                    </tr>
                    <tr class="total-row">
                        <td>Total</td>
                        <td class="${cellClass(totalActive)}">${totalActive}</td>
                        <td class="${cellClass(totalInactive)}">${totalInactive}</td>
                        <td class="${cellClass(totalDisabled)}">${totalDisabled}</td>
                        <td>${grandTotal}</td>
                    </tr>
                </tbody>
            </table>
        `;
    }

    private _hasCode(slot: LockCoordinatorSlotData): boolean {
        if (slot.code_length) {
            return true;
        }
        if (slot.code === null || slot.code === '') {
            return false;
        }
        return true;
    }
}

customElements.define('lcm-lock-codes-card', LockCodesCard);

declare global {
    interface Window {
        customCards?: Array<{ description: string; name: string; type: string }>;
    }
}

window.customCards = window.customCards || [];
window.customCards.push({
    description: 'Displays lock slot codes from Lock Code Manager',
    name: 'LCM Lock Codes Card',
    type: 'custom:lcm-lock-codes-card'
});
