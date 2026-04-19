import { mdiClose } from '@mdi/js';
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

        ha-input,
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

        .helper-entry {
            align-items: center;
            display: flex;
            justify-content: space-between;
        }

        .helper-entity-id {
            color: var(--primary-text-color);
            font-size: 13px;
            overflow: hidden;
            text-overflow: ellipsis;
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
                            html`<ha-list-item .value=${entry.entry_id}
                                >${entry.title}</ha-list-item
                            >`
                    )}
                </ha-select>
            </div>

            <div class="editor-row">
                <ha-input
                    .label=${'Slot Number'}
                    .value=${String(this._config.slot ?? '')}
                    type="number"
                    min="1"
                    max="9999"
                    @input=${this._slotChanged}
                ></ha-input>
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
                        (opt) => html`<ha-list-item .value=${opt.value}>${opt.label}</ha-list-item>`
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

            <div class="checkbox-row">
                <ha-checkbox
                    .checked=${this._config.show_lock_count !== false}
                    @change=${this._showLockCountChanged}
                ></ha-checkbox>
                <label @click=${this._toggleShowLockCount}>Lock Count</label>
            </div>

            <div class="section-label">Initially Expanded</div>

            <div class="checkbox-row">
                <ha-checkbox
                    .checked=${!(
                        this._config.collapsed_sections ?? ['conditions', 'lock_status']
                    ).includes('conditions')}
                    @change=${(e: Event) =>
                        this._toggleCollapsedSection(
                            'conditions',
                            !(e.target as HTMLInputElement).checked
                        )}
                ></ha-checkbox>
                <label>Conditions</label>
            </div>

            <div class="checkbox-row">
                <ha-checkbox
                    .checked=${!(
                        this._config.collapsed_sections ?? ['conditions', 'lock_status']
                    ).includes('lock_status')}
                    @change=${(e: Event) =>
                        this._toggleCollapsedSection(
                            'lock_status',
                            !(e.target as HTMLInputElement).checked
                        )}
                ></ha-checkbox>
                <label>Lock Status</label>
            </div>

            <div class="section-label">Condition Helper Entities</div>

            <div class="editor-row">
                <ha-entity-picker
                    .hass=${this._hass}
                    .value=${''}
                    .label=${'Add helper entity'}
                    .includeDomains=${[
                        'input_boolean',
                        'input_datetime',
                        'input_number',
                        'input_text',
                        'input_select',
                        'timer',
                        'counter'
                    ]}
                    @value-changed=${this._addConditionHelper}
                ></ha-entity-picker>
            </div>
            ${(this._config.condition_helpers ?? []).map(
                (eid: string, idx: number) => html`
                    <div class="editor-row helper-entry">
                        <span class="helper-entity-id">${eid}</span>
                        <ha-icon-button
                            .path=${mdiClose}
                            @click=${() => this._removeConditionHelper(idx)}
                        ></ha-icon-button>
                    </div>
                `
            )}
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

    private _showLockCountChanged(ev: Event): void {
        const target = ev.target as HTMLInputElement;
        this._updateConfig('show_lock_count', target.checked);
    }

    private _toggleCollapsedSection(
        section: 'conditions' | 'lock_status',
        collapsed: boolean
    ): void {
        const current = this._config?.collapsed_sections ?? ['conditions', 'lock_status'];
        const updated = collapsed
            ? current.includes(section)
                ? current
                : [...current, section]
            : current.filter((s: string) => s !== section);
        this._updateConfig('collapsed_sections', updated);
    }

    private _addConditionHelper(e: CustomEvent): void {
        const entityId = e.detail.value;
        if (!entityId) return;
        const current = this._config?.condition_helpers ?? [];
        if (current.includes(entityId)) return;
        this._updateConfig('condition_helpers', [...current, entityId]);
    }

    private _removeConditionHelper(idx: number): void {
        const current = [...(this._config?.condition_helpers ?? [])];
        current.splice(idx, 1);
        this._updateConfig('condition_helpers', current.length > 0 ? current : undefined);
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

    private _toggleShowLockCount(): void {
        this._updateConfig('show_lock_count', this._config?.show_lock_count === false);
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

customElements.define('lcm-slot-editor', LcmSlotCardEditor);
