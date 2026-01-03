import { mdiEye, mdiEyeOff } from '@mdi/js';
import { LitElement, TemplateResult, css, html, nothing } from 'lit';

import { HomeAssistant } from './ha_type_stubs';
import {
    CodeDisplayMode,
    LockCodeManagerLockDataCardConfig,
    LockCoordinatorData,
    LockCoordinatorSlotData
} from './types';

const DEFAULT_TITLE = 'Lock Codes';
const DEFAULT_CODE_DISPLAY: CodeDisplayMode = 'unmasked';

interface SlotGroup {
    /** For empty groups, the range string like "4-10" or "4, 6, 8" */
    rangeLabel?: string;
    slots: LockCoordinatorSlotData[];
    type: 'active' | 'empty';
}

class LockCodeManagerLockDataCard extends LitElement {
    static styles = css`
        :host {
            display: flex;
            flex-direction: column;
            gap: 8px;
        }

        .header-card {
            background: var(--ha-card-background, var(--card-background-color, #fff));
            border: var(--ha-card-border-width, 1px) solid
                var(--ha-card-border-color, var(--divider-color, #e0e0e0));
            border-radius: var(--ha-card-border-radius, 12px);
            box-shadow: var(--ha-card-box-shadow, 0 2px 2px rgba(0, 0, 0, 0.1));
            padding: 16px;
        }

        .header-title {
            align-items: center;
            display: flex;
            font-size: 1.25em;
            font-weight: 500;
            gap: 8px;
            margin: 0;
        }

        .header-title ha-icon {
            --mdc-icon-size: 24px;
            color: var(--primary-color);
        }

        ha-card {
            padding: 16px;
        }

        .slots-grid {
            display: grid;
            gap: 10px;
            grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
        }

        .slot-chip {
            border: 2px solid rgba(var(--rgb-primary-text-color), 0.06);
            border-radius: 12px;
            display: flex;
            flex-direction: column;
            gap: 6px;
            padding: 12px 12px 14px;
            position: relative;
        }

        /* Active LCM Managed: Primary blue with tinted background */
        .slot-chip.active.managed {
            background: linear-gradient(
                135deg,
                rgba(var(--rgb-primary-color), 0.08),
                rgba(var(--rgb-primary-color), 0.03)
            );
            border-color: var(--primary-color);
        }

        /* Active Unmanaged (not LCM): Neutral gray, plain background */
        .slot-chip.active.unmanaged {
            background: linear-gradient(
                135deg,
                rgba(var(--rgb-primary-text-color), 0.04),
                rgba(var(--rgb-primary-text-color), 0.01)
            );
            border-color: rgba(var(--rgb-primary-text-color), 0.25);
            border-style: solid;
        }

        /* Inactive/Disabled LCM Managed: Muted blue dotted, slightly faded */
        /* Only LCM managed slots can be inactive/disabled (unmanaged are active or empty) */
        .slot-chip.inactive.managed,
        .slot-chip.disabled.managed {
            background: linear-gradient(
                135deg,
                rgba(var(--rgb-primary-color), 0.04),
                rgba(var(--rgb-primary-color), 0.01)
            );
            border-color: var(--primary-color);
            border-style: dotted;
            opacity: 0.75;
        }

        .slot-chip.empty {
            background: linear-gradient(
                135deg,
                rgba(var(--rgb-primary-text-color), 0.03),
                rgba(var(--rgb-primary-text-color), 0.01)
            );
            opacity: 0.7;
        }

        .slot-chip.full-width {
            grid-column: 1 / -1;
            justify-self: center;
            max-width: 360px;
            width: 100%;
        }

        .slot-top {
            align-items: center;
            display: flex;
            justify-content: space-between;
        }

        .slot-badges {
            align-items: center;
            display: inline-flex;
            gap: 6px;
        }

        .slot-label {
            color: var(--secondary-text-color);
            font-size: 11px;
            font-weight: 500;
            letter-spacing: 0.03em;
            text-transform: uppercase;
        }

        .slot-status {
            border-radius: 999px;
            font-size: 10px;
            font-weight: 600;
            letter-spacing: 0.02em;
            padding: 2px 6px;
            text-transform: uppercase;
        }

        .slot-status.active {
            background: rgba(var(--rgb-primary-color), 0.16);
            color: var(--primary-color);
        }

        .slot-status.empty {
            background: rgba(var(--rgb-primary-text-color), 0.08);
            color: var(--secondary-text-color);
        }

        .slot-status.inactive {
            background: rgba(var(--rgb-primary-color), 0.12);
            color: var(--primary-color);
        }

        .slot-status.disabled {
            background: rgba(var(--rgb-warning-color, 255, 152, 0), 0.12);
            color: var(--warning-color, #ff9800);
        }

        .slot-origin {
            border-radius: 999px;
            font-size: 10px;
            font-weight: 600;
            letter-spacing: 0.02em;
            padding: 2px 6px;
            text-transform: uppercase;
        }

        .slot-origin.managed {
            background: rgba(var(--rgb-primary-color), 0.16);
            color: var(--primary-color);
        }

        .slot-origin.external {
            background: rgba(var(--rgb-primary-text-color), 0.08);
            color: var(--secondary-text-color);
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

        .slot-code {
            color: var(--primary-text-color);
            font-family: 'Roboto Mono', monospace;
            font-size: 16px;
            font-weight: 600;
            letter-spacing: 1px;
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

        .slot-code-actions ha-icon-button {
            --mdc-icon-button-size: 28px;
            --mdc-icon-size: 16px;
        }

        .slot-code.masked {
            color: var(--secondary-text-color);
        }

        .slot-code.no-code {
            color: var(--disabled-text-color);
            font-family: inherit;
            font-size: 12px;
            font-style: italic;
            font-weight: 400;
            letter-spacing: normal;
        }

        .slot-code.disabled {
            color: var(--primary-text-color);
            opacity: 0.6;
        }

        .slot-code.disabled.masked {
            color: var(--secondary-text-color);
        }

        .empty-summary {
            align-items: center;
            background: rgba(var(--rgb-primary-text-color), 0.03);
            border: 1px dashed rgba(var(--rgb-primary-text-color), 0.12);
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
            font-size: 11px;
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
            border-top: 1px solid rgba(var(--rgb-primary-text-color), 0.06);
            color: var(--primary-text-color);
            font-weight: 500;
        }

        .summary-table td:first-child {
            color: var(--secondary-text-color);
            font-size: 11px;
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
            border-top: 2px solid rgba(var(--rgb-primary-text-color), 0.08);
            font-weight: 600;
        }

        .summary-cell-zero {
            color: var(--disabled-text-color) !important;
            font-weight: 400 !important;
        }
    `;

