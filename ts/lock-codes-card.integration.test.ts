/* eslint-disable no-underscore-dangle, prefer-destructuring */
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

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

/** Creates a LockCoordinatorData object with sensible defaults and optional overrides */
function makeLockCoordinatorData(overrides?: Partial<LockCoordinatorData>): LockCoordinatorData {
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
                slot: 1
            }
        ],
        ...overrides
    };
}

/** Type alias for the lock codes card element with its internal properties exposed */
interface LockCodesCardElement extends HTMLElement {
    _config?: unknown;
    _data?: LockCoordinatorData;
    _error?: string;
    _hass?: HomeAssistant;
    hass: HomeAssistant;
    setConfig(config: Record<string, unknown>): void;
}

describe('LockCodesCard integration', () => {
    let el: LockCodesCardElement;
    let container: HTMLDivElement;

    // Import the card module to trigger customElements.define, guarding against
    // re-definition if the module is reloaded in watch mode
    beforeAll(async () => {
        if (!customElements.get('lcm-lock-codes')) {
            await import('./lock-codes-card');
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
        it('throws when lock_entity_id is missing', () => {
            el = document.createElement('lcm-lock-codes') as LockCodesCardElement;
            expect(() => el.setConfig({ type: 'custom:lcm-lock-codes' })).toThrow(
                'lock_entity_id is required'
            );
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
                type: 'custom:lcm-lock-codes'
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
                type: 'custom:lcm-lock-codes'
            });
            el.hass = hass;

            container.appendChild(el);
            await flush();

            const subscribeMessage = hass.connection.subscribeMessage as ReturnType<typeof vi.fn>;
            expect(subscribeMessage).toHaveBeenCalled();

            const msg = subscribeMessage.mock.calls[0][1];
            expect(msg).toMatchObject({
                lock_entity_id: 'lock.front_door',
                type: 'lock_code_manager/subscribe_lock_codes'
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
                }
            });
            el.setConfig({
                lock_entity_id: 'lock.front_door',
                type: 'custom:lcm-lock-codes'
            });
            el.hass = hass;

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
                }
            });
            el.setConfig({
                lock_entity_id: 'lock.front_door',
                type: 'custom:lcm-lock-codes'
            });
            el.hass = hass;

            container.appendChild(el);
            await flush();

            const testData = makeLockCoordinatorData({
                slots: [
                    {
                        slot: 1,
                        code: '1234',
                        name: 'User A',
                        managed: true,
                        active: true,
                        enabled: true
                    },
                    {
                        slot: 2,
                        code: null,
                        code_length: 6,
                        name: 'User B',
                        managed: true,
                        active: false,
                        enabled: true
                    },
                    { slot: 3, code: null, name: undefined, managed: false }
                ]
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
                type: 'custom:lcm-lock-codes'
            });
            el.hass = hass;

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
                }
            });
            el.setConfig({
                lock_entity_id: 'lock.front_door',
                type: 'custom:lcm-lock-codes'
            });
            el.hass = hass;

            container.appendChild(el);
            await flush();

            // Manually set an error to simulate a prior failure
            el._error = 'Previous error';
            capturedCallback!(makeLockCoordinatorData());

            expect(el._error).toBeUndefined();
        });
    });

    describe('SlotCode sentinel handling', () => {
        let card: LockCodesCardElement & Record<string, unknown>;

        beforeEach(async () => {
            card = document.createElement('lcm-lock-codes') as LockCodesCardElement &
                Record<string, unknown>;
            card.setConfig({
                lock_entity_id: 'lock.front_door',
                type: 'custom:lcm-lock-codes'
            });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();
        });

        /* eslint-disable @typescript-eslint/no-explicit-any -- accessing private methods for testing */
        it('_hasCode returns false for "empty" sentinel', () => {
            expect((card as any)._hasCode({ slot: 1, code: 'empty' })).toBe(false);
        });

        it('_hasCode returns true for "unknown" sentinel', () => {
            expect((card as any)._hasCode({ slot: 1, code: 'unknown' })).toBe(true);
        });

        it('_hasCode returns true for code_length with null code', () => {
            expect((card as any)._hasCode({ slot: 1, code: null, code_length: 4 })).toBe(true);
        });

        it('_getCodeClass returns "no-code" for "empty" sentinel', () => {
            expect((card as any)._getCodeClass({ slot: 1, code: 'empty' })).toBe('no-code');
        });

        it('_getCodeClass returns "masked" for "unknown" sentinel', () => {
            expect((card as any)._getCodeClass({ slot: 1, code: 'unknown' })).toBe('masked');
        });

        it('_formatCode returns dash for "empty" sentinel', () => {
            expect((card as any)._formatCode({ slot: 1, code: 'empty' })).toBe('—');
        });

        it('_formatCode returns spaced bullets for "unknown" sentinel', () => {
            expect((card as any)._formatCode({ slot: 1, code: 'unknown' })).toBe('• • •');
        });
        it('_startEditing clears edit value for "empty" sentinel', () => {
            const mockEvent = { stopPropagation: () => {} };
            (card as any)._startEditing(mockEvent, { slot: 1, code: 'empty' });
            expect((card as any)._editValue).toBe('');
            expect((card as any)._editingSlot).toBe(1);
        });

        it('_startEditing clears edit value for "unknown" sentinel', () => {
            const mockEvent = { stopPropagation: () => {} };
            (card as any)._startEditing(mockEvent, { slot: 2, code: 'unknown' });
            expect((card as any)._editValue).toBe('');
        });

        it('_startEditing prefills edit value for regular code', () => {
            const mockEvent = { stopPropagation: () => {} };
            (card as any)._startEditing(mockEvent, { slot: 3, code: '9876' });
            expect((card as any)._editValue).toBe('9876');
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */

        it('stores "empty" and "unknown" codes in _data', async () => {
            let capturedCallback: ((data: unknown) => void) | undefined;
            const card2 = document.createElement('lcm-lock-codes') as LockCodesCardElement;
            const hass = createMockHassWithConnection({
                onSubscribe: (callback) => {
                    capturedCallback = callback;
                }
            });
            card2.setConfig({
                lock_entity_id: 'lock.front_door',
                type: 'custom:lcm-lock-codes'
            });
            card2.hass = hass;
            container.appendChild(card2);
            await flush();

            capturedCallback!(
                makeLockCoordinatorData({
                    slots: [
                        {
                            slot: 1,
                            code: '1234',
                            managed: true,
                            enabled: true,
                            active: true,
                            name: 'Active'
                        },
                        {
                            slot: 2,
                            code: 'unknown',
                            managed: true,
                            enabled: true,
                            active: true,
                            name: 'Masked'
                        },
                        { slot: 3, code: 'empty', managed: false }
                    ]
                })
            );

            expect(card2._data?.slots[0].code).toBe('1234');
            expect(card2._data?.slots[1].code).toBe('unknown');
            expect(card2._data?.slots[2].code).toBe('empty');
        });
    });

    describe('getStubConfig', () => {
        it('returns lock entity from first config entry when data exists', async () => {
            const LockCodesCard = customElements.get('lcm-lock-codes') as unknown as {
                getStubConfig(hass: HomeAssistant): Promise<Record<string, unknown>>;
            };
            const hass = createMockHassWithConnection();
            hass.callWS = vi
                .fn()
                .mockResolvedValueOnce([{ entry_id: 'entry-1' }])
                .mockResolvedValueOnce({
                    locks: [{ entity_id: 'lock.front_door' }]
                });

            const result = await LockCodesCard.getStubConfig(hass);
            expect(result).toEqual({
                lock_entity_id: 'lock.front_door',
                type: 'custom:lcm-lock-codes'
            });
        });

        it('returns stub config when no entries exist', async () => {
            const LockCodesCard = customElements.get('lcm-lock-codes') as unknown as {
                getStubConfig(hass: HomeAssistant): Promise<Record<string, unknown>>;
            };
            const hass = createMockHassWithConnection();
            hass.callWS = vi.fn().mockResolvedValue([]);

            const result = await LockCodesCard.getStubConfig(hass);
            expect(result).toEqual({
                lock_entity_id: 'lock.stub',
                type: 'custom:lcm-lock-codes'
            });
        });

        it('returns stub config when callWS throws', async () => {
            const LockCodesCard = customElements.get('lcm-lock-codes') as unknown as {
                getStubConfig(hass: HomeAssistant): Promise<Record<string, unknown>>;
            };
            const hass = createMockHassWithConnection();
            hass.callWS = vi.fn().mockRejectedValue(new Error('fail'));

            const result = await LockCodesCard.getStubConfig(hass);
            expect(result).toEqual({
                lock_entity_id: 'lock.stub',
                type: 'custom:lcm-lock-codes'
            });
        });

        it('returns stub config when entries exist but no locks', async () => {
            const LockCodesCard = customElements.get('lcm-lock-codes') as unknown as {
                getStubConfig(hass: HomeAssistant): Promise<Record<string, unknown>>;
            };
            const hass = createMockHassWithConnection();
            hass.callWS = vi
                .fn()
                .mockResolvedValueOnce([{ entry_id: 'entry-1' }])
                .mockResolvedValueOnce({ locks: [] });

            const result = await LockCodesCard.getStubConfig(hass);
            expect(result).toEqual({
                lock_entity_id: 'lock.stub',
                type: 'custom:lcm-lock-codes'
            });
        });
    });

    describe('stub config behavior', () => {
        it('sets _isStub to true when lock_entity_id is lock.stub', () => {
            el = document.createElement('lcm-lock-codes') as LockCodesCardElement;
            el.setConfig({
                lock_entity_id: 'lock.stub',
                type: 'custom:lcm-lock-codes'
            });
            expect((el as Record<string, unknown>)._isStub).toBe(true);
        });

        it('sets _isStub to false when lock_entity_id is real', () => {
            el = document.createElement('lcm-lock-codes') as LockCodesCardElement;
            el.setConfig({
                lock_entity_id: 'lock.front_door',
                type: 'custom:lcm-lock-codes'
            });
            expect((el as Record<string, unknown>)._isStub).toBe(false);
        });

        it('render returns static preview when _isStub is true', async () => {
            el = document.createElement('lcm-lock-codes') as LockCodesCardElement;
            el.setConfig({
                lock_entity_id: 'lock.stub',
                type: 'custom:lcm-lock-codes'
            });
            el.hass = createMockHassWithConnection();
            container.appendChild(el);
            await flush();

            /* eslint-disable @typescript-eslint/no-explicit-any */
            const result = (el as any).render();
            expect(result).toBeDefined();
            expect(result.strings?.join('')).toContain('Lock Code Manager Lock Codes');
            /* eslint-enable @typescript-eslint/no-explicit-any */
        });
    });

    describe('_saveCode set/clear usercode paths', () => {
        let card: LockCodesCardElement & Record<string, unknown>;
        let sendMessagePromiseMock: ReturnType<typeof vi.fn>;

        beforeEach(async () => {
            card = document.createElement('lcm-lock-codes') as LockCodesCardElement &
                Record<string, unknown>;
            card.setConfig({
                lock_entity_id: 'lock.front_door',
                type: 'custom:lcm-lock-codes'
            });
            sendMessagePromiseMock = vi.fn().mockResolvedValue({});
            const hass = createMockHassWithConnection();
            // Add sendMessagePromise to the connection mock
            (hass.connection as Record<string, unknown>).sendMessagePromise =
                sendMessagePromiseMock;
            card.hass = hass;
            container.appendChild(card);
            await flush();
        });

        /* eslint-disable @typescript-eslint/no-explicit-any */
        it('sends set_usercode when usercode is provided', async () => {
            (card as any)._editValue = '5678';
            (card as any)._saving = false;
            await (card as any)._saveCode(1);
            expect(sendMessagePromiseMock).toHaveBeenCalledWith(
                expect.objectContaining({
                    code_slot: 1,
                    lock_entity_id: 'lock.front_door',
                    type: 'lock_code_manager/set_usercode',
                    usercode: '5678'
                })
            );
        });

        it('sends clear_usercode when usercode is empty', async () => {
            (card as any)._editValue = '';
            (card as any)._saving = false;
            await (card as any)._saveCode(2);
            expect(sendMessagePromiseMock).toHaveBeenCalledWith(
                expect.objectContaining({
                    code_slot: 2,
                    lock_entity_id: 'lock.front_door',
                    type: 'lock_code_manager/clear_usercode'
                })
            );
        });

        it('sends clear_usercode when usercode is whitespace only', async () => {
            (card as any)._editValue = '   ';
            (card as any)._saving = false;
            await (card as any)._saveCode(3);
            expect(sendMessagePromiseMock).toHaveBeenCalledWith(
                expect.objectContaining({
                    code_slot: 3,
                    type: 'lock_code_manager/clear_usercode'
                })
            );
        });

        it('handles string slot numbers', async () => {
            (card as any)._editValue = '1234';
            (card as any)._saving = false;
            await (card as any)._saveCode('5');
            expect(sendMessagePromiseMock).toHaveBeenCalledWith(
                expect.objectContaining({
                    code_slot: 5,
                    type: 'lock_code_manager/set_usercode'
                })
            );
        });

        it('exits edit mode on success', async () => {
            (card as any)._editValue = '9999';
            (card as any)._editingSlot = 1;
            (card as any)._saving = false;
            await (card as any)._saveCode(1);
            expect((card as any)._editingSlot).toBeNull();
            expect((card as any)._editValue).toBe('');
            expect((card as any)._saving).toBe(false);
        });

        it('does not send when already saving', async () => {
            (card as any)._editValue = '1234';
            (card as any)._saving = true;
            await (card as any)._saveCode(1);
            expect(sendMessagePromiseMock).not.toHaveBeenCalled();
        });

        it('handles sendMessagePromise errors gracefully', async () => {
            (card as any)._editValue = '1234';
            (card as any)._saving = false;
            sendMessagePromiseMock.mockRejectedValueOnce(new Error('Network error'));
            const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
            await (card as any)._saveCode(1);
            expect(consoleSpy).toHaveBeenCalled();
            expect((card as any)._saving).toBe(false);
            consoleSpy.mockRestore();
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('_identifyBorrowedSlots', () => {
        /* eslint-disable @typescript-eslint/no-explicit-any */
        let card: LockCodesCardElement & Record<string, unknown>;

        beforeEach(async () => {
            card = document.createElement('lcm-lock-codes') as LockCodesCardElement &
                Record<string, unknown>;
            card.setConfig({ lock_entity_id: 'lock.test_1', type: 'custom:lcm-lock-codes' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();
        });

        it('borrows from next empty group for odd lone active slot', () => {
            const groups = [
                { slots: [{ slot: 1, code: '1234', managed: true }], type: 'active' as const },
                {
                    slots: [
                        { slot: 2, code: null, managed: false },
                        { slot: 3, code: null, managed: false }
                    ],
                    type: 'empty' as const
                }
            ];
            const borrowed = (card as any)._identifyBorrowedSlots(groups);
            expect(borrowed.has(2)).toBe(true);
        });

        it('borrows from prev empty group for even lone active slot', () => {
            const groups = [
                {
                    slots: [
                        { slot: 1, code: null, managed: false },
                        { slot: 2, code: null, managed: false }
                    ],
                    type: 'empty' as const
                },
                { slots: [{ slot: 4, code: '5678', managed: true }], type: 'active' as const }
            ];
            const borrowed = (card as any)._identifyBorrowedSlots(groups);
            expect(borrowed.has(2)).toBe(true);
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('_renderCodeEditMode and _renderCodeSection dispatch', () => {
        /* eslint-disable @typescript-eslint/no-explicit-any */
        let card: LockCodesCardElement & Record<string, unknown>;

        beforeEach(async () => {
            card = document.createElement('lcm-lock-codes') as LockCodesCardElement &
                Record<string, unknown>;
            card.setConfig({ lock_entity_id: 'lock.test_1', type: 'custom:lcm-lock-codes' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();
        });

        it('renders edit input template via _renderCodeEditMode', () => {
            (card as any)._editValue = '9999';
            (card as any)._saving = false;
            const slot = { slot: 1, code: '1234', managed: false };
            const result = (card as any)._renderCodeEditMode(slot);
            expect(result).toBeDefined();
            const strings = result.strings?.join('') ?? '';
            expect(strings).toContain('slot-code-edit');
            expect(strings).toContain('slot-code-input');
        });

        it('_renderCodeSection dispatches to edit mode when editing unmanaged slot', () => {
            (card as any)._editingSlot = 1;
            (card as any)._editValue = '9999';
            (card as any)._saving = false;
            const slot = { slot: 1, code: '1234', managed: false };
            const result = (card as any)._renderCodeSection(slot, true, 'masked_with_reveal');
            expect(result).toBeDefined();
            const strings = result.strings?.join('') ?? '';
            expect(strings).toContain('slot-code-edit');
        });

        it('edit mode stopPropagation wrapper is called', () => {
            (card as any)._editValue = '9999';
            (card as any)._saving = false;
            const slot = { slot: 1, code: '1234', managed: false };
            const result = (card as any)._renderCodeEditMode(slot);
            // The first value in the template is the stopPropagation handler
            const stopPropHandler = result.values?.find((v: unknown) => typeof v === 'function');
            expect(stopPropHandler).toBeDefined();
            const mockEvent = { stopPropagation: vi.fn() };
            stopPropHandler(mockEvent);
            expect(mockEvent.stopPropagation).toHaveBeenCalled();
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });
});
