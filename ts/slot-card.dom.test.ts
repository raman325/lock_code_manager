import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';

import { HomeAssistant } from './ha_type_stubs';
import { createMockHassWithConnection } from './test/mock-hass';
import { SlotCardData } from './types';

/**
 * The slot card judged by what it renders: data arrives through the
 * subscription, and assertions read the shadow DOM.
 */

interface SlotCard extends HTMLElement {
    hass: HomeAssistant;
    setConfig: (config: Record<string, unknown>) => void;
    updateComplete: Promise<boolean>;
}

const PIN = '4815';

describe('slot card, rendered', () => {
    let card: SlotCard;
    let push: (data: SlotCardData) => void;

    beforeAll(async () => {
        if (!customElements.get('lcm-user')) {
            await import('./slot-card');
        }
    });

    beforeEach(async () => {
        card = document.createElement('lcm-user') as SlotCard;
        card.setConfig({
            code_display: 'masked_with_reveal',
            config_entry_id: 'test-entry',
            name: 'Mom',
            type: 'custom:lcm-user'
        });
        card.hass = createMockHassWithConnection({
            onSubscribe: (callback) => {
                push = callback as (data: SlotCardData) => void;
            }
        });
        document.body.appendChild(card);
        await new Promise((r) => setTimeout(r, 0));
    });

    afterEach(() => {
        card.remove();
    });

    it('has no PIN column in the lock status rows (PR #1203)', async () => {
        // The per-lock PIN column vanished in masked_with_reveal and was
        // removed; the hero shows the PIN once for every lock.
        push({
            active: true,
            conditions: {},
            config_entry_id: 'test-entry',
            config_entry_title: 'Test Config',
            enabled: true,
            locks: [
                {
                    code: PIN,
                    entity_id: 'lock.front',
                    in_sync: true,
                    name: 'Front'
                },
                {
                    code: PIN,
                    entity_id: 'lock.back',
                    in_sync: true,
                    name: 'Back'
                }
            ],
            name: 'Mom',
            pin: PIN,
            slot_num: 1
        });
        await card.updateComplete;
        const root = card.shadowRoot!;

        const rows = [...root.querySelectorAll('.lock-row')];
        expect(rows.map((row) => row.querySelector('.lock-name')?.textContent?.trim())).toEqual([
            'Front',
            'Back'
        ]);
        for (const row of rows) {
            expect(row.querySelector('.lock-code-field')).toBeNull();
            expect(row.textContent).not.toContain(PIN);
        }
    });
});