    private _hass?: HomeAssistant;
    private _config?: LockCodeManagerLockDataCardConfig;
    private _data?: LockCoordinatorData;
    private _error?: string;
    private _revealed = false;
    private _unsub?: () => void;
    private _subscribing = false;

    set hass(hass: HomeAssistant) {
        this._hass = hass;
        void this._subscribe();
    }

    static getConfigElement(): HTMLElement {
        return document.createElement('lock-code-data-card-editor');
    }

    static getStubConfig(): Partial<LockCodeManagerLockDataCardConfig> {
        return { lock_entity_id: '' };
    }

    setConfig(config: LockCodeManagerLockDataCardConfig): void {
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

    connectedCallback(): void {
        super.connectedCallback();
        void this._subscribe();
    }

    disconnectedCallback(): void {
        super.disconnectedCallback();
        this._unsubscribe();
    }

    protected render(): TemplateResult {
        const hassLockName =
            this._hass?.states[this._config?.lock_entity_id ?? '']?.attributes?.friendly_name;
        const lockName =
            this._data?.lock_name ?? hassLockName ?? this._config?.lock_entity_id ?? '';
        const headerTitle = this._config?.title ?? lockName ?? DEFAULT_TITLE;

        return html`
            <div class="header-card">
                <h2 class="header-title">
                    <ha-icon icon="mdi:lock-smart"></ha-icon>
                    ${headerTitle} – User Codes
                </h2>
            </div>
            <ha-card>
                ${this._error
                    ? html`<div class="message">${this._error}</div>`
                    : this._renderSlots()}
                ${this._renderSummaryTable()}
            </ha-card>
        `;
    }

    private _toggleReveal(): void {
        this._revealed = !this._revealed;
        this._unsubscribe();
        void this._subscribe();
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
            this._unsub = await this._hass.connection.subscribeMessage<LockCoordinatorData>(
                (event) => {
                    this._data = event;
                    this._error = undefined;
                    this.requestUpdate();
                },
                {
                    lock_entity_id: this._config.lock_entity_id,
                    reveal: this._shouldReveal(),
                    type: 'lock_code_manager/subscribe_lock_coordinator_data'
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
            // - Are managed by LCM AND have a configured code (disabled/empty with config)
            const hasConfiguredCode = !!(slot.configured_code || slot.configured_code_length);
            const shouldExpand =
                this._hasCode(slot) || (slot.managed === true && hasConfiguredCode);
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
        } else if (slot.enabled === false && hasConfiguredCode) {
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
            stateClass = 'disabled';
            statusText = 'Disabled';
            statusClass = 'disabled';
        } else {
            stateClass = 'empty';
            statusText = 'Empty';
            statusClass = 'empty';
        }

        // Determine managed class for styling
        const managedClass = managed === true ? 'managed' : managed === false ? 'unmanaged' : '';

        return html`
            <div class="slot-chip ${stateClass} ${managedClass} ${isAlone ? 'full-width' : ''}">
                <div class="slot-top">
                    <span class="slot-label">Slot ${slot.slot}</span>
                    <div class="slot-badges">
                        <span class="slot-status ${statusClass}"> ${statusText} </span>
                        ${managed === undefined
                            ? nothing
                            : html`<span class="slot-origin ${managed ? 'managed' : 'external'}">
                                  ${managed ? 'Managed' : 'Unmanaged'}
                              </span>`}
                    </div>
                </div>
                ${showName
                    ? html`<span class="slot-name ${slotName ? '' : 'unnamed'}">
                          ${slotName ?? 'Unnamed'}
                      </span>`
                    : nothing}
                <div class="slot-code-row">
                    <span class="slot-code ${this._getCodeClass(slot)}">
                        ${this._formatCode(slot)}
                    </span>
                    ${mode === 'masked_with_reveal' && hasCode
                        ? html`<span class="slot-code-actions">
                              <ha-icon-button
                                  .path=${this._revealed ? mdiEyeOff : mdiEye}
                                  @click=${this._toggleReveal}
                                  .label=${this._revealed ? 'Hide codes' : 'Reveal codes'}
                              ></ha-icon-button>
                          </span>`
                        : nothing}
                </div>
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

customElements.define('lock-code-manager-lock-data', LockCodeManagerLockDataCard);

declare global {
    interface Window {
        customCards?: Array<{ description: string; name: string; type: string }>;
    }
}

window.customCards = window.customCards || [];
window.customCards.push({
    description: 'Displays lock slot codes from Lock Code Manager',
    name: 'Lock Code Manager Lock Data',
    type: 'custom:lock-code-manager-lock-data'
});
