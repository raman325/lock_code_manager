import { LitElement, TemplateResult, css, html, nothing } from 'lit';

import { HomeAssistant } from './ha_type_stubs';
import {
    CodeDisplayMode,
    GetLocksResponse,
    LockCodeManagerLockDataCardConfig,
    LockInfo
} from './types';

const CODE_DISPLAY_OPTIONS: Array<{ label: string; value: CodeDisplayMode }> = [
    { label: 'Masked with Reveal', value: 'masked_with_reveal' },
    { label: 'Always Masked', value: 'masked' },
    { label: 'Always Visible', value: 'unmasked' }
];

class LockCodeDataCardEditor extends LitElement {
    static styles = css`
        .editor-row {
            margin-bottom: 16px;
        }

        ha-entity-picker,
        ha-textfield,
        ha-select {
            display: block;
            width: 100%;
        }

        .no-locks-warning {
            color: var(--warning-color);
            padding: 8px;
        }
    `;

    private _hass?: HomeAssistant;
    private _config?: LockCodeManagerLockDataCardConfig;
    private _locks: LockInfo[] = [];
    private _loading = true;

    set hass(hass: HomeAssistant) {
        const hassChanged = this._hass !== hass;
        this._hass = hass;
        if (hassChanged) {
            void this._fetchLocks();
        }
    }

    setConfig(config: LockCodeManagerLockDataCardConfig): void {
        this._config = config;
    }

    protected render(): TemplateResult {
        if (!this._hass || !this._config) {
            return html``;
        }

        const lockEntityIds = this._locks.map((l) => l.entity_id);

        return html`
            <div class="editor-row">
                ${this._loading
                    ? html`<span>Loading locks...</span>`
                    : this._locks.length === 0
                      ? html`<div class="no-locks-warning">
                            No locks are currently managed by Lock Code Manager.
                        </div>`
                      : nothing}
                <ha-entity-picker
                    .hass=${this._hass}
                    .value=${this._config.lock_entity_id}
                    .includeEntities=${lockEntityIds}
                    .label=${'Lock'}
                    .required=${true}
                    @value-changed=${this._lockChanged}
                ></ha-entity-picker>
            </div>

            <div class="editor-row">
                <ha-textfield
                    .label=${'Title (optional)'}
                    .value=${this._config.title ?? ''}
                    @input=${this._titleChanged}
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
        `;
    }

    private async _fetchLocks(): Promise<void> {
        if (!this._hass) {
            return;
        }
        this._loading = true;
        try {
            const result = await this._hass.callWS<GetLocksResponse>({
                type: 'lock_code_manager/get_locks'
            });
            this._locks = result.locks;
        } catch {
            this._locks = [];
        }
        this._loading = false;
        this.requestUpdate();
    }

    private _lockChanged(ev: CustomEvent): void {
        if (!this._config) {
            return;
        }
        const { value } = ev.detail;
        if (value === this._config.lock_entity_id) {
            return;
        }
        this._config = { ...this._config, lock_entity_id: value };
        this._dispatchConfig();
    }

    private _titleChanged(ev: Event): void {
        if (!this._config) {
            return;
        }
        const target = ev.target as HTMLInputElement;
        const value = target.value || undefined;
        if (value === this._config.title) {
            return;
        }
        this._config = { ...this._config, title: value };
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

customElements.define('lock-code-data-card-editor', LockCodeDataCardEditor);
