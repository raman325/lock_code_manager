import { mdiAccountPlus, mdiEye, mdiEyeOff } from '@mdi/js';
import { LitElement, TemplateResult, css, html, nothing } from 'lit';
import { property, state } from 'lit/decorators.js';

import { CONDITION_DOMAINS, ensureEntityPickerLoaded, isConditionEntity } from './ha-components';
import { HomeAssistant } from './ha_type_stubs';
import { lcmCssVars, lcmDialogActionStyles, lcmRevealButtonStyles } from './shared-styles';
import { LockCodeManagerAddUserCardConfig } from './types';

/**
 * The button that adds a user to a Lock Code Manager config entry.
 *
 * Deliberately holds no state about the entry it belongs to: it collects a
 * name and an optional PIN, hands them to the `add_user` action, and
 * reloads. Allocation, capacity and slot numbering are the integration's
 * business, and asking the card to preview any of that would mean teaching
 * it rules that already live in one place.
 */
export class LockCodeManagerAddUserCard extends LitElement {
    static styles = [
        lcmCssVars,
        lcmDialogActionStyles,
        lcmRevealButtonStyles,
        css`
            ha-card {
                align-items: center;
                cursor: pointer;
                display: flex;
                gap: 12px;
                justify-content: center;
                padding: 20px 16px;
            }

            ha-card:hover {
                background: var(--lcm-section-bg-hover);
            }

            ha-card:focus-visible {
                outline: 2px solid var(--primary-color);
                outline-offset: 2px;
            }

            .label {
                color: var(--primary-text-color);
                font-size: 15px;
                font-weight: 500;
            }

            ha-svg-icon {
                color: var(--primary-color);
            }

            .dialog-content {
                display: flex;
                flex-direction: column;
                gap: 16px;
                min-width: 280px;
            }

            .dialog-description {
                color: var(--secondary-text-color);
                font-size: 13px;
                margin: 0;
            }

            .dialog-check {
                align-items: center;
                display: flex;
                gap: 10px;
            }

            .dialog-check input[type='checkbox'] {
                accent-color: var(--primary-color);
                height: 18px;
                margin: 0;
                width: 18px;
            }

            .field {
                display: flex;
                flex-direction: column;
                gap: 6px;
            }

            .field-label {
                color: var(--secondary-text-color);
                font-size: 11px;
                font-weight: 600;
                letter-spacing: 0.08em;
                text-transform: uppercase;
            }

            .field-help {
                color: var(--secondary-text-color);
                font-size: 12px;
                font-weight: 400;
                letter-spacing: normal;
                text-transform: none;
            }

            .field-control {
                align-items: center;
                display: flex;
                gap: 4px;
            }

            .field-control .field-input {
                flex: 1;
                min-width: 0;
            }

            .field-input {
                background: var(--card-background-color, #fff);
                border: 1px solid var(--lcm-border-color-strong);
                border-radius: 4px;
                box-sizing: border-box;
                color: var(--primary-text-color);
                font-family: inherit;
                font-size: 15px;
                padding: 10px 12px;
                width: 100%;
            }

            .field-input:focus {
                border-color: var(--primary-color);
                box-shadow: 0 0 0 1px var(--primary-color);
                outline: none;
            }

            .dialog-error {
                color: var(--lcm-error-color);
                font-size: 13px;
            }

            .dialog-saving {
                color: var(--secondary-text-color);
                font-size: 13px;
            }
        `
    ];

    @property({ attribute: false }) public hass?: HomeAssistant;

    @state() private _config?: LockCodeManagerAddUserCardConfig;

    @state() private _showDialog = false;

    @state() private _name = '';

    @state() private _pin = '';

    @state() private _enabled = true;

    @state() private _saving = false;

    @state() private _error?: string;

    @state() private _condition = '';

    @state() private _pickerReady = false;

    @state() private _pinVisible = false;

    static getStubConfig(): Partial<LockCodeManagerAddUserCardConfig> {
        return { type: 'custom:lcm-add-user' };
    }

    setConfig(config: LockCodeManagerAddUserCardConfig): void {
        if (!config.config_entry_id && !config.config_entry_title) {
            throw new Error('config_entry_id or config_entry_title is required');
        }
        this._config = config;
    }

    getCardSize(): number {
        return 1;
    }

    connectedCallback(): void {
        super.connectedCallback();
        void ensureEntityPickerLoaded().then((ready) => {
            this._pickerReady = ready;
        });
    }

    render(): TemplateResult {
        return html`
            <ha-card
                role="button"
                tabindex="0"
                aria-label="Add user"
                @click=${this._open}
                @keydown=${(e: KeyboardEvent) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        this._open();
                    }
                }}
            >
                <ha-svg-icon .path=${mdiAccountPlus}></ha-svg-icon>
                <span class="label">Add user</span>
            </ha-card>
            ${this._showDialog ? this._renderDialog() : nothing}
        `;
    }

    /** Seam so a test can observe the reload without navigating the runner. */
    protected _reload(): void {
        window.location.reload();
    }

    private _open(): void {
        this._name = '';
        this._pin = '';
        this._condition = '';
        this._pinVisible = false;
        this._enabled = true;
        this._error = undefined;
        this._showDialog = true;
    }

