import { LitElement, TemplateResult, css, html, nothing } from 'lit';

import { HomeAssistant } from './ha_type_stubs';
import {
    CodeDisplayMode,
    ConfigEntryJSONFragment,
    GetConfigEntriesResponse,
    LockCodeManagerSlotCardConfig
} from './types';

const CODE_DISPLAY_OPTIONS: Array<{ label: string; value: CodeDisplayMode }> = [
    { label: 'Masked with Reveal', value: 'masked_with_reveal' },
    { label: 'Always Masked', value: 'masked' },
    { label: 'Always Visible', value: 'unmasked' }
];

class LcmSlotCardEditor extends LitElement {
    static styles = css`
        .editor-row {
            margin-bottom: 16px;
        }

        ha-textfield,
        ha-select {
            display: block;
            width: 100%;
        }

        .no-entries-warning {
            color: var(--warning-color);
            padding: 8px;
        }

        .checkbox-row {
            align-items: center;
            display: flex;
            gap: 8px;
            margin-bottom: 8px;
        }

        .checkbox-row label {
            cursor: pointer;
        }

        .section-label {
            color: var(--secondary-text-color);
            font-size: 12px;
            font-weight: 500;
            letter-spacing: 0.05em;
            margin-bottom: 8px;
            margin-top: 16px;
            text-transform: uppercase;
        }
    `;

    private _hass?: HomeAssistant;
    private _config?: LockCodeManagerSlotCardConfig;
    private _configEntries: ConfigEntryJSONFragment[] = [];
    private _loading = true;

    set hass(hass: HomeAssistant) {
        const hassChanged = this._hass !== hass;
        this._hass = hass;
        if (hassChanged) {
            void this._fetchConfigEntries();
        }
    }

    setConfig(config: LockCodeManagerSlotCardConfig): void {
        this._config = config;
    }

    protected render(): TemplateResult {
        if (!this._hass || !this._config) {
            return html``;
        }

        return html`
            <div class="editor-row">
                ${this._loading
                    ? html`<span>Loading config entries...</span>`
                    : this._configEntries.length === 0
                      ? html`<div class="no-entries-warning">
                            No Lock Code Manager config entries found.
                        </div>`
                      : nothing}
                <ha-select
                    .label=${'LCM Config Entry'}
                    .value=${this._config.config_entry_id ?? ''}
                    @selected=${this._configEntryChanged}
                    @closed=${this._stopPropagation}
                    fixedMenuPosition
                    naturalMenuWidth
                >
                    ${this._configEntries.map(
                        (entry) =>
                            html`<mwc-list-item .value=${entry.entry_id}
                                >${entry.title}</mwc-list-item
                            >`
                    )}
                </ha-select>
            </div>

            <div class="editor-row">
                <ha-textfield
                    .label=${'Slot Number'}
                    .value=${String(this._config.slot ?? '')}
                    type="number"
                    min="1"
                    max="9999"
                    @input=${this._slotChanged}
                ></ha-textfield>
            </div>

            <div class="editor-row">
                <ha-select
                    .label=${'Code Display'}
                    .value=${this._config.code_display ?? 'masked_with_reveal'}
                    @selected=${this._displayModeChanged}
                    @closed=${this._stopPropagation}
                    fixedMenuPosition
                    naturalMenuWidth
                >
                    ${CODE_DISPLAY_OPTIONS.map(
                        (opt) =>
                            html`<mwc-list-item .value=${opt.value}>${opt.label}</mwc-list-item>`
                    )}
                </ha-select>
            </div>

            <div class="section-label">Show Sections</div>

            <div class="checkbox-row">
                <ha-checkbox
                    .checked=${this._config.show_conditions !== false}
                    @change=${this._showConditionsChanged}
                ></ha-checkbox>
                <label @click=${this._toggleShowConditions}>Conditions</label>
            </div>

            <div class="checkbox-row">
                <ha-checkbox
                    .checked=${this._config.show_lock_status !== false}
                    @change=${this._showLockStatusChanged}
                ></ha-checkbox>
                <label @click=${this._toggleShowLockStatus}>Lock Status</label>
            </div>

            <div class="checkbox-row">
                <ha-checkbox
                    .checked=${this._config.show_code_sensors !== false}
                    @change=${this._showCodeSensorsChanged}
                ></ha-checkbox>
                <label @click=${this._toggleShowCodeSensors}>Code Sensors in Lock Status</label>
            </div>

            <div class="checkbox-row">
                <ha-checkbox
                    .checked=${this._config.show_lock_sync !== false}
                    @change=${this._showLockSyncChanged}
                ></ha-checkbox>
                <label @click=${this._toggleShowLockSync}>Sync Status in Lock Status</label>
            </div>
        `;
    }

