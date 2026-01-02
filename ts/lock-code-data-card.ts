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
        ha-card {
            padding: 16px;
        }

        .card-header {
            align-items: center;
            display: grid;
            gap: 8px 12px;
            grid-template-columns: minmax(0, 1fr) auto;
        }

        .title-row {
            align-items: center;
            display: flex;
            gap: 8px;
            min-width: 0;
        }

        .title {
            font-size: 18px;
            font-weight: 600;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: normal;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
        }

        .reveal-toggle {
            align-items: center;
            color: var(--secondary-text-color);
            cursor: pointer;
            display: inline-flex;
            font-size: 13px;
            gap: 4px;
        }

        .reveal-toggle:hover {
            color: var(--primary-text-color);
        }

        .reveal-toggle ha-icon-button {
            --mdc-icon-button-size: 32px;
            --mdc-icon-size: 18px;
        }

        .summary-row {
            align-items: center;
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin: 12px 0 16px;
        }

        .summary-left {
            display: flex;
            gap: 8px;
        }

        .summary-chip {
            background: rgba(var(--rgb-primary-text-color), 0.04);
            border: 1px solid rgba(var(--rgb-primary-text-color), 0.08);
            border-radius: 999px;
            color: var(--secondary-text-color);
            font-size: 12px;
            font-weight: 500;
            padding: 4px 10px;
        }

        .summary-chip strong {
            color: var(--primary-text-color);
            font-weight: 600;
            margin-right: 4px;
        }

        .slots-grid {
            display: grid;
            gap: 10px;
            grid-template-columns: repeat(auto-fill, minmax(170px, 1fr));
        }

        .slot-chip {
            background: linear-gradient(
                135deg,
                rgba(var(--rgb-primary-text-color), 0.03),
                rgba(var(--rgb-primary-text-color), 0.01)
            );
            border: 1px solid rgba(var(--rgb-primary-text-color), 0.06);
            border-radius: 12px;
            display: flex;
            flex-direction: column;
            gap: 6px;
            padding: 12px 12px 14px;
            position: relative;
        }

        .slot-chip.active {
            border-color: rgba(var(--rgb-primary-color), 0.6);
            box-shadow: 0 0 0 1px rgba(var(--rgb-primary-color), 0.2);
        }

        .slot-chip.empty {
            opacity: 0.7;
        }

        .slot-chip.manual {
            background: rgba(var(--rgb-primary-text-color), 0.03);
            border-color: rgba(var(--rgb-primary-text-color), 0.12);
            border-width: 0.5px;
            border-style: solid;
            box-shadow: none;
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
        const title = this._config?.title ?? (lockName || DEFAULT_TITLE);
        const summary = this._getSummary();

        return html`
            <ha-card>
                <div class="card-header">
                    <div class="title-row">
                        <div class="title">${title}</div>
                    </div>
                </div>
                ${summary
                    ? html`<div class="summary-row">
                          <div class="summary-left">
                              <div class="summary-chip">
                                  <strong>${summary.active}/${summary.total}</strong> Active
                              </div>
                          </div>
                      </div>`
                    : nothing}
                ${this._error
                    ? html`<div class="message">${this._error}</div>`
                    : this._renderSlots()}
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
                        ? group.slots.map((slot) => this._renderSlotChip(slot))
                        : this._renderEmptySummary(group)
                )}
            </div>
        `;
    }

    private _groupSlots(slots: LockCoordinatorSlotData[]): SlotGroup[] {
        const groups: SlotGroup[] = [];
        let currentEmpty: LockCoordinatorSlotData[] = [];

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

        for (const slot of slots) {
            const hasCode = this._hasCode(slot);
            if (hasCode) {
                flushEmpty();
                groups.push({ slots: [slot], type: 'active' });
            } else {
                currentEmpty.push(slot);
            }
        }
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

    private _renderSlotChip(slot: LockCoordinatorSlotData): TemplateResult {
        const hasCode = this._hasCode(slot);
        const slotName = slot.name?.trim();
        const managed = hasCode ? slot.managed : undefined;
        const mode = this._config?.code_display ?? DEFAULT_CODE_DISPLAY;
        const showName = !!slotName || managed !== false;
        return html`
            <div
                class="slot-chip ${hasCode ? 'active' : 'empty'} ${managed === false
                    ? 'manual'
                    : ''}"
            >
                <div class="slot-top">
                    <span class="slot-label">Slot ${slot.slot}</span>
                    <div class="slot-badges">
                        <span class="slot-status ${hasCode ? 'active' : 'empty'}">
                            ${hasCode ? 'Active' : 'Empty'}
                        </span>
                        ${managed === undefined
                            ? nothing
                            : html`<span class="slot-origin ${managed ? 'managed' : 'external'}">
                                  ${managed ? 'LCM' : 'Manual'}
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
        if (slot.code !== null && slot.code !== '') return '';
        if (slot.code_length) return 'masked';
        return 'no-code';
    }

    private _formatCode(slot: LockCoordinatorSlotData): string {
        if (slot.code !== null && slot.code !== '') {
            return String(slot.code);
        }
        if (slot.code_length) {
            return '•'.repeat(slot.code_length);
        }
        return 'No code';
    }

    private _renderEmptySummary(group: SlotGroup): TemplateResult {
        return html`<div class="empty-summary">
            <ha-icon icon="mdi:minus-circle-outline"></ha-icon>
            <span class="empty-summary-label">Empty slots</span>
            <span class="empty-summary-range">${group.rangeLabel}</span>
        </div>`;
    }

    private _getSummary():
        | {
              active: number;
              empty: number;
              total: number;
          }
        | undefined {
        const slots = this._data?.slots;
        if (!slots || slots.length === 0) {
            return undefined;
        }
        let active = 0;
        let empty = 0;
        for (const slot of slots) {
            if (this._hasCode(slot)) {
                active += 1;
            } else {
                empty += 1;
            }
        }
        return { active, empty, total: slots.length };
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
