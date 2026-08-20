import { mdiAccountPlus } from '@mdi/js';
import { LitElement, TemplateResult, css, html, nothing } from 'lit';
import { property, state } from 'lit/decorators.js';

import { HomeAssistant } from './ha_type_stubs';
import { lcmCssVars } from './shared-styles';
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
                gap: 8px;
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
                    <ha-textfield
                        label="Name"
                        required
                        .value=${this._name}
                        @input=${(e: Event) => {
                            this._name = (e.target as HTMLInputElement).value;
                        }}
                    ></ha-textfield>
                    <ha-textfield
                        label="PIN"
                        type="password"
                        .value=${this._pin}
                        @input=${(e: Event) => {
                            this._pin = (e.target as HTMLInputElement).value;
                        }}
                    ></ha-textfield>
                    <label class="dialog-check">
                        <ha-checkbox
                            .checked=${this._enabled}
                            @change=${(e: Event) => {
                                this._enabled = (e.target as HTMLInputElement).checked;
                            }}
                        ></ha-checkbox>
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
                </div>
                <ha-button slot="secondaryAction" @click=${this._close}>Cancel</ha-button>
                <ha-button slot="primaryAction" .disabled=${this._saving} @click=${this._commit}>
                    Add
                </ha-button>
            </ha-dialog>
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
                ...(this._pin && { pin: this._pin })
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
