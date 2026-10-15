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

    it.each([
        ['an odd slot borrows the next empty one', 1, [2], '3 – 5'],
        ['an even slot borrows the previous empty one', 4, [3], '1 – 2']
    ])(
        'pairs a lone active slot, not full width: %s (PR #811)',
        async (_label, active, borrowed, rest) => {
            const slots: LockCoordinatorSlotData[] = [1, 2, 3, 4, 5].map((slot) =>
                slot === active ? { ...managed, slot } : { code: 'empty', managed: false, slot }
            );
            const root = await show(slots);

            const chips = [...root.querySelectorAll('.slots-grid > .slot-chip')];
            expect(chips.some((chip) => chip.classList.contains('full-width'))).toBe(false);
            expect(
                chips
                    .filter((chip) => chip.classList.contains('empty'))
                    .map((chip) => chip.textContent?.replace(/\s+/g, ' ').trim())
            ).toEqual(borrowed.map((slot) => `Slot ${slot} Empty`));
            expect(root.querySelector('.empty-summary-range')?.textContent?.trim()).toBe(rest);
        }
    );

    it.each([
        [
            'a readable PIN, masked',
            {
                code: null,
                code_length: 6,
                configured_code_length: 6,
                in_sync: true
            }
        ],
        [
            'a withheld code in sync',
            {
                code: 'unreadable_code',
                configured_code_length: 6,
                in_sync: true
            }
        ],
        [
            "a slot in sync the lock didn't answer for",
            {
                code: 'unanswered',
                configured_code_length: 6,
                in_sync: true
            }
        ]
    ])('masks %s to the PIN length, not a fixed 3 dots (PR #1282)', async (_label, code) => {
        // Matter and masking Z-Wave locks never report the digits; the card
        // showed a fixed three dots whatever the PIN's length. The last two
        // are what tests/test_websocket.py's
        // test_a_lock_that_gives_no_pin_back_is_never_shown_as_reporting_one
        // receives from the backend.
        const root = await show([{ ...managed, ...code }]);

        expect(root.querySelector('.lcm-code')?.textContent?.trim()).toBe('••••••');
    });

    it.each([
        [
            'shows "Not reported" when nothing stands in',
            { active: false, enabled: false, in_sync: true },
            'Not reported',
            "This lock can't report its codes"
        ],
        [
            'stands the configured PIN in when in sync',
            { configured_code_length: 4, in_sync: true },
            '••••',
            "Configured in Lock Code Manager — this lock can't report its codes"
        ]
    ])(
        "says a lock that can't report a slot did answer (#1581): %s",
        async (_label, fields, text, title) => {
            // Zigbee user status Not Supported: the lock answered, so the slot
            // is not "unanswered".
            const root = await show([{ ...managed, code: 'not_reported', ...fields }]);

            const code = root.querySelector<HTMLElement>('.lcm-code');
            expect(code?.textContent?.trim()).toBe(text);
            expect(code?.title).toBe(title);
            expect(root.textContent).not.toContain("didn't answer");
        }
    );

    it("shows a code a lock that can report won't report as held, not empty (#1581)", async () => {
        // The backend only hands "not_reported" over for a slot it doesn't
        // manage from a lock that has described some slot: a keypad code.
        const root = await show([
            managed,
            { code: 'not_reported', managed: false, slot: 2 },
            { code: 'empty', managed: false, slot: 3 }
        ]);

        const chip = [...root.querySelectorAll<HTMLElement>('.slot-chip')].find((element) =>
            element.textContent?.replace(/\s+/g, ' ').includes('Slot 2 ')
        );
        expect(chip, 'slot 2 is folded into the empty slots').toBeTruthy();
        expect(chip?.querySelector('.lcm-code')?.textContent?.trim()).toBe('Not reported');
        expect(root.querySelector('.empty-summary-range')?.textContent?.trim() ?? '3').toBe('3');
    });

    it('keeps codes masked in "masked" mode even when the reveal flag is set', async () => {
        card.setConfig({
            code_display: 'masked',
            lock_entity_id: 'lock.front_door',
            type: 'custom:lcm-lock-codes'
        });
        // eslint-disable-next-line no-underscore-dangle -- no public way to set reveal in masked mode
        (card as unknown as { _revealed: boolean })._revealed = true;
        const root = await show([managed]);

        expect(root.querySelector('.lcm-code')?.textContent?.trim()).toBe('••••');
        expect(root.textContent).not.toContain(MANAGED_PIN);
    });

    it('titles the card with the entity id when nothing names the lock', async () => {
        // No lock_name in the payload, and hass has no state for the lock.
        push({
            lock_entity_id: 'lock.front_door',
            slots: [managed]
        } as unknown as LockCoordinatorData);
        await card.updateComplete;

        expect(card.shadowRoot!.querySelector('.card-header-title')?.textContent).toBe(
            'lock.front_door'
        );
    });

    it.each([
        // Both empties between two lone actives are borrowed, one each side.
        ['active, empty, empty, active, empty', 5, [1, 4], ['Slot 2 Empty', 'Slot 3 Empty'], ['5']],
        [
            'active, 3 empty, active, 3 empty',
            8,
            [1, 5],
            ['Slot 2 Empty', 'Slot 6 Empty'],
            ['3 – 4', '7 – 8']
        ]
    ])(
        'summarises each run of empty slots between interleaved actives: %s',
        async (_label, total, actives, borrowed, summaries) => {
            const slots: LockCoordinatorSlotData[] = Array.from({ length: total }, (_, i) =>
                actives.includes(i + 1)
                    ? { ...managed, slot: i + 1 }
                    : { code: 'empty', managed: false, slot: i + 1 }
            );
            const root = await show(slots);

            const chips = [...root.querySelectorAll('.slots-grid > .slot-chip')];
            expect(
                chips
                    .filter((chip) => chip.classList.contains('empty'))
                    .map((chip) => chip.textContent?.replace(/\s+/g, ' ').trim())
            ).toEqual(borrowed);
            expect(
                [...root.querySelectorAll('.empty-summary-range')].map((range) =>
                    range.textContent?.trim()
                )
            ).toEqual(summaries);
        }
    );
});
