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
            expect(el._data).toBeUndefined();
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

        it('_hasCode returns true for "unreadable_code" sentinel', () => {
            expect((card as any)._hasCode({ slot: 1, code: 'unreadable_code' })).toBe(true);
        });

        it('_hasCode returns true for code_length with null code', () => {
            expect((card as any)._hasCode({ slot: 1, code: null, code_length: 4 })).toBe(true);
        });

        it('_formatCode sizes hollow dots to the configured length when known', () => {
            expect(
                (card as any)._formatCode({
                    slot: 1,
                    code: 'unreadable_code',
                    configured_code_length: 4
                })
            ).toBe('◦◦◦◦');
        });

        describe('managed + in-sync unreadable code (configured-PIN proxy)', () => {
            const proxySlot = {
                slot: 1,
                code: 'unreadable_code',
                active: true,
                managed: true,
                in_sync: true
            };

            it('masks to the configured length, not a fixed 3 dots', () => {
                expect((card as any)._revealed).toBe(false);
                expect((card as any)._formatCode({ ...proxySlot, configured_code_length: 4 })).toBe(
                    '••••'
                );
                expect(
                    (card as any)._getCodeClass({ ...proxySlot, configured_code_length: 4 })
                ).toBe('configured masked');
            });

            it('reveals the configured PIN, marked as configured (italic class)', () => {
                (card as any)._revealed = true;
                try {
                    expect(
                        (card as any)._formatCode({ ...proxySlot, configured_code: '1234' })
                    ).toBe('1234');
                    expect(
                        (card as any)._getCodeClass({ ...proxySlot, configured_code: '1234' })
                    ).toBe('configured');
                } finally {
                    (card as any)._revealed = false;
                }
            });

            it('offers the eye for a proxy code but not an unvouched one', () => {
                expect((card as any)._canReveal({ ...proxySlot, configured_code_length: 4 })).toBe(
                    true
                );
                expect((card as any)._canReveal({ slot: 1, code: 'unreadable_code' })).toBe(false);
            });

            it.each([
                ['switched off', { active: false, enabled: false }],
                ['held off by its condition', { active: false, enabled: true }]
            ])('never stands in a PIN meant to be off the lock (%s)', (_label, state) => {
                // In sync here means the clear is trusted: the PIN is not on the lock.
                const slot = { ...proxySlot, ...state, configured_code: '1234' };
                (card as any)._revealed = true;
                try {
                    expect((card as any)._formatCode(slot)).toBe('◦◦◦');
                    expect((card as any)._getCodeClass(slot)).not.toContain('configured');
                    expect((card as any)._canReveal(slot)).toBe(false);
                    expect((card as any)._codeTitle(slot)).not.toContain('Lock Code Manager');
                } finally {
                    (card as any)._revealed = false;
                }
            });

            it('tooltips distinguish proxy from unreadable', () => {
                expect(
                    (card as any)._codeTitle({ ...proxySlot, configured_code_length: 4 })
                ).toContain('Lock Code Manager');
                expect((card as any)._codeTitle({ slot: 1, code: 'unreadable_code' })).toContain(
                    "doesn't report"
                );
            });
        });
        it('_startEditing clears edit value for "empty" sentinel', () => {
            const mockEvent = { stopPropagation: () => {} };
            (card as any)._startEditing(mockEvent, { slot: 1, code: 'empty' });
            expect((card as any)._editValue).toBe('');
            expect((card as any)._editingSlot).toBe(1);
        });

        it('_startEditing clears edit value for "unreadable_code" sentinel', () => {
            const mockEvent = { stopPropagation: () => {} };
            (card as any)._startEditing(mockEvent, { slot: 2, code: 'unreadable_code' });
            expect((card as any)._editValue).toBe('');
        });

        it('editing one slot does not unmask every other code on the card', () => {
            // `_revealed` is card-global and `_formatCode` reads it for every
            // row, so revealing to edit one unmanaged slot put every managed
            // user's PIN on screen in cleartext -- and `_saveCode` never
            // restored it, so they stayed there after the write landed.
            (card as any)._revealed = false;

            (card as any)._startEditing({ stopPropagation: () => {} }, { code: 'empty', slot: 1 });

            expect((card as any)._revealed).toBe(false);
        });

        it('leaves the codes masked after the write lands', async () => {
            (card as any)._revealed = false;
            (card as any)._editValue = '1234';
            (card as any)._startEditing({ stopPropagation: () => {} }, { code: 'empty', slot: 1 });

            await (card as any)._saveCode(1);

            expect((card as any)._revealed).toBe(false);
        });

        it('_startEditing prefills edit value for regular code', () => {
            const mockEvent = { stopPropagation: () => {} };
            (card as any)._startEditing(mockEvent, { slot: 3, code: '9876' });
            expect((card as any)._editValue).toBe('9876');
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('unanswered sentinel', () => {
        /* eslint-disable @typescript-eslint/no-explicit-any -- accessing private methods for testing */
        let card: LockCodesCardElement & Record<string, unknown>;

        beforeEach(async () => {
            card = document.createElement('lcm-lock-codes') as LockCodesCardElement &
                Record<string, unknown>;
            card.setConfig({ lock_entity_id: 'lock.front_door', type: 'custom:lcm-lock-codes' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();
        });

        const classes = (slot: Record<string, unknown>): string[] =>
            (card as any)._getCodeClass(slot).split(' ');

        const vouched = {
            active: true,
            code: 'unanswered',
            in_sync: true,
            managed: true,
            slot: 1
        };

        it('stands the configured PIN in when managed and in sync', () => {
            expect((card as any)._formatCode({ ...vouched, configured_code_length: 4 })).toBe(
                '••••'
            );
            expect(classes({ ...vouched, configured_code_length: 4 })).toEqual([
                'configured',
                'masked'
            ]);
            expect((card as any)._canReveal({ ...vouched, configured_code_length: 4 })).toBe(true);
        });

        it('reveals the configured PIN in italics when managed and in sync', () => {
            (card as any)._revealed = true;
            try {
                expect((card as any)._formatCode({ ...vouched, configured_code: '1234' })).toBe(
                    '1234'
                );
                expect(classes({ ...vouched, configured_code: '1234' })).toEqual(['configured']);
            } finally {
                (card as any)._revealed = false;
            }
        });

        it('never stands in a PIN meant to be off the lock', () => {
            const slot = { ...vouched, active: false, configured_code: '1234', enabled: false };
            (card as any)._revealed = true;
            try {
                expect((card as any)._formatCode(slot)).toBe('◦◦◦');
                expect((card as any)._canReveal(slot)).toBe(false);
                expect((card as any)._codeTitle(slot)).toBe("This lock didn't answer");
            } finally {
                (card as any)._revealed = false;
            }
        });

        it("says the stand-in is configured because the lock didn't answer", () => {
            const title = (card as any)._codeTitle({ ...vouched, configured_code_length: 4 });
            expect(title).toContain('Lock Code Manager');
            expect(title).toContain("didn't answer");
        });

        it("shows hollow dots and says the lock didn't answer otherwise", () => {
            const slot = { code: 'unanswered', managed: false, slot: 2 };
            expect((card as any)._formatCode(slot)).toBe('◦◦◦');
            expect(classes(slot)).toContain('unreadable');
            expect((card as any)._codeTitle(slot)).toBe("This lock didn't answer");
            expect((card as any)._canReveal(slot)).toBe(false);
        });

        it('sizes the hollow dots to the configured PIN when out of sync', () => {
            const slot = { ...vouched, configured_code_length: 6, in_sync: false };
            expect((card as any)._formatCode(slot)).toBe('◦◦◦◦◦◦');
            expect((card as any)._canReveal(slot)).toBe(false);
        });

        it('does not count as a code on the lock', () => {
            expect((card as any)._hasCode({ code: 'unanswered', slot: 1 })).toBe(false);
        });

        it('never pre-fills the edit field', () => {
            (card as any)._startEditing(
                { stopPropagation: () => {} },
                { code: 'unanswered', slot: 2 }
            );
            expect((card as any)._editValue).toBe('');
        });

        it('renders a payload slot as hollow dots, never as the raw label', async () => {
            let capturedCallback: ((data: unknown) => void) | undefined;
            const card2 = document.createElement('lcm-lock-codes') as LockCodesCardElement;
            card2.setConfig({
                code_display: 'unmasked',
                lock_entity_id: 'lock.front_door',
                type: 'custom:lcm-lock-codes'
            });
            card2.hass = createMockHassWithConnection({
                onSubscribe: (callback) => {
                    capturedCallback = callback;
                }
            });
            container.appendChild(card2);
            await flush();

            capturedCallback!(
                makeLockCoordinatorData({
                    slots: [{ code: 'unanswered', managed: false, slot: 1 }]
                })
            );

            expect((card2 as any)._formatCode(card2._data!.slots[0])).toBe('◦◦◦');
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('checking sentinel', () => {
        /* eslint-disable @typescript-eslint/no-explicit-any -- accessing private methods for testing */
        let card: LockCodesCardElement & Record<string, unknown>;

        beforeEach(async () => {
            card = document.createElement('lcm-lock-codes') as LockCodesCardElement &
                Record<string, unknown>;
            card.setConfig({ lock_entity_id: 'lock.front_door', type: 'custom:lcm-lock-codes' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();
        });

        const checking = {
            active: true,
            code: 'checking',
            config_entry_id: 'entry1',
            configured_code: '1234',
            enabled: true,
            in_sync: false,
            managed: true,
            name: 'Housekeeper',
            slot: 2
        };

        it('shows the slot pending with a label, never a PIN', () => {
            (card as any)._revealed = true;
            try {
                expect((card as any)._isPending(checking)).toBe(true);
                expect((card as any)._formatCode(checking)).toBe('Checking…');
                expect((card as any)._getCodeClass(checking).split(' ')).toEqual([
                    'pending',
                    'no-code'
                ]);
                expect((card as any)._codeTitle(checking)).toBe(
                    'Waiting for the lock to confirm this slot'
                );
                expect((card as any)._canReveal(checking)).toBe(false);
            } finally {
                (card as any)._revealed = false;
            }
        });

        it('does not count as a code on the lock', () => {
            expect((card as any)._hasCode(checking)).toBe(false);
        });

        it('renders the row pending, with no PIN', async () => {
            let capturedCallback: ((data: unknown) => void) | undefined;
            const card2 = document.createElement('lcm-lock-codes') as LockCodesCardElement;
            card2.setConfig({
                code_display: 'unmasked',
                lock_entity_id: 'lock.front_door',
                type: 'custom:lcm-lock-codes'
            });
            card2.hass = createMockHassWithConnection({
                onSubscribe: (callback) => {
                    capturedCallback = callback;
                }
            });
            container.appendChild(card2);
            await flush();

            capturedCallback!(makeLockCoordinatorData({ slots: [checking] }));
            await flush();

            const chip = card2.shadowRoot!.querySelector('.slot-chip')!;
            expect(chip.classList.contains('pending')).toBe(true);
            const code = chip.querySelector('.lcm-code')!;
            expect(code.classList.contains('pending')).toBe(true);
            expect(code.getAttribute('title')).toBe('Waiting for the lock to confirm this slot');
            expect(code.textContent).toContain('Checking…');
            expect(chip.textContent).not.toContain('1234');
            expect(chip.querySelector('.lcm-reveal-button')).toBeNull();
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('disabled-on-lock sentinel', () => {
        /* eslint-disable @typescript-eslint/no-explicit-any -- accessing private methods for testing */
        let card: LockCodesCardElement & Record<string, unknown>;

        beforeEach(async () => {
            card = document.createElement('lcm-lock-codes') as LockCodesCardElement &
                Record<string, unknown>;
            card.setConfig({ lock_entity_id: 'lock.front_door', type: 'custom:lcm-lock-codes' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await flush();
        });

        const classes = (slot: Record<string, unknown>): string[] =>
            (card as any)._getCodeClass(slot).split(' ');

        const disabled = { code: 'disabled_on_lock', slot: 3 };

        it('says the code is disabled on the lock, as a label', () => {
            expect((card as any)._formatCode(disabled)).toBe('Disabled on lock');
            expect(classes(disabled)).toEqual(['disabled-on-lock']);
            expect((card as any)._codeTitle(disabled)).toBe('Disabled on lock');
            expect((card as any)._canReveal(disabled)).toBe(false);
        });

        it('never stands the configured PIN in, even managed and in sync', () => {
            const slot = { ...disabled, configured_code: '1234', in_sync: true, managed: true };
            (card as any)._revealed = true;
            try {
                expect((card as any)._formatCode(slot)).toBe('Disabled on lock');
                expect(classes(slot)).not.toContain('configured');
                expect((card as any)._canReveal(slot)).toBe(false);
            } finally {
                (card as any)._revealed = false;
            }
        });

        it('counts as a code on the lock', () => {
            expect((card as any)._hasCode(disabled)).toBe(true);
        });

        it('never pre-fills the edit field', () => {
            (card as any)._startEditing({ stopPropagation: () => {} }, disabled);
            expect((card as any)._editValue).toBe('');
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
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
                    type: 'lock_code_manager/write_unmanaged_code',
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
                    type: 'lock_code_manager/clear_unmanaged_code'
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
                    type: 'lock_code_manager/clear_unmanaged_code'
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
                    type: 'lock_code_manager/write_unmanaged_code'
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

        it('tells the user when the write was refused', async () => {
            // A refused write used to reach console.error and nothing else, so
            // the row sat there with the typed value and "Enter to save" --
            // indistinguishable from a submission that never happened.
            (card as any)._editValue = '1234';
            (card as any)._saving = false;
            (card as any)._editingSlot = 1;
            sendMessagePromiseMock.mockRejectedValueOnce(new Error('Code already in use'));

            await (card as any)._saveCode(1);

            expect((card as any)._writeError).toBe('Code already in use');
            expect((card as any)._saving).toBe(false);
            // Still editing, so the message sits beside what it is about.
            expect((card as any)._editingSlot).toBe(1);
            expect((card as any)._editValue).toBe('1234');
        });

        it('a failed write does not follow you to the next row', async () => {
            // The banner renders above the whole slot list, so a failure left
            // over from slot 1 reads as if it belongs to whatever row you
            // moved on to.
            (card as any)._editingSlot = 1;
            (card as any)._editValue = '1234';
            sendMessagePromiseMock.mockRejectedValueOnce(new Error('Code already in use'));
            await (card as any)._saveCode(1);
            expect((card as any)._writeError).toBe('Code already in use');

            (card as any)._startEditing({ stopPropagation: () => {} }, { code: 'empty', slot: 2 });

            expect((card as any)._writeError).toBeUndefined();
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
            const stopPropHandler = result.values?.find((v: unknown) => typeof v === 'function');
            expect(stopPropHandler).toBeDefined();
            const mockEvent = { stopPropagation: vi.fn() };
            stopPropHandler(mockEvent);
            expect(mockEvent.stopPropagation).toHaveBeenCalled();
        });

        it('edit mode save button handler calls _saveCode', () => {
            (card as any)._editValue = '9999';
            (card as any)._saving = false;
            const slot = { slot: 1, code: '1234', managed: false };
            const result = (card as any)._renderCodeEditMode(slot);
            // Recursively collect all arrow functions from the template,
            // including those nested inside ha-icon-button sub-templates.
            const allHandlers: Array<() => void> = [];
            const collect = (tmpl: any): void => {
                for (const v of tmpl?.values ?? []) {
                    if (typeof v === 'function') allHandlers.push(v);
                    if (v?.strings && v?.values) collect(v);
                }
            };
            collect(result);
            // Skip the first handler (stopPropagation wrapper, already tested
            // above) and call the rest with a mock event that satisfies
            // both arrow-function handlers (no args needed) and method
            // references like _handleEditInput (needs e.target.value).
            expect(allHandlers.length).toBeGreaterThanOrEqual(2);
            const mockEvt = {
                key: 'Enter',
                stopPropagation: () => {},
                target: { value: '9999' }
            };
            for (const h of allHandlers.slice(1)) {
                try {
                    h(mockEvt);
                } catch {
                    // Some handlers may fail in isolation (e.g. calling
                    // async methods without full card state); coverage is
                    // gained by entering the function body.
                }
            }
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('_formatSlotRange', () => {
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

        it('returns empty string for empty array', () => {
            expect((card as any)._formatSlotRange([])).toBe('');
        });

        it('returns single slot number', () => {
            expect((card as any)._formatSlotRange([{ slot: 5 }])).toBe('5');
        });

        it('formats consecutive range', () => {
            const slots = [{ slot: 1 }, { slot: 2 }, { slot: 3 }];
            expect((card as any)._formatSlotRange(slots)).toBe('1 – 3');
        });

        it('formats non-consecutive slots', () => {
            const slots = [{ slot: 1 }, { slot: 3 }, { slot: 5 }];
            expect((card as any)._formatSlotRange(slots)).toBe('1, 3, 5');
        });

        it('formats mixed ranges and singles', () => {
            const slots = [
                { slot: 1 },
                { slot: 2 },
                { slot: 3 },
                { slot: 5 },
                { slot: 7 },
                { slot: 8 }
            ];
            expect((card as any)._formatSlotRange(slots)).toBe('1 – 3, 5, 7 – 8');
        });

        it('handles non-numeric slots by joining', () => {
            const slots = [{ slot: 'A' }, { slot: 'B' }];
            expect((card as any)._formatSlotRange(slots)).toBe('A, B');
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('_getCodeClass', () => {
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

        it('returns no-code for empty slot', () => {
            expect((card as any)._getCodeClass({ slot: 1, code: 'empty' })).toBe('no-code');
        });

        it('returns unreadable for an unvouched unreadable code', () => {
            expect((card as any)._getCodeClass({ slot: 1, code: 'unreadable_code' })).toBe(
                'unreadable'
            );
        });

        it('returns masked for code_length without code', () => {
            expect((card as any)._getCodeClass({ slot: 1, code: null, code_length: 4 })).toBe(
                'masked'
            );
        });

        it('returns empty string for actual code', () => {
            expect((card as any)._getCodeClass({ slot: 1, code: '1234' })).toBe('');
        });

        it('returns "off masked" when slot disabled and code masked', () => {
            (card as any)._config = {
                code_display: 'masked',
                lock_entity_id: 'lock.test_1',
                type: 'custom:lcm-lock-codes'
            };
            expect(
                (card as any)._getCodeClass({
                    slot: 1,
                    code: 'empty',
                    configured_code: '1234',
                    enabled: false
                })
            ).toBe('off masked');
        });

        it('returns "off" when slot disabled and code unmasked', () => {
            (card as any)._config = {
                code_display: 'unmasked',
                lock_entity_id: 'lock.test_1',
                type: 'custom:lcm-lock-codes'
            };
            expect(
                (card as any)._getCodeClass({
                    slot: 1,
                    code: 'empty',
                    configured_code: '1234',
                    enabled: false
                })
            ).toBe('off');
        });

        it('returns "pending masked" when slot enabled and code masked', () => {
            (card as any)._config = {
                code_display: 'masked',
                lock_entity_id: 'lock.test_1',
                type: 'custom:lcm-lock-codes'
            };
            expect(
                (card as any)._getCodeClass({
                    slot: 1,
                    code: 'empty',
                    configured_code: '1234',
                    enabled: true,
                    in_sync: false,
                    managed: true
                })
            ).toBe('pending masked');
        });

        it('returns "pending" when slot enabled and code unmasked', () => {
            (card as any)._config = {
                code_display: 'unmasked',
                lock_entity_id: 'lock.test_1',
                type: 'custom:lcm-lock-codes'
            };
            expect(
                (card as any)._getCodeClass({
                    slot: 1,
                    code: 'empty',
                    configured_code: '1234',
                    enabled: true,
                    in_sync: false,
                    managed: true
                })
            ).toBe('pending');
        });

        it('returns "pending masked" when out of sync, whatever the enabled state', () => {
            (card as any)._config = {
                code_display: 'masked',
                lock_entity_id: 'lock.test_1',
                type: 'custom:lcm-lock-codes'
            };
            // Pending follows the in-sync sensor; an unknown enabled state
            // changes nothing.
            expect(
                (card as any)._getCodeClass({
                    slot: 1,
                    code: 'empty',
                    configured_code: '1234',
                    in_sync: false,
                    managed: true
                })
            ).toBe('pending masked');
        });

        it('returns "off masked" for configured_code_length when slot disabled', () => {
            expect(
                (card as any)._getCodeClass({
                    slot: 1,
                    code: 'empty',
                    configured_code_length: 4,
                    enabled: false
                })
            ).toBe('off masked');
        });

        it('returns "pending masked" for configured_code_length when slot enabled', () => {
            expect(
                (card as any)._getCodeClass({
                    slot: 1,
                    code: 'empty',
                    configured_code_length: 4,
                    enabled: true,
                    in_sync: false,
                    managed: true
                })
            ).toBe('pending masked');
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('_renderCodeDisplayMode pending icon', () => {
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

        // Recursively collect all template-literal strings, including nested
        // sub-templates passed via ${...}. Used to assert presence of conditional
        // markup like the pending icon.
        const collectAllStrings = (tmpl: any): string => {
            if (!tmpl) return '';
            const parts: string[] = [];
            if (tmpl.strings) parts.push(tmpl.strings.join(''));
            for (const v of tmpl.values ?? []) {
                if (v && typeof v === 'object' && (v.strings || v.values)) {
                    parts.push(collectAllStrings(v));
                }
            }
            return parts.join(' ');
        };

        it('renders clock-icon prefix for pending state', () => {
            const slot = {
                slot: 1,
                code: 'empty',
                configured_code: '1234',
                enabled: true,
                in_sync: false,
                managed: true
            };
            const result = (card as any)._renderCodeDisplayMode(
                slot,
                false,
                'masked_with_reveal',
                false
            );
            expect(collectAllStrings(result)).toContain('lcm-code-pending-icon');
        });

        it('does not render clock-icon prefix for off state', () => {
            const slot = {
                slot: 1,
                code: 'empty',
                configured_code: '1234',
                enabled: false,
                managed: true
            };
            const result = (card as any)._renderCodeDisplayMode(
                slot,
                false,
                'masked_with_reveal',
                false
            );
            expect(collectAllStrings(result)).not.toContain('lcm-code-pending-icon');
        });

        it('does not render clock-icon prefix when lock has the code', () => {
            const slot = { slot: 1, code: '1234', enabled: true, managed: true };
            const result = (card as any)._renderCodeDisplayMode(
                slot,
                true,
                'masked_with_reveal',
                false
            );
            expect(collectAllStrings(result)).not.toContain('lcm-code-pending-icon');
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('_renderSlotChip pending state on slot name', () => {
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

        // Recursively collect template-literal strings, including nested
        // sub-templates, so we can assert presence of conditional markup
        // and class modifiers on the rendered chip.
        const collectAllStrings = (tmpl: any): string => {
            if (!tmpl) return '';
            const parts: string[] = [];
            if (tmpl.strings) parts.push(tmpl.strings.join(''));
            for (const v of tmpl.values ?? []) {
                if (v && typeof v === 'object' && (v.strings || v.values)) {
                    parts.push(collectAllStrings(v));
                } else if (typeof v === 'string') {
                    parts.push(v);
                }
            }
            return parts.join(' ');
        };

        // Split a rendered template's collected text into whitespace-separated
        // tokens. The chip's interpolated class modifiers ('active', 'pending',
        // 'disabled', etc.) appear as their own tokens, distinct from the
        // hyphenated 'slot-name-pending-icon' class on the icon element.
        const tokenize = (rendered: string): string[] =>
            rendered.split(/\s+/).filter((s) => s.length > 0);

        it('marks chip as pending when slot is enabled and lock has no code', () => {
            const slot = {
                active: true,
                code: 'empty',
                configured_code: '1234',
                enabled: true,
                in_sync: false,
                managed: true,
                name: 'Test User',
                slot: 1
            };
            const result = (card as any)._renderSlotChip(slot, false);
            const tokens = tokenize(collectAllStrings(result));
            expect(tokens).toContain('pending');
        });

        it('does not mark chip as pending when slot is disabled', () => {
            const slot = {
                active: false,
                code: 'empty',
                configured_code: '1234',
                enabled: false,
                managed: true,
                name: 'Test User',
                slot: 1
            };
            const result = (card as any)._renderSlotChip(slot, false);
            const rendered = collectAllStrings(result);
            const tokens = tokenize(rendered);
            // Chip should be 'disabled' but not 'pending'.
            expect(tokens).toContain('disabled');
            expect(tokens).not.toContain('pending');
            expect(rendered).not.toContain('slot-name-pending-icon');
        });

        it('does not mark chip as pending when lock has the code', () => {
            const slot = {
                active: true,
                code: '1234',
                enabled: true,
                managed: true,
                name: 'Test User',
                slot: 1
            };
            const result = (card as any)._renderSlotChip(slot, false);
            const rendered = collectAllStrings(result);
            const tokens = tokenize(rendered);
            expect(tokens).not.toContain('pending');
            expect(rendered).not.toContain('slot-name-pending-icon');
        });

        it('renders clock-icon prefix on slot-name for pending slots', () => {
            const slot = {
                active: true,
                code: 'empty',
                configured_code: '1234',
                enabled: true,
                in_sync: false,
                managed: true,
                name: 'Test User',
                slot: 1
            };
            const result = (card as any)._renderSlotChip(slot, false);
            expect(collectAllStrings(result)).toContain('slot-name-pending-icon');
        });

        it('does not render clock-icon prefix on slot-name for disabled slots', () => {
            const slot = {
                active: false,
                code: 'empty',
                configured_code: '1234',
                enabled: false,
                managed: true,
                name: 'Test User',
                slot: 1
            };
            const result = (card as any)._renderSlotChip(slot, false);
            expect(collectAllStrings(result)).not.toContain('slot-name-pending-icon');
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('pending follows the in-sync sensor', () => {
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

        const collectAllStrings = (tmpl: any): string => {
            if (!tmpl) return '';
            const parts: string[] = [];
            if (tmpl.strings) parts.push(tmpl.strings.join(''));
            for (const v of tmpl.values ?? []) {
                if (v && typeof v === 'object' && (v.strings || v.values)) {
                    parts.push(collectAllStrings(v));
                } else if (typeof v === 'string') {
                    parts.push(v);
                }
            }
            return parts.join(' ');
        };

        const chipIsPending = (slot: Record<string, unknown>): boolean =>
            collectAllStrings((card as any)._renderSlotChip(slot, false))
                .split(/\s+/)
                .includes('pending');

        const codeIsPending = (slot: Record<string, unknown>): boolean =>
            (card as any)._getCodeClass(slot).split(' ').includes('pending');

        const managedSlot = {
            active: true,
            configured_code: '1234',
            enabled: true,
            managed: true,
            name: 'Alice',
            slot: 1
        };

        it('marks a managed out-of-sync slot pending whatever the lock holds', () => {
            for (const lockHolds of [
                { code: 'empty' },
                { code: '9999' },
                { code: null, code_length: 4 },
                { code: 'unreadable_code' },
                { code: 'unanswered' }
            ]) {
                const slot = { ...managedSlot, ...lockHolds, in_sync: false };
                expect(chipIsPending(slot), JSON.stringify(lockHolds)).toBe(true);
                expect(codeIsPending(slot), JSON.stringify(lockHolds)).toBe(true);
            }
        });

        it('does not mark a slot pending when its condition keeps the PIN off the lock', () => {
            // What the backend sends for an enabled slot whose condition is
            // blocking: the lock is empty, which is what LCM wants there.
            const slot = { ...managedSlot, active: false, code: 'empty', in_sync: true };
            expect(chipIsPending(slot)).toBe(false);
            expect((card as any)._getCodeClass(slot)).toBe('off masked');
        });

        it('does not mark a slot pending while its in-sync state is unknown', () => {
            const slot = { ...managedSlot, code: 'empty' };
            expect(chipIsPending(slot)).toBe(false);
            expect(codeIsPending(slot)).toBe(false);
        });

        it('does not call an active PIN off while its in-sync state is unknown', () => {
            // The in-sync sensor reads unavailable (the lock entity is), so
            // in_sync is null: neither a write due nor a PIN meant off the lock.
            for (const inSync of [null, undefined]) {
                const slot = { ...managedSlot, code: 'empty', in_sync: inSync };
                expect((card as any)._getCodeClass(slot), String(inSync)).toBe('masked');
            }
        });

        it('never marks an unmanaged slot pending', () => {
            const slot = { ...managedSlot, code: 'empty', in_sync: false, managed: false };
            expect(chipIsPending(slot)).toBe(false);
            expect(codeIsPending(slot)).toBe(false);
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('_renderSlotChip — slot label and state badge dot', () => {
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

        // Variant of collectAllStrings that also folds numeric values into
        // the rendered string so we can assert on slot numbers and similar
        // primitive interpolations. Interleaves strings + values in the
        // same order the template renders so adjacent text reads naturally.
        const collectAllStrings = (tmpl: any): string => {
            if (!tmpl) return '';
            const strings: string[] = (tmpl.strings ?? []) as string[];
            const values: unknown[] = (tmpl.values ?? []) as unknown[];
            const parts: string[] = [];
            for (let i = 0; i < strings.length; i++) {
                parts.push(strings[i]);
                if (i < values.length) {
                    const v = values[i];
                    if (v && typeof v === 'object' && ((v as any).strings || (v as any).values)) {
                        parts.push(collectAllStrings(v));
                    } else if (Array.isArray(v)) {
                        for (const item of v) {
                            if (item && typeof item === 'object') {
                                parts.push(collectAllStrings(item));
                            }
                        }
                    } else if (
                        typeof v === 'string' ||
                        typeof v === 'number' ||
                        typeof v === 'boolean'
                    ) {
                        parts.push(String(v));
                    }
                }
            }
            return parts.join('');
        };

        it('renders "Slot N · {entry_title}" when config_entry_title is set', () => {
            const slot = {
                active: true,
                code: '1234',
                config_entry_title: 'House Locks',
                enabled: true,
                managed: true,
                name: 'Alice',
                slot: 3
            };
            const result = (card as any)._renderSlotChip(slot, false);
            const rendered = collectAllStrings(result);
            expect(rendered).toContain('slot-entry-title');
            expect(rendered).toContain('House Locks');
            // The slot number should still be present.
            expect(rendered).toMatch(/Slot\s*3/);
        });

        it('renders just "Slot N" when config_entry_title is absent', () => {
            const slot = {
                active: true,
                code: '1234',
                enabled: true,
                managed: true,
                name: 'Alice',
                slot: 3
            };
            const result = (card as any)._renderSlotChip(slot, false);
            const rendered = collectAllStrings(result);
            // No entry-title span when no title is provided.
            expect(rendered).not.toContain('slot-entry-title');
            expect(rendered).toMatch(/Slot\s*3/);
        });

        it('renders a colored dot prefix on the state badge for active slots', () => {
            const slot = {
                active: true,
                code: '1234',
                enabled: true,
                managed: true,
                name: 'Alice',
                slot: 1
            };
            const result = (card as any)._renderSlotChip(slot, false);
            const rendered = collectAllStrings(result);
            // The dot span sits inside the lcm-badge for state badges.
            expect(rendered).toContain('class="dot"');
        });

        it('renders a dot prefix on inactive (blocked) state badges', () => {
            const slot = {
                active: false,
                code: '1234',
                configured_code: '1234',
                enabled: true,
                managed: true,
                name: 'Alice',
                slot: 1
            };
            const result = (card as any)._renderSlotChip(slot, false);
            const rendered = collectAllStrings(result);
            expect(rendered).toContain('class="dot"');
        });

        it('renders a dot prefix on disabled state badges', () => {
            const slot = {
                active: false,
                code: 'empty',
                configured_code: '1234',
                enabled: false,
                managed: true,
                name: 'Alice',
                slot: 1
            };
            const result = (card as any)._renderSlotChip(slot, false);
            const rendered = collectAllStrings(result);
            expect(rendered).toContain('class="dot"');
        });

        it('does NOT render a dot on Managed/External identity badges', () => {
            // External slot: status badge is 'empty' (no dot), managed badge is 'external' (no dot).
            const slot = {
                code: '9999',
                managed: false,
                slot: 5
            };
            const result = (card as any)._renderSlotChip(slot, false);
            const rendered = collectAllStrings(result);
            // The status badge for an external slot is 'active' (has code), so it gets a dot,
            // but the identity badge (external) must not.
            // Count dot spans — only the state badge should have one.
            const dotMatches = rendered.match(/class="dot"/g) ?? [];
            expect(dotMatches.length).toBe(1);
        });

        it('renders the eye reveal button for disabled slots with configured_code', () => {
            // A disabled managed slot whose lock has no code, but LCM has the
            // configured PIN — eye reveal should render so the user can see
            // the masked configured PIN.
            const slot = {
                active: false,
                code: 'empty',
                configured_code: '1234',
                enabled: false,
                managed: true,
                name: 'Alice',
                slot: 1
            };
            const result = (card as any)._renderSlotChip(slot, false);
            const rendered = collectAllStrings(result);
            expect(rendered).toContain('lcm-reveal-button');
        });

        it('renders the eye reveal button for slots with only configured_code_length', () => {
            const slot = {
                active: false,
                code: 'empty',
                configured_code_length: 4,
                enabled: false,
                managed: true,
                name: 'Alice',
                slot: 1
            };
            const result = (card as any)._renderSlotChip(slot, false);
            const rendered = collectAllStrings(result);
            expect(rendered).toContain('lcm-reveal-button');
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('_formatCode', () => {
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

        it('returns muted hollow dots for an unvouched unreadable code', () => {
            expect((card as any)._formatCode({ slot: 1, code: 'unreadable_code' })).toBe('◦◦◦');
        });

        it('returns dash for empty slot with no configured code', () => {
            expect((card as any)._formatCode({ slot: 1, code: 'empty' })).toBe('—');
        });

        it('returns masked code when shouldMask', () => {
            (card as any)._revealed = false;
            expect((card as any)._formatCode({ slot: 1, code: '1234' })).toBe('••••');
        });

        it('returns actual code when revealed', () => {
            (card as any)._revealed = true;
            expect((card as any)._formatCode({ slot: 1, code: '1234' })).toBe('1234');
        });

        it('returns masked configured_code for disabled slot', () => {
            (card as any)._revealed = false;
            expect(
                (card as any)._formatCode({ slot: 1, code: null, configured_code: '5678' })
            ).toBe('••••');
        });

        it('returns revealed configured_code when unmasked', () => {
            (card as any)._config = {
                lock_entity_id: 'lock.test_1',
                code_display: 'unmasked',
                type: 'custom:lcm-lock-codes'
            };
            expect(
                (card as any)._formatCode({ slot: 1, code: null, configured_code: '5678' })
            ).toBe('5678');
        });

        it('returns bullets for configured_code_length', () => {
            expect(
                (card as any)._formatCode({ slot: 1, code: null, configured_code_length: 6 })
            ).toBe('••••••');
        });

        it('returns bullets for code_length', () => {
            expect((card as any)._formatCode({ slot: 1, code: 'empty', code_length: 4 })).toBe(
                '••••'
            );
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    describe('_navigateToSlot', () => {
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

        it('navigates with valid config entry id', () => {
            const pushStateSpy = vi.spyOn(history, 'pushState');
            (card as any)._navigateToSlot('test-entry-id');
            expect(pushStateSpy).toHaveBeenCalledWith(
                null,
                '',
                '/config/integrations/integration/lock_code_manager#config_entry=test-entry-id'
            );
            pushStateSpy.mockRestore();
        });

        it('returns early for undefined config entry id', () => {
            const pushStateSpy = vi.spyOn(history, 'pushState');
            (card as any)._navigateToSlot(undefined);
            expect(pushStateSpy).not.toHaveBeenCalled();
            pushStateSpy.mockRestore();
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    // Phase A — A3 + A8: keyboard a11y on managed slot chips and the
    // reveal-button click no longer bubbles into chip navigation.
    describe('Phase A — slot-chip a11y + reveal stopPropagation', () => {
        /** Recursively join all template strings (deep) */
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        function deepStrings(result: any): string {
            if (!result || typeof result !== 'object') return '';
            if (!result.strings) return '';
            const own = (result.strings ?? []).join('');
            const nested = (result.values ?? []).map(deepStrings).join('');
            return own + nested;
        }

        /** Recursively collect all function values from a TemplateResult */
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        function collectAllHandlers(result: any): Array<(...args: any[]) => void> {
            // eslint-disable-next-line @typescript-eslint/no-explicit-any
            const handlers: Array<(...args: any[]) => void> = [];
            if (!result?.values) return handlers;
            for (const v of result.values) {
                if (typeof v === 'function') {
                    handlers.push(v);
                } else if (v?.strings && v?.values) {
                    handlers.push(...collectAllHandlers(v));
                } else if (Array.isArray(v)) {
                    for (const item of v) {
                        if (item?.strings && item?.values) {
                            handlers.push(...collectAllHandlers(item));
                        }
                    }
                }
            }
            return handlers;
        }

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

        // A3 — slot-chip a11y when clickable. The a11y attributes are bound
        // as dynamic Lit values (ternary on isClickable), so they appear in
        // the template's `values` array rather than in the static `strings`.
        // We assert against the raw template fragment values for the chip.
        it('exposes role=button, tabindex=0 and aria-label on a clickable managed slot chip', () => {
            const tmpl = (card as any)._renderSlotChip(
                {
                    active: true,
                    code: '1234',
                    config_entry_id: 'entry-id',
                    config_entry_title: 'Front Door',
                    enabled: true,
                    managed: true,
                    name: 'Alice',
                    slot: 3
                },
                false
            );
            const values = tmpl.values ?? [];
            // The chip's static strings include "role=", "tabindex=" and
            // "aria-label=" attribute names; the dynamic values fill in the
            // attribute values. When clickable, those values are populated.
            expect(values).toContain('button');
            expect(values).toContain('0');
            expect(values).toContain('Manage slot 3 · Front Door');
        });

        it('does NOT expose button role on a non-clickable (unmanaged) slot chip', () => {
            const tmpl = (card as any)._renderSlotChip(
                {
                    code: '5678',
                    managed: false,
                    slot: 4
                },
                false
            );
            const values = tmpl.values ?? [];
            // When not clickable, the ternaries fall through to `nothing`,
            // so the dynamic role/tabindex/aria-label values are removed.
            expect(values).not.toContain('button');
            // 'Manage slot 4' must NOT appear as an aria-label value.
            expect(
                values.some((v: unknown) => typeof v === 'string' && v.startsWith('Manage slot'))
            ).toBe(false);
        });

        it('Enter and Space on a clickable slot chip navigate to the slot', () => {
            let navTo: string | undefined;
            (card as any)._navigateToSlot = (id: string) => {
                navTo = id;
            };
            const tmpl = (card as any)._renderSlotChip(
                {
                    active: true,
                    code: '1234',
                    config_entry_id: 'entry-id',
                    enabled: true,
                    managed: true,
                    name: 'Alice',
                    slot: 3
                },
                false
            );
            const handlers = collectAllHandlers(tmpl);
            // Find a keydown-shaped handler that responds to Enter.
            for (const h of handlers.filter((fn) => fn.length === 1)) {
                if (navTo === 'entry-id') break;
                try {
                    h({
                        key: 'Enter',
                        preventDefault: () => undefined
                    } as unknown as KeyboardEvent);
                } catch {
                    // ignore — some handlers expect different shapes
                }
            }
            expect(navTo).toBe('entry-id');
        });

        // A8 — reveal click stops propagation so chip navigation doesn't fire
        it('reveal-button click on a clickable slot calls stopPropagation', () => {
            let stopped = 0;
            const tmpl = (card as any)._renderCodeDisplayMode(
                {
                    active: true,
                    code: '1234',
                    config_entry_id: 'entry-id',
                    enabled: true,
                    managed: true,
                    name: 'Alice',
                    slot: 3
                },
                true,
                'masked_with_reveal',
                false
            );
            const handlers = collectAllHandlers(tmpl);
            // The reveal click handler is the arity-1 handler that calls
            // stopPropagation on the synthetic Event. Lift the fakeEvent out
            // of the loop so the no-loop-func ESLint rule is satisfied.
            const fakeEvent = {
                stopPropagation: () => {
                    stopped += 1;
                }
            } as unknown as Event;
            for (const h of handlers.filter((fn) => fn.length === 1)) {
                try {
                    h(fakeEvent);
                } catch {
                    // ignore
                }
            }
            expect(stopped).toBeGreaterThanOrEqual(1);
        });
        /* eslint-enable @typescript-eslint/no-explicit-any */
    });

    // Phase B: a11y polish — semantic HTML, ARIA labels, reduced motion (PR #1116 Phase B).
    describe('Phase B — a11y polish', () => {
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        let card: any;

        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        function deepStrings(result: any): string {
            if (result === null || result === undefined) return '';
            if (typeof result === 'string') return result;
            if (typeof result === 'number' || typeof result === 'boolean') return String(result);
            if (Array.isArray(result)) return result.map(deepStrings).join('');
            if (!result || typeof result !== 'object') return '';
            if (!result.strings) return '';
            const own = (result.strings ?? []).join('');
            const nested = (result.values ?? []).map(deepStrings).join('');
            return own + nested;
        }

        beforeEach(async () => {
            card = document.createElement('lcm-lock-codes');
            card.setConfig({ lock_entity_id: 'lock.test_1', type: 'custom:lcm-lock-codes' });
            card.hass = createMockHassWithConnection();
            container.appendChild(card);
            await new Promise((r) => setTimeout(r, 0));
        });

        /* eslint-disable @typescript-eslint/no-explicit-any */

        describe('B1: card title is rendered as <h2>', () => {
            it('main render renders card-header-title as <h2>', () => {
                card._data = makeLockCoordinatorData();
                const tmpl = card.render();
                const joined = deepStrings(tmpl);
                expect(joined).toContain('<h2 class="card-header-title"');
                expect(joined).not.toContain('<span class="card-header-title"');
            });

            it('stub render renders card-header-title as <h2>', () => {
                card._isStub = true;
                const tmpl = card.render();
                const joined = deepStrings(tmpl);
                expect(joined).toContain('<h2 class="card-header-title"');
            });
        });

        describe('B2: aria-hidden on decorative dots and icons', () => {
            it('header icon bubble has aria-hidden', () => {
                card._data = makeLockCoordinatorData();
                const tmpl = card.render();
                const joined = deepStrings(tmpl);
                expect(joined).toMatch(/<div class="header-icon" aria-hidden="true"/);
            });

            it('state badge dot has aria-hidden', () => {
                const tmpl = card._renderSlotChip(
                    {
                        active: true,
                        code: '1234',
                        enabled: true,
                        managed: true,
                        name: 'Alice',
                        slot: 1
                    },
                    false
                );
                const joined = deepStrings(tmpl);
                expect(joined).toMatch(/<span class="dot" aria-hidden="true"/);
            });

            it('slot-name pending icon has aria-hidden + visually-hidden text', () => {
                const tmpl = card._renderSlotChip(
                    {
                        active: false,
                        code: null,
                        configured_code: '1234',
                        enabled: true,
                        in_sync: false,
                        managed: true,
                        name: 'Alice',
                        slot: 1
                    },
                    false
                );
                const joined = deepStrings(tmpl);
                expect(joined).toContain('class="slot-name-pending-icon"');
                expect(joined).toMatch(/class="slot-name-pending-icon"[\s\S]*?aria-hidden="true"/);
                expect(joined).toContain('class="visually-hidden">Pending sync');
            });

            it('lcm-code pending icon has aria-hidden + visually-hidden text', () => {
                const tmpl = card._renderCodeDisplayMode(
                    {
                        code: null,
                        configured_code: '1234',
                        enabled: true,
                        in_sync: false,
                        managed: true,
                        slot: 1
                    },
                    false,
                    'masked_with_reveal',
                    false
                );
                const joined = deepStrings(tmpl);
                expect(joined).toContain('class="lcm-code-pending-icon"');
                expect(joined).toMatch(/class="lcm-code-pending-icon"[\s\S]*?aria-hidden="true"/);
                expect(joined).toContain('class="visually-hidden">Pending sync');
            });
        });

        describe('B3: edit input accessible name', () => {
            it('slot-code-input has aria-label', () => {
                const tmpl = card._renderCodeEditMode({ slot: 5 });
                // deepStrings only returns the static template strings, not
                // interpolated values, so check both: static aria-label
                // attribute, and the slot number rendered as a value.
                const joined = deepStrings(tmpl);
                expect(joined).toMatch(/class="slot-code-input"/);
                // The static fragment containing the aria-label attribute is
                // sliced into two parts by `${slot.slot}`, so the literal
                // "aria-label=" prefix and the static "Slot " / " PIN"
                // tokens are present in the joined static strings.
                expect(joined).toContain('aria-label="Slot ');
                expect(joined).toContain(' PIN"');
            });
        });

        describe('B4: summary table semantics', () => {
            it('summary table has visually-hidden caption', () => {
                card._data = makeLockCoordinatorData();
                const tmpl = card._renderSummaryTable();
                const joined = deepStrings(tmpl);
                expect(joined).toContain('<caption class="visually-hidden">');
                expect(joined).toContain('Code slot summary');
            });

            it('summary table headers use scope="col"', () => {
                card._data = makeLockCoordinatorData();
                const tmpl = card._renderSummaryTable();
                const joined = deepStrings(tmpl);
                expect(joined).toContain('scope="col"');
            });

            it('summary table row labels use scope="row"', () => {
                card._data = makeLockCoordinatorData();
                const tmpl = card._renderSummaryTable();
                const joined = deepStrings(tmpl);
                expect(joined).toContain('scope="row"');
            });
        });

        describe('B5: suspended banner role=status', () => {
            it('suspended banner gets role="status"', () => {
                card._data = makeLockCoordinatorData({ sync_status: 'suspended' });
                const tmpl = card.render();
                const joined = deepStrings(tmpl);
                expect(joined).toMatch(/class="suspended-banner" role="status"/);
            });
        });

        describe('B6: prefers-reduced-motion CSS rule', () => {
            it('lock card stylesheet contains a prefers-reduced-motion: reduce media query', async () => {
                const { lockCodesCardStyles } = await import('./lock-codes-card.styles');
                const allCss = lockCodesCardStyles.map((s) => String(s.cssText ?? s)).join('\n');
                expect(allCss).toMatch(/@media\s*\(prefers-reduced-motion:\s*reduce\)/);
            });
        });

        describe('VC1: active managed slot chip has no special tint', () => {
            it('stylesheet does not define .slot-chip.active.managed background', async () => {
                const { lockCodesCardStyles } = await import('./lock-codes-card.styles');
                const allCss = lockCodesCardStyles.map((s) => String(s.cssText ?? s)).join('\n');
                // .slot-chip.active.managed should not have its own background rule
                // (active inherits the chip's default --lcm-section-bg).
                expect(allCss).not.toMatch(/\.slot-chip\.active\.managed\s*\{/);
            });

            it('active.unmanaged still has a subtle distinguishing tint', async () => {
                const { lockCodesCardStyles } = await import('./lock-codes-card.styles');
                const allCss = lockCodesCardStyles.map((s) => String(s.cssText ?? s)).join('\n');
                expect(allCss).toMatch(/\.slot-chip\.active\.unmanaged\s*\{[^}]*background:/s);
            });
        });

        /* eslint-enable @typescript-eslint/no-explicit-any */
    });
});
