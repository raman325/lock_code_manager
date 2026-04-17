/* eslint-disable no-underscore-dangle, @typescript-eslint/member-ordering, prefer-destructuring */
import { LitElement, html } from 'lit';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { HomeAssistant } from './ha_type_stubs';
import { LcmSubscriptionHost, LcmSubscriptionMixin } from './subscription-mixin';
import { createMockHassWithConnection } from './test/mock-hass';
import { CodeDisplayMode } from './types';

/**
 * Integration tests for the LcmSubscriptionMixin.
 *
 * These tests exercise the actual mixin lifecycle (connectedCallback,
 * disconnectedCallback, subscribe, unsubscribe) using a concrete test
 * subclass attached to the Document Object Model. Because jsdom does not
 * fully support Lit's shadow Document Object Model rendering, we verify
 * behavior through the component's public properties and mock interactions
 * rather than querying rendered output.
 */

// Concrete test subclass that implements the abstract methods
class TestSubscriptionElement
    extends LcmSubscriptionMixin(LitElement)
    implements LcmSubscriptionHost
{
    _config = { code_display: 'masked_with_reveal' as CodeDisplayMode };
    _hass?: HomeAssistant;
    _data?: unknown;
    _error?: string;

    protected _getDefaultCodeDisplay(): CodeDisplayMode {
        return 'masked_with_reveal';
    }

    protected _buildSubscribeMessage() {
        return { type: 'test/subscribe', id: 1 };
    }

    protected _handleSubscriptionData(data: unknown) {
        this._data = data;
    }

    render() {
        return html``;
    }
}

// Register the custom element once, guarding against re-definition in watch mode
if (!customElements.get('test-subscription-element')) {
    customElements.define('test-subscription-element', TestSubscriptionElement);
}

describe('LcmSubscriptionMixin integration', () => {
    let el: TestSubscriptionElement;
    let container: HTMLDivElement;

    beforeEach(() => {
        container = document.createElement('div');
        document.body.appendChild(container);
    });

    afterEach(() => {
        container.remove();
    });

    /** Helper to flush microtasks so async subscribe completes */
    async function flush(): Promise<void> {
        await new Promise((r) => setTimeout(r, 0));
    }

    it('connectedCallback triggers subscribe', async () => {
        el = document.createElement('test-subscription-element') as TestSubscriptionElement;
        el._hass = createMockHassWithConnection();
        container.appendChild(el);
        await flush();

        expect(el._hass.connection.subscribeMessage).toHaveBeenCalledOnce();
    });

    it('disconnectedCallback triggers unsubscribe', async () => {
        const unsubFn = vi.fn();
        el = document.createElement('test-subscription-element') as TestSubscriptionElement;
        el._hass = createMockHassWithConnection();
        (el._hass.connection.subscribeMessage as ReturnType<typeof vi.fn>).mockResolvedValue(
            unsubFn
        );
        container.appendChild(el);
        await flush();

        container.removeChild(el);
        expect(unsubFn).toHaveBeenCalledOnce();
    });

    it('reconnect after disconnect creates a new subscription', async () => {
        const unsubFn = vi.fn();
        el = document.createElement('test-subscription-element') as TestSubscriptionElement;
        el._hass = createMockHassWithConnection();
        (el._hass.connection.subscribeMessage as ReturnType<typeof vi.fn>).mockResolvedValue(
            unsubFn
        );

        // First connect
        container.appendChild(el);
        await flush();
        expect(el._hass.connection.subscribeMessage).toHaveBeenCalledTimes(1);

        // Disconnect
        container.removeChild(el);
        expect(unsubFn).toHaveBeenCalledTimes(1);

        // Reconnect
        container.appendChild(el);
        await flush();
        expect(el._hass.connection.subscribeMessage).toHaveBeenCalledTimes(2);
    });

    it('error during subscribe sets _error', async () => {
        el = document.createElement('test-subscription-element') as TestSubscriptionElement;
        el._hass = createMockHassWithConnection();
        (el._hass.connection.subscribeMessage as ReturnType<typeof vi.fn>).mockRejectedValue(
            new Error('Connection refused')
        );

        container.appendChild(el);
        await flush();

        expect(el._error).toBe('Connection refused');
        expect(el._data).toBeUndefined();
    });

    it('no double subscribe when already subscribed', async () => {
        el = document.createElement('test-subscription-element') as TestSubscriptionElement;
        el._hass = createMockHassWithConnection();
        container.appendChild(el);
        await flush();

        // Manually call _subscribe again while already subscribed
        await (el as unknown as { _subscribe: () => Promise<void> })._subscribe();
        await flush();

        // Should still only have one subscription call (guard prevents duplicate)
        expect(el._hass.connection.subscribeMessage).toHaveBeenCalledOnce();
    });

    it('toggleReveal unsubscribes and resubscribes', async () => {
        const unsubFn = vi.fn();
        el = document.createElement('test-subscription-element') as TestSubscriptionElement;
        el._hass = createMockHassWithConnection();
        (el._hass.connection.subscribeMessage as ReturnType<typeof vi.fn>).mockResolvedValue(
            unsubFn
        );

        container.appendChild(el);
        await flush();
        expect(el._hass.connection.subscribeMessage).toHaveBeenCalledTimes(1);

        // Toggle reveal - should unsubscribe the old one and create a new subscription
        (el as unknown as { _toggleReveal: () => void })._toggleReveal();
        await flush();

        expect(unsubFn).toHaveBeenCalledOnce();
        expect(el._hass.connection.subscribeMessage).toHaveBeenCalledTimes(2);
    });

    it('subscription callback sets _data and clears _error', async () => {
        let capturedCallback: ((data: unknown) => void) | undefined;
        el = document.createElement('test-subscription-element') as TestSubscriptionElement;
        el._hass = createMockHassWithConnection({
            onSubscribe: (callback) => {
                capturedCallback = callback;
            }
        });

        container.appendChild(el);
        await flush();

        expect(capturedCallback).toBeDefined();

        // Simulate receiving data
        const testData = { slot_num: 1, name: 'Test User' };
        capturedCallback!(testData);

        expect(el._data).toEqual(testData);
        expect(el._error).toBeUndefined();
    });

    it('sets error when connection is unavailable', async () => {
        el = document.createElement('test-subscription-element') as TestSubscriptionElement;
        // Create hass without connection (using basic createMockHass indirectly)
        el._hass = {
            callWS: vi.fn(),
            config: { state: 'RUNNING' },
            states: {}
        } as unknown as HomeAssistant;

        container.appendChild(el);
        await flush();

        expect(el._error).toBe('Websocket connection unavailable');
    });

    describe('_formatSubscriptionError', () => {
        it('returns message from Error instance', () => {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            expect((el as any)._formatSubscriptionError(new Error('test error'))).toBe(
                'test error'
            );
        });

        it('returns message from object with message property', () => {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            expect((el as any)._formatSubscriptionError({ message: 'obj error' })).toBe(
                'obj error'
            );
        });

        it('returns JSON for unknown error shapes', () => {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const result = (el as any)._formatSubscriptionError({ code: 42 });
            expect(result).toContain('Failed to subscribe');
            expect(result).toContain('42');
        });
    });
});
