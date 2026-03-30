/* eslint-disable no-underscore-dangle, prefer-destructuring */
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import { HomeAssistant } from './ha_type_stubs';
import { createMockHassWithConnection } from './test/mock-hass';
import { SlotCardData } from './types';

/**
 * Integration tests for the LockCodeManagerSlotCard (lcm-slot) component.
 *
 * These tests exercise the card's subscription lifecycle, configuration
 * validation, and data handling by mounting the actual component in jsdom.
 * Because jsdom does not fully support Lit's shadow Document Object Model
 * rendering, we focus on verifying state management and subscription
 * behavior through the component's properties rather than querying
 * rendered output.
 */

/** Creates a SlotCardData object with sensible defaults and optional overrides */
function makeSlotCardData(overrides?: Partial<SlotCardData>): SlotCardData {
    return {
        active: true,
        conditions: {},
        config_entry_id: 'test-entry',
        config_entry_title: 'Test Config',
        enabled: true,
        locks: [
            {
                code: '1234',
                entity_id: 'lock.test_1',
                in_sync: true,
                name: 'Test Lock'
            }
        ],
        name: 'Test User',
        pin: '1234',
        slot_num: 1,
        ...overrides
    };
}

/** Type alias for the slot card element with its internal properties exposed */
interface SlotCardElement extends HTMLElement {
    _config?: unknown;
    _data?: SlotCardData;
    _error?: string;
    _hass?: HomeAssistant;
    hass: HomeAssistant;
    setConfig(config: Record<string, unknown>): void;
}

describe('LockCodeManagerSlotCard integration', () => {
    let el: SlotCardElement;
    let container: HTMLDivElement;

    // Import the card module to trigger customElements.define, guarding against
    // re-definition if the module is reloaded in watch mode
    beforeAll(async () => {
        if (!customElements.get('lcm-slot')) {
            await import('./slot-card');
        }
    });

    beforeEach(() => {
        container = document.createElement('div');
        document.body.appendChild(container);
    });

    afterEach(() => {
        if (el && el.parentNode) {
            el.parentNode.removeChild(el);
        }
        container.remove();
    });

    /** Helper to flush microtasks so async operations complete */
    async function flush(): Promise<void> {
        await new Promise((r) => setTimeout(r, 0));
    }

    describe('config validation', () => {
        it('throws when config_entry_id and config_entry_title are both missing', () => {
            el = document.createElement('lcm-slot') as SlotCardElement;
            expect(() => el.setConfig({ slot: 1, type: 'custom:lcm-slot' })).toThrow(
                'config_entry_id or config_entry_title is required'
            );
        });

        it('throws when slot is missing', () => {
            el = document.createElement('lcm-slot') as SlotCardElement;
            expect(() => el.setConfig({ config_entry_id: 'abc', type: 'custom:lcm-slot' })).toThrow(
                'slot must be a number between 1 and 9999'
            );
        });

        it('throws when slot is out of range', () => {
            el = document.createElement('lcm-slot') as SlotCardElement;
            expect(() =>
                el.setConfig({ config_entry_id: 'abc', slot: 0, type: 'custom:lcm-slot' })
            ).toThrow('slot must be a number between 1 and 9999');
        });

        it('accepts valid config and stores it', () => {
            el = document.createElement('lcm-slot') as SlotCardElement;
            el.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            expect(el._config).toBeDefined();
        });
    });

    describe('subscription connects with correct message', () => {
        it('builds subscribe message with config_entry_id', async () => {
            el = document.createElement('lcm-slot') as SlotCardElement;
            const hass = createMockHassWithConnection();
            el.setConfig({ config_entry_id: 'my-entry', slot: 3, type: 'custom:lcm-slot' });
            el.hass = hass;

            container.appendChild(el);
            await flush();

            const subscribeMessage = hass.connection.subscribeMessage as ReturnType<typeof vi.fn>;
            expect(subscribeMessage).toHaveBeenCalled();

            // Verify the message passed to subscribeMessage
            const msg = subscribeMessage.mock.calls[0][1];
            expect(msg).toMatchObject({
                config_entry_id: 'my-entry',
                slot: 3,
                type: 'lock_code_manager/subscribe_code_slot'
            });
        });

        it('builds subscribe message with config_entry_title when no id', async () => {
            el = document.createElement('lcm-slot') as SlotCardElement;
            const hass = createMockHassWithConnection();
            el.setConfig({
                config_entry_title: 'My Lock Manager',
                slot: 2,
                type: 'custom:lcm-slot'
            });
            el.hass = hass;

            container.appendChild(el);
            await flush();

            const subscribeMessage = hass.connection.subscribeMessage as ReturnType<typeof vi.fn>;
            const msg = subscribeMessage.mock.calls[0][1];
            expect(msg).toMatchObject({
                config_entry_title: 'My Lock Manager',
                slot: 2,
                type: 'lock_code_manager/subscribe_code_slot'
            });
            expect(msg.config_entry_id).toBeUndefined();
        });
    });

    describe('data handling', () => {
        it('stores subscription data in _data', async () => {
            let capturedCallback: ((data: unknown) => void) | undefined;
            el = document.createElement('lcm-slot') as SlotCardElement;
            const hass = createMockHassWithConnection({
                onSubscribe: (callback) => {
                    capturedCallback = callback;
                }
            });
            el.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            el.hass = hass;

            container.appendChild(el);
            await flush();

            const testData = makeSlotCardData();
            capturedCallback!(testData);

            expect(el._data).toEqual(testData);
        });

        it('handles masked PIN data (pin is null with pin_length)', async () => {
            let capturedCallback: ((data: unknown) => void) | undefined;
            el = document.createElement('lcm-slot') as SlotCardElement;
            const hass = createMockHassWithConnection({
                onSubscribe: (callback) => {
                    capturedCallback = callback;
                }
            });
            el.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            el.hass = hass;

            container.appendChild(el);
            await flush();

            const maskedData = makeSlotCardData({ pin: null, pin_length: 4 });
            capturedCallback!(maskedData);

            expect(el._data?.pin).toBeNull();
            expect(el._data?.pin_length).toBe(4);
        });

        it('handles revealed PIN data', async () => {
            let capturedCallback: ((data: unknown) => void) | undefined;
            el = document.createElement('lcm-slot') as SlotCardElement;
            const hass = createMockHassWithConnection({
                onSubscribe: (callback) => {
                    capturedCallback = callback;
                }
            });
            el.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            el.hass = hass;

            container.appendChild(el);
            await flush();

            const revealedData = makeSlotCardData({ pin: '5678' });
            capturedCallback!(revealedData);

            expect(el._data?.pin).toBe('5678');
        });
    });

    describe('error handling', () => {
        it('sets _error when subscription fails', async () => {
            el = document.createElement('lcm-slot') as SlotCardElement;
            const hass = createMockHassWithConnection();
            (hass.connection.subscribeMessage as ReturnType<typeof vi.fn>).mockRejectedValue(
                new Error('Subscription failed')
            );
            el.setConfig({ config_entry_id: 'abc', slot: 1, type: 'custom:lcm-slot' });
            el.hass = hass;

            container.appendChild(el);
            await flush();

            expect(el._error).toBe('Subscription failed');
        });
    });
});