    private async _fetchConfigEntries(): Promise<void> {
        if (!this._hass) {
            return;
        }
        this._loading = true;
        try {
            const result = await this._hass.callWS<GetConfigEntriesResponse>({
                domain: 'lock_code_manager',
                type: 'config_entries/get'
            });
            this._configEntries = result.filter((e) => e.state === 'loaded');
        } catch {
            this._configEntries = [];
        }
        this._loading = false;
        this.requestUpdate();
    }

    private _configEntryChanged(ev: CustomEvent): void {
        if (!this._config) {
            return;
        }
        const target = ev.target as HTMLSelectElement;
        const { value } = target;
        if (value === this._config.config_entry_id) {
            return;
        }
        // Clear config_entry_title when using config_entry_id
        const { config_entry_title: _, ...rest } = this._config;
        this._config = { ...rest, config_entry_id: value };
        this._dispatchConfig();
    }

    private _slotChanged(ev: Event): void {
        if (!this._config) {
            return;
        }
        const target = ev.target as HTMLInputElement;
        const value = parseInt(target.value, 10);
        if (isNaN(value) || value === this._config.slot) {
            return;
        }
        this._config = { ...this._config, slot: value };
        this._dispatchConfig();
    }

    private _displayModeChanged(ev: CustomEvent): void {
        if (!this._config) {
            return;
        }
        const target = ev.target as HTMLSelectElement;
        const value = target.value as CodeDisplayMode;
        if (value === this._config.code_display) {
            return;
        }
        this._config = { ...this._config, code_display: value };
        this._dispatchConfig();
    }

    private _showConditionsChanged(ev: Event): void {
        const target = ev.target as HTMLInputElement;
        this._updateConfig('show_conditions', target.checked);
    }

    private _showLockStatusChanged(ev: Event): void {
        const target = ev.target as HTMLInputElement;
        this._updateConfig('show_lock_status', target.checked);
    }

    private _showCodeSensorsChanged(ev: Event): void {
        const target = ev.target as HTMLInputElement;
        this._updateConfig('show_code_sensors', target.checked);
    }

    private _showLockSyncChanged(ev: Event): void {
        const target = ev.target as HTMLInputElement;
        this._updateConfig('show_lock_sync', target.checked);
    }

    private _toggleShowConditions(): void {
        this._updateConfig('show_conditions', this._config?.show_conditions === false);
    }

    private _toggleShowLockStatus(): void {
        this._updateConfig('show_lock_status', this._config?.show_lock_status === false);
    }

    private _toggleShowCodeSensors(): void {
        this._updateConfig('show_code_sensors', this._config?.show_code_sensors === false);
    }

    private _toggleShowLockSync(): void {
        this._updateConfig('show_lock_sync', this._config?.show_lock_sync === false);
    }

    private _updateConfig<K extends keyof LockCodeManagerSlotCardConfig>(
        key: K,
        value: LockCodeManagerSlotCardConfig[K]
    ): void {
        if (!this._config) {
            return;
        }
        this._config = { ...this._config, [key]: value };
        this._dispatchConfig();
    }

    private _stopPropagation(ev: Event): void {
        ev.stopPropagation();
    }

    private _dispatchConfig(): void {
        this.dispatchEvent(
            new CustomEvent('config-changed', {
                bubbles: true,
                composed: true,
                detail: { config: this._config }
            })
        );
    }
}

customElements.define('lcm-slot-card-editor', LcmSlotCardEditor);
