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
const DEFAULT_CODE_DISPLAY: CodeDisplayMode = 'masked_with_reveal';

class LockCodeManagerLockDataCard extends LitElement {
    static styles = css`
        ha-card {
            padding: 12px 16px 16px;
        }

        .card-header {
            align-items: center;
            display: flex;
            font-size: 18px;
            font-weight: 500;
            gap: 8px;
            justify-content: space-between;
            margin-bottom: 12px;
        }

        .card-header ha-icon-button {
            --mdc-icon-button-size: 36px;
            --mdc-icon-size: 20px;
            color: var(--secondary-text-color);
        }

        table {
            border-collapse: collapse;
            width: 100%;
        }

        th,
        td {
            border-bottom: 1px solid var(--divider-color);
            padding: 6px 8px;
            text-align: left;
            vertical-align: top;
        }

        th {
            color: var(--secondary-text-color);
            font-size: 12px;
            font-weight: 600;
            letter-spacing: 0.02em;
            text-transform: uppercase;
        }

        tbody tr:last-child td {
            border-bottom: none;
        }

        .empty {
            color: var(--secondary-text-color);
            font-style: italic;
        }

        .code-masked {
            font-family: monospace;
            letter-spacing: 2px;
        }
    `;

    private _hass?: HomeAssistant;
    private _config?: LockCodeManagerLockDataCardConfig;
    private _data?: LockCoordinatorData;
    private _error?: string;
    private _revealed = false;
    private _unsub?: () => void;

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
        const title =
            this._config?.title ??
            this._data?.lock_name ??
            this._hass?.states[this._config?.lock_entity_id ?? '']?.attributes?.friendly_name ??
            DEFAULT_TITLE;
        const mode = this._config?.code_display ?? DEFAULT_CODE_DISPLAY;

        return html`
            <ha-card>
                <div class="card-header">
                    <span>${title}</span>
                    ${mode === 'masked_with_reveal'
                        ? html`<ha-icon-button
                              .path=${this._revealed ? mdiEyeOff : mdiEye}
                              @click=${this._toggleReveal}
                          ></ha-icon-button>`
                        : nothing}
                </div>
                ${this._error
                    ? html`<div class="empty">${this._error}</div>`
                    : html`<table>
                          <thead>
                              <tr>
                                  <th>Slot</th>
                                  <th>Code</th>
                              </tr>
                          </thead>
                          <tbody>
                              ${this._renderRows()}
                          </tbody>
                      </table>`}
            </ha-card>
        `;
    }

    private _toggleReveal(): void {
        this._revealed = !this._revealed;
        // Resubscribe with new reveal state to get updated data from backend
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
        // Request revealed data from backend when:
        // - mode is 'unmasked' (always show codes)
        // - mode is 'masked_with_reveal' AND user has toggled reveal on
        return mode === 'unmasked' || (mode === 'masked_with_reveal' && this._revealed);
    }

    private async _subscribe(): Promise<void> {
        if (!this._hass || !this._config || this._unsub) {
            return;
        }
        if (!this._hass.connection?.subscribeMessage) {
            this._error = 'Websocket connection unavailable';
            return;
        }

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
            this._error = err instanceof Error ? err.message : 'Failed to subscribe';
        }
    }

    private _renderRows(): TemplateResult {
        const slots = this._data?.slots ?? [];
        if (slots.length === 0) {
            return html`<tr>
                <td class="empty" colspan="2">No codes reported</td>
            </tr>`;
        }
        return html`${slots.map(
            (slot) =>
                html`<tr>
                    <td>${slot.slot}</td>
                    <td>${this._renderCode(slot)}</td>
                </tr>`
        )}`;
    }

    private _renderCode(slot: LockCoordinatorSlotData): TemplateResult {
        // Backend handles masking: code is present when revealed, null when masked
        if (slot.code !== null) {
            return html`${String(slot.code)}`;
        }

        // Code is masked - use code_length for bullet display
        if (slot.code_length) {
            return html`<span class="code-masked">${'•'.repeat(slot.code_length)}</span>`;
        }

        // No code in this slot
        return html``;
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
