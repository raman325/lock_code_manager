import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import { HomeAssistant } from './ha_type_stubs';
import { createMockHassWithConnection } from './test/mock-hass';
import { LockCoordinatorData, LockCoordinatorSlotData } from './types';

/**
 * The lock-codes card judged by what it renders.
 *
 * Data arrives through the subscription the way the backend sends it, and
 * every assertion reads the card's shadow DOM: what a user would see, not
 * the fields behind it.
 */

interface LockCodesCard extends HTMLElement {
    hass: HomeAssistant;
    setConfig: (config: Record<string, unknown>) => void;
    updateComplete: Promise<boolean>;
}

const MANAGED_PIN = '4815';

describe('lock-codes card, rendered', () => {
    let card: LockCodesCard;
    let push: (data: LockCoordinatorData) => void;
    let sendMessagePromise: ReturnType<typeof vi.fn>;

    beforeAll(async () => {
        if (!customElements.get('lcm-lock-codes')) {
            await import('./lock-codes-card');
        }
    });

    beforeEach(async () => {
        card = document.createElement('lcm-lock-codes') as LockCodesCard;
        card.setConfig({
            lock_entity_id: 'lock.front_door',
            type: 'custom:lcm-lock-codes'
        });
        const hass = createMockHassWithConnection({
            onSubscribe: (callback) => {
                push = callback as (data: LockCoordinatorData) => void;
            }
        });
        sendMessagePromise = vi.fn().mockResolvedValue({});
        (hass.connection as unknown as Record<string, unknown>).sendMessagePromise =
            sendMessagePromise;
        card.hass = hass;
        document.body.appendChild(card);
        await new Promise((r) => setTimeout(r, 0));
    });

    afterEach(() => {
        card.remove();
    });

    async function show(slots: LockCoordinatorSlotData[]): Promise<ShadowRoot> {
        push({
            lock_entity_id: 'lock.front_door',
            lock_name: 'Front Door',
            slots
        });
        await card.updateComplete;
        return card.shadowRoot!;
    }

    /** The editable code of the chip labelled ``Slot <slot>``. */
    function codeOf(root: ShadowRoot, slot: number): HTMLElement | undefined {
        return [...root.querySelectorAll<HTMLElement>('.lcm-code.editable')].find((element) =>
            element
                .closest('.slot-chip')
                ?.textContent?.replace(/\s+/g, ' ')
                .includes(`Slot ${slot} `)
        );
    }

    async function edit(root: ShadowRoot, slot: number, value: string): Promise<void> {
        const code = codeOf(root, slot);
        expect(code, `slot ${slot} is not editable`).toBeTruthy();
        code!.click();
        await card.updateComplete;
        const input = root.querySelector<HTMLInputElement>('.slot-code-input')!;
        input.value = value;
        input.dispatchEvent(new Event('input'));
        input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter' }));
        await new Promise((r) => setTimeout(r, 0));
        await card.updateComplete;
    }

    const managed: LockCoordinatorSlotData = {
        active: true,
        code: MANAGED_PIN,
        config_entry_id: 'entry',
        enabled: true,
        managed: true,
        name: 'Mom',
        slot: 1
    };

    it('keeps every other PIN masked after an unmanaged code is edited (PR #1519)', async () => {
        // Revealing to edit one row put every managed PIN on screen in
        // cleartext, and it stayed there after the write.
        const root = await show([managed, { code: '2222', managed: false, slot: 2 }]);
        expect(root.textContent).not.toContain(MANAGED_PIN);

        await edit(root, 2, '9876');

        expect(sendMessagePromise).toHaveBeenCalled();
        expect(root.textContent).not.toContain(MANAGED_PIN);
    });

    it("drops a failed write's error when another row is edited (PR #1521)", async () => {
        const root = await show([
            managed,
            { code: '2222', managed: false, slot: 2 },
            { code: '3333', managed: false, slot: 3 }
        ]);
        sendMessagePromise.mockRejectedValueOnce(new Error('Code already in use'));
        await edit(root, 2, '1111');
        expect(root.querySelector('.write-error')?.textContent).toContain('Code already in use');

        // Straight to another row, still editing this one: no cancel, which
        // clears the error on its own.
        codeOf(root, 3)!.click();
        await card.updateComplete;

        expect(root.querySelector('.write-error')).toBeNull();
    });

    it('pairs a lone active slot with an empty neighbour, not full width (PR #811)', async () => {
        const root = await show([
            managed,
            { code: 'empty', managed: false, slot: 2 },
            { code: 'empty', managed: false, slot: 3 },
            { code: 'empty', managed: false, slot: 4 }
        ]);

        const chips = [...root.querySelectorAll('.slots-grid > .slot-chip')];
        expect(chips[0].classList.contains('full-width')).toBe(false);
        expect(chips[1].classList.contains('empty')).toBe(true);
        expect(chips[1].textContent).toContain('Slot 2');
        expect(root.querySelector('.empty-summary-range')?.textContent).toContain('3');
    });

    it.each([
        [
            'the masked slot the backend sends',
            {
                code: null,
                code_length: 6,
                configured_code_length: 6,
                in_sync: true
            }
        ],
        [
            'an unreadable code with its configured length',
            {
                code: 'unreadable_code',
                configured_code_length: 6,
                in_sync: true
            }
        ]
    ])('masks %s to the PIN length, not a fixed 3 dots (PR #1282)', async (_label, code) => {
        // Matter and masking Z-Wave locks never report the digits; the card
        // showed a fixed three dots whatever the PIN's length. The first slot
        // is what tests/test_websocket.py's
        // test_a_lock_that_withholds_codes_tells_the_card_the_pin_length
        // receives from the backend.
        const root = await show([{ ...managed, ...code }]);

        expect(root.querySelector('.lcm-code')?.textContent?.trim()).toBe('••••••');
    });
});