    private _close(): void {
        this._showDialog = false;
    }

    private _renderDialog(): TemplateResult {
        return html`
            <ha-dialog open @closed=${this._close} .heading=${'Add user'}>
                <div class="dialog-content">
                    <p class="dialog-description">
                        A slot is picked for you on every lock in this entry. Leave the PIN blank to
                        set one later.
                    </p>
                    <div class="field">
                        <label class="field-label" for="add-user-name">Name</label>
                        <div class="field-control">
                            <input
                                id="add-user-name"
                                class="field-input"
                                type="text"
                                required
                                .value=${this._name}
                                @input=${(e: Event) => {
                                    this._name = (e.target as HTMLInputElement).value;
                                }}
                            />
                        </div>
                    </div>
                    <div class="field">
                        <label class="field-label" for="add-user-pin">PIN</label>
                        <div class="field-control">
                            <input
                                id="add-user-pin"
                                class="field-input"
                                type=${this._pinVisible ? 'text' : 'password'}
                                inputmode="numeric"
                                .value=${this._pin}
                                @input=${(e: Event) => {
                                    this._pin = (e.target as HTMLInputElement).value;
                                }}
                            />
                            <ha-icon-button
                                class="lcm-reveal-button"
                                .path=${this._pinVisible ? mdiEyeOff : mdiEye}
                                .label=${this._pinVisible ? 'Hide PIN' : 'Show PIN'}
                                @click=${() => {
                                    this._pinVisible = !this._pinVisible;
                                }}
                            ></ha-icon-button>
                        </div>
                    </div>
                    ${this._renderConditionField()}
                    <label class="dialog-check">
                        <input
                            type="checkbox"
                            .checked=${this._enabled}
                            @change=${(e: Event) => {
                                this._enabled = (e.target as HTMLInputElement).checked;
                            }}
                        />
                        <span>Enabled</span>
                    </label>
                    ${
                        this._error
                            ? html`<div class="dialog-error" aria-live="polite">
                                  ${this._error}
                              </div>`
                            : nothing
                    }
                    ${
                        this._saving
                            ? html`<div class="dialog-saving" aria-live="polite">Adding…</div>`
                            : nothing
                    }
                    <div class="dialog-actions">
                        <button class="dialog-action" @click=${this._close}>Cancel</button>
                        <button
                            class="dialog-action"
                            .disabled=${this._saving}
                            @click=${this._commit}
                        >
                            Add
                        </button>
                    </div>
                </div>
            </ha-dialog>
        `;
    }

    /**
     * The condition field, or nothing at all.
     *
     * Home Assistant does not always hand a dashboard its entity picker,
     * and there is no honest substitute -- an entity id typed from memory
     * is a support ticket. When the picker is missing the field is left
     * out and the user card's own condition dialog covers it, one step
     * later, with the picker it force-loads for itself.
     */
    private _renderConditionField(): TemplateResult | typeof nothing {
        if (!this._pickerReady) return nothing;
        return html`
            <div class="field">
                <span class="field-label">Active only when</span>
                <ha-entity-picker
                    .hass=${this.hass}
                    .value=${this._condition}
                    .includeDomains=${CONDITION_DOMAINS}
                    .entityFilter=${(state: { entity_id: string }) =>
                        isConditionEntity(state.entity_id)}
                    @value-changed=${(e: CustomEvent) => {
                        this._condition = (e.detail?.value as string) || '';
                    }}
                ></ha-entity-picker>
                <span class="field-help">
                    Optional. The PIN works only while this entity is on.
                </span>
            </div>
        `;
    }

    private async _commit(): Promise<void> {
        const name = this._name.trim();
        if (!name) {
            this._error = 'Give the user a name';
            return;
        }
        if (!this.hass || !this._config) {
            this._error = 'Card not initialized';
            return;
        }
        if (this._saving) return;
        this._saving = true;
        this._error = undefined;
        try {
            await this.hass.callService('lock_code_manager', 'add_user', {
                enabled: this._enabled,
                name,
                // Either/or, never both: the action treats the two as
                // exclusive and refuses a call that carries the pair.
                ...(this._config.config_entry_id
                    ? { config_entry_id: this._config.config_entry_id }
                    : { config_entry_title: this._config.config_entry_title }),
                ...(this._pin && { pin: this._pin }),
                ...(this._condition && { condition: this._condition })
            });
        } catch (err) {
            this._error = err instanceof Error ? err.message : String(err);
            return;
        } finally {
            this._saving = false;
        }
        // The dashboard is generated by a strategy, so the new user has no
        // card until the strategy runs again. Nothing short of a reload
        // re-runs it.
        this._reload();
    }
}

customElements.define('lcm-add-user', LockCodeManagerAddUserCard);

declare global {
    interface HTMLElementTagNameMap {
        'lcm-add-user': LockCodeManagerAddUserCard;
    }
}

window.customCards = window.customCards || [];
window.customCards.push({
    description: 'Adds a user to a Lock Code Manager config entry',
    name: 'LCM Add User Card',
    preview: true,
    type: 'lcm-add-user'
});
