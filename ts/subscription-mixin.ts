/**
 * Mixin for LCM cards that use WebSocket subscriptions with reveal functionality.
 *
 * Provides common subscription lifecycle management and reveal toggle logic.
 */
import { MessageBase } from 'home-assistant-js-websocket';
import { LitElement } from 'lit';
import { state } from 'lit/decorators.js';

import { HomeAssistant } from './ha_type_stubs';
import { CodeDisplayMode } from './types';

// eslint-disable-next-line @typescript-eslint/no-explicit-any -- Mixin pattern requires generic constructor type
type Constructor<T = object> = new (...args: any[]) => T;

export interface LcmSubscriptionConfig {
    code_display?: CodeDisplayMode;
}

/**
 * Interface for classes using the subscription mixin.
 * Implementers must provide these members.
 */
export interface LcmSubscriptionHost {
    _config?: LcmSubscriptionConfig;
    _data?: unknown;
    _error?: string;
    _hass?: HomeAssistant;
}

/**
 * Mixin that adds WebSocket subscription management and reveal toggle functionality.
 *
 * Usage:
 * ```typescript
 * class MyCard extends LcmSubscriptionMixin(LitElement) {
 *   protected _getDefaultCodeDisplay(): CodeDisplayMode { return 'masked_with_reveal'; }
 *   protected _buildSubscribeMessage(): object { return { type: '...', ... }; }
 *   protected _handleSubscriptionData(data: MyDataType): void { this._data = data; }
 * }
 * ```
 */
export function LcmSubscriptionMixin<T extends Constructor<LitElement & LcmSubscriptionHost>>(
    Base: T
) {
    abstract class LcmSubscriptionClass extends Base {
        // Properties
        @state() protected _revealed = false;

        protected _unsub?: () => void;
        protected _subscribing = false;

        // Lifecycle overrides
        override connectedCallback(): void {
            super.connectedCallback();
            void this._subscribe();
        }

        override disconnectedCallback(): void {
            super.disconnectedCallback();
            this._unsubscribe();
        }

        // Protected methods
        /**
         * Format subscription error for display.
         * Override for card-specific error formatting.
         */
        protected _formatSubscriptionError(err: unknown): string {
            if (err instanceof Error) {
                return err.message;
            }
            if (typeof err === 'object' && err !== null && 'message' in err) {
                return String((err as { message: unknown }).message);
            }
            return `Failed to subscribe: ${JSON.stringify(err)}`;
        }

        protected _shouldReveal(): boolean {
            const mode = this._config?.code_display ?? this._getDefaultCodeDisplay();
            return mode === 'unmasked' || (mode === 'masked_with_reveal' && this._revealed);
        }

        protected async _subscribe(): Promise<void> {
            if (!this._hass || !this._config || this._unsub || this._subscribing) {
                return;
            }
            if (!this._hass.connection?.subscribeMessage) {
                this._error = 'Websocket connection unavailable';
                return;
            }

            this._subscribing = true;
            try {
                const message = this._buildSubscribeMessage();
                this._unsub = await this._hass.connection.subscribeMessage<unknown>((event) => {
                    this._handleSubscriptionData(event);
                    this._error = undefined;
                    this.requestUpdate();
                }, message);
            } catch (err) {
                this._data = undefined;
                this._error = this._formatSubscriptionError(err);
                this.requestUpdate();
            } finally {
                this._subscribing = false;
            }
        }

        protected _toggleReveal(): void {
            this._revealed = !this._revealed;
            this._unsubscribe();
            void this._subscribe();
        }

        protected _unsubscribe(): void {
            if (this._unsub) {
                this._unsub();
                this._unsub = undefined;
            }
        }

        // Abstract methods (must be implemented by subclasses)
        /**
         * Get the default code display mode for this card.
         * Override to provide card-specific default.
         */
        protected abstract _getDefaultCodeDisplay(): CodeDisplayMode;

        /**
         * Build the WebSocket subscription message.
         * Must include `type` and any card-specific parameters.
         */
        protected abstract _buildSubscribeMessage(): MessageBase;

        /**
         * Handle incoming subscription data.
         * Typically sets `this._data = data` and clears errors.
         */
        protected abstract _handleSubscriptionData(data: unknown): void;
    }

    return LcmSubscriptionClass;
}