describe('slot card, rendered from a fresh mount', () => {
    let card: SlotCard | undefined;

    beforeAll(async () => {
        if (!customElements.get('lcm-user')) {
            await import('./slot-card');
        }
    });

    afterEach(() => {
        card?.remove();
        card = undefined;
    });

    let pushData: (data: Partial<SlotCardData>) => void;

    const baseData: SlotCardData = {
        active: true,
        conditions: {},
        config_entry_id: 'test-entry',
        config_entry_title: 'Test Config',
        enabled: true,
        locks: [],
        name: 'Mom',
        pin: PIN,
        slot_num: 1
    };

    async function mount(
        config: Record<string, unknown>,
        data: Partial<SlotCardData>,
        states: Record<string, { attributes?: Record<string, unknown>; state: string }> = {},
        callWS?: (msg: { [key: string]: unknown; type: string }) => unknown
    ): Promise<ShadowRoot> {
        let push: ((data: SlotCardData) => void) | undefined;
        card = document.createElement('lcm-user') as SlotCard;
        card.setConfig({
            config_entry_id: 'test-entry',
            name: 'Mom',
            type: 'custom:lcm-user',
            ...config
        });
        card.hass = createMockHassWithConnection({
            callWS,
            onSubscribe: (callback) => {
                push = callback as (data: SlotCardData) => void;
            },
            states
        });
        pushData = (next) => push!({ ...baseData, ...next });
        document.body.appendChild(card);
        await new Promise((r) => setTimeout(r, 0));
        push!({ ...baseData, ...data });
        await card.updateComplete;
        return card.shadowRoot!;
    }

    it('keeps the PIN masked in "masked" mode even when the reveal flag is set', async () => {
        const root = await mount({ code_display: 'masked' }, { pin: PIN, pin_length: 4 });
        // eslint-disable-next-line no-underscore-dangle -- no public way to set reveal in masked mode
        (card as unknown as { _revealed: boolean })._revealed = true;
        (card as unknown as { requestUpdate: () => void }).requestUpdate();
        await card!.updateComplete;

        const heroPin = root.querySelector('.hero-pin-value');
        expect(heroPin).not.toBeNull();
        expect(heroPin!.textContent).toContain('••••');
        expect(heroPin!.textContent).not.toContain(PIN);
    });

    it('counts only locks in sync in the lock status badge', async () => {
        const root = await mount(
            {},
            {
                locks: [
                    { code: PIN, entity_id: 'lock.a', in_sync: true, name: 'A' },
                    { code: PIN, entity_id: 'lock.b', in_sync: false, name: 'B' },
                    // Not yet known: the card's LockSyncStatus allows null.
                    {
                        code: PIN,
                        entity_id: 'lock.c',
                        in_sync: null as unknown as boolean,
                        name: 'C'
                    }
                ]
            }
        );

        const lockStatusTitle = [...root.querySelectorAll('.collapsible-title')].find((title) =>
            title.textContent?.includes('Lock Status')
        );
        expect(lockStatusTitle).toBeTruthy();
        expect(lockStatusTitle!.querySelector('.collapsible-badge')?.textContent).toBe('1/3');
    });

    describe('recent uses', () => {
        const EVENT = 'event.lcm_slot_1_pin_used';
        const FIRST = '2026-10-27T08:00:00.000+00:00';
        const SECOND = '2026-10-28T09:00:00.000+00:00';
        const STATES = {
            [EVENT]: { state: SECOND },
            'lock.front_door': { attributes: { friendly_name: 'Front Door' }, state: 'locked' }
        };

        function history(...uses: Array<[string, string, string]>): unknown {
            return {
                [EVENT]: uses.map(([when, target, operation]) => {
                    return {
                        a: { credential_type: 'pin', operation, source: target, target },
                        s: when
                    };
                })
            };
        }

        async function settle(): Promise<void> {
            await new Promise((r) => setTimeout(r, 0));
            await card!.updateComplete;
        }

        it('clicking "Last used" lists each use with what it was used against', async () => {
            const root = await mount(
                {},
                { event_entity_id: EVENT, last_used: SECOND, last_used_lock: 'Front Door' },
                STATES,
                (msg) =>
                    msg.type === 'history/history_during_period'
                        ? history(
                              [FIRST, 'lock.front_door', 'unlock'],
                              [SECOND, 'cover.side_gate', 'unknown']
                          )
                        : undefined
            );
            expect(root.querySelector('.use-row')).toBeNull();

            (root.querySelector('.event-row') as HTMLElement).click();
            await settle();

            const rows = [...root.querySelectorAll('.use-row')].map((row) =>
                row.textContent.replace(/\s+/g, ' ').trim()
            );
            expect(rows).toEqual([
                expect.stringContaining('cover.side_gate PIN'),
                expect.stringContaining('Front Door PIN · Unlocked')
            ]);
            expect(root.querySelector('.uses-full-history')?.textContent).toContain('Full history');
            expect(root.querySelector('.event-row')?.getAttribute('aria-expanded')).toBe('true');
        });

        it('a new use while the list is open joins it without reading the history again', async () => {
            const THIRD = '2026-10-28T10:00:00.000+00:00';
            // The recorder has not committed the newest use yet: the history
            // answer stops at the use before it.
            const states: Record<string, { attributes?: Record<string, unknown>; state: string }> =
                {
                    ...STATES,
                    [EVENT]: {
                        attributes: {
                            credential_type: 'pin',
                            operation: 'unlock',
                            target: 'lock.front_door'
                        },
                        state: SECOND
                    }
                };
            const callWS = vi.fn(() => history([FIRST, 'lock.front_door', 'unlock']));
            const root = await mount(
                {},
                { event_entity_id: EVENT, last_used: SECOND, last_used_lock: 'Front Door' },
                states,
                callWS
            );
            (root.querySelector('.event-row') as HTMLElement).click();
            await settle();
            expect(root.querySelectorAll('.use-row')).toHaveLength(2);

            states[EVENT] = {
                attributes: {
                    credential_type: 'rfid',
                    operation: 'lock',
                    target: 'lock.front_door'
                },
                state: THIRD
            };
            pushData({ event_entity_id: EVENT, last_used: THIRD, last_used_lock: 'Front Door' });
            await settle();

            const rows = [...root.querySelectorAll('.use-row')].map((row) => row.textContent);
            expect(rows).toHaveLength(3);
            expect(rows[0]).toContain('RFID · Locked');
            expect(callWS).toHaveBeenCalledTimes(1);
        });

        it('says so when the history cannot be read', async () => {
            const root = await mount(
                {},
                { event_entity_id: EVENT, last_used: SECOND, last_used_lock: 'Front Door' },
                STATES,
                () => {
                    throw new Error('recorder not loaded');
                }
            );
            (root.querySelector('.event-row') as HTMLElement).click();
            await settle();
            expect(root.querySelector('.uses-message')?.textContent).toContain(
                "Couldn't read the history"
            );
        });
    });

    it('shows the last-used row when the event entity is "unknown" (never fired)', async () => {
        const root = await mount(
            {},
            { event_entity_id: 'event.lcm_slot_1_pin_used' },
            { 'event.lcm_slot_1_pin_used': { state: 'unknown' } }
        );

        const row = root.querySelector('.event-row');
        expect(row).not.toBeNull();
        expect(row!.textContent).toContain('Never used');
    });
});
