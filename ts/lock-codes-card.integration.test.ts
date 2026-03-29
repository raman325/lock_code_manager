import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { HomeAssistant } from './ha_type_stubs';
import { createMockHassWithConnection } from './test/mock-hass';
import { LockCoordinatorData } from './types';

/**
 * Integration tests for the LockCodesCard (lcm-lock-codes) component.
 *
 * These tests exercise the card's subscription lifecycle, configuration
 * validation, and data handling by mounting the actual component in jsdom.
 * Because jsdom does not fully support Lit's shadow Document Object Model
 * rendering, we focus on verifying state management and subscription
 * behavior through the component's properties rather than querying
 * rendered output.
 */

// Import the card module to trigger customElements.define
import './lock-codes-card';

/** Creates a LockCoordinatorData object with sensible defaults and optional overrides */
function makeLockCoordinatorData(
    overrides?: Partial<LockCoordinatorData>
): LockCoordinatorData {
    return {
        lock_entity_id: 'lock.test_1',
        lock_name: 'Test Lock',
        slots: [
            {
                active: true,
                code: '1234',
                config_entry_id: 'test-entry',
                enabled: true,
                managed: true,
                name: 'Test User',
                slot: 1,
            },
        ],
        ...overrides,
    };
}

/** Type alias for the lock codes card element with its internal properties exposed */
interface LockCodesCardElement extends HTMLElement {
    _config?: unknown;
    _data?: LockCoordinatorData;
    _error?: string;
    _hass?: HomeAssistant;
    setConfig(config: Record<string, unknown>): void;
}

describe('LockCodesCard integration', () => {
    let el: LockCodesCardElement;
    let container: HTMLDivElement;

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
        it('throws when lock_entity_id is missing', () => {
            el = document.createElement('lcm-lock-codes') as LockCodesCardElement;
            expect(() =>
                el.setConfig({ type: 'custom:lcm-lock-codes' })
            ).toThrow('lock_entity_id is required');
        });

        it('throws when lock_entity_id is empty', () => {
            el = document.createElement('lcm-lock-codes') as LockCodesCardElement;
            expect(() =>
                el.setConfig({ lock_entity_id: '', type: 'custom:lcm-lock-codes' })
            ).toThrow('lock_entity_id is required');
        });

        it('accepts valid config and stores it', () => {
            el = document.createElement('lcm-lock-codes') as LockCodesCardElement;
            el.setConfig({
                lock_entity_id: 'lock.front_door',
                type: 'custom:lcm-lock-codes',
            });
            expect(el._config).toBeDefined();
        });
    });

    describe('subscription connects with correct message', () => {
        it('builds subscribe message with lock_entity_id', async () => {
            el = document.createElement('lcm-lock-codes') as LockCodesCardElement;
            const hass = createMockHassWithConnection();
            el.setConfig({
                lock_entity_id: 'lock.front_door',
                type: 'custom:lcm-lock-codes',
            });
            el._hass = hass;

            container.appendChild(el);
            await flush();

            const subscribeMessage = hass.connection.subscribeMessage as ReturnType<typeof vi.fn>;
            expect(subscribeMessage).toHaveBeenCalled();

            const msg = subscribeMessage.mock.calls[0][1];
            expect(msg).toMatchObject({
                lock_entity_id: 'lock.front_door',
                type: 'lock_code_manager/subscribe_lock_codes',
            });
        });
    });

    describe('data handling', () => {
        it('stores subscription data in _data', async () => {
            let capturedCallback: ((data: unknown) => void) | undefined;
            el = document.createElement('lcm-lock-codes') as LockCodesCardElement;
            const hass = createMockHassWithConnection({
                onSubscribe: (callback) => {
                    capturedCallback = callback;
                },
            });
            el.setConfig({
                lock_entity_id: 'lock.front_door',
                type: 'custom:lcm-lock-codes',
            });
            el._hass = hass;

            container.appendChild(el);
            await flush();

            const testData = makeLockCoordinatorData();
            capturedCallback!(testData);

            expect(el._data).toEqual(testData);
        });

        it('handles data with multiple slots', async () => {
            let capturedCallback: ((data: unknown) => void) | undefined;
            el = document.createElement('lcm-lock-codes') as LockCodesCardElement;
            const hass = createMockHassWithConnection({
                onSubscribe: (callback) => {
                    capturedCallback = callback;
                },
            });
            el.setConfig({
                lock_entity_id: 'lock.front_door',
                type: 'custom:lcm-lock-codes',
            });
            el._hass = hass;

            container.appendChild(el);
            await flush();

            const testData = makeLockCoordinatorData({
                slots: [
                    { slot: 1, code: '1234', name: 'User A', managed: true, active: true, enabled: true },
                    { slot: 2, code: null, code_length: 6, name: 'User B', managed: true, active: false, enabled: true },
                    { slot: 3, code: null, name: undefined, managed: false },
                ],
            });
            capturedCallback!(testData);

            expect(el._data?.slots).toHaveLength(3);
            expect(el._data?.slots[0].code).toBe('1234');
            expect(el._data?.slots[1].code).toBeNull();
            expect(el._data?.slots[1].code_length).toBe(6);
            expect(el._data?.slots[2].managed).toBe(false);
        });
    });

    describe('error handling', () => {
        it('sets _error when subscription fails', async () => {
            el = document.createElement('lcm-lock-codes') as LockCodesCardElement;
            const hass = createMockHassWithConnection();
            (hass.connection.subscribeMessage as ReturnType<typeof vi.fn>).mockRejectedValue(
                new Error('Lock not found')
            );
            el.setConfig({
                lock_entity_id: 'lock.front_door',
                type: 'custom:lcm-lock-codes',
            });
            el._hass = hass;

            container.appendChild(el);
            await flush();

            expect(el._error).toBe('Lock not found');
        });

        it('clears _error when subscription data arrives', async () => {
            let capturedCallback: ((data: unknown) => void) | undefined;
            el = document.createElement('lcm-lock-codes') as LockCodesCardElement;
            const hass = createMockHassWithConnection({
                onSubscribe: (callback) => {
                    capturedCallback = callback;
                },
            });
            el.setConfig({
                lock_entity_id: 'lock.front_door',
                type: 'custom:lcm-lock-codes',
            });
            el._hass = hass;

            container.appendChild(el);
            await flush();

            // Manually set an error to simulate a prior failure
            el._error = 'Previous error';
            capturedCallback!(makeLockCoordinatorData());

            expect(el._error).toBeUndefined();
        });
    });
});
