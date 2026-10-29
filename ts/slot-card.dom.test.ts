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
        const BOB_EVENT = 'event.lcm_slot_1_bob_pin_used';
        const FIRST = '2026-10-27T08:00:00.000+00:00';
        const SECOND = '2026-10-28T09:00:00.000+00:00';
        const THIRD = '2026-10-28T10:00:00.000+00:00';
        const STATES = {
            [BOB_EVENT]: { state: SECOND },
            [EVENT]: { state: SECOND },
            'lock.front_door': { attributes: { friendly_name: 'Front Door' }, state: 'locked' }
        };

        interface Stream {
            msg: { [key: string]: unknown; type: string };
            send: (rows: Array<[string, string, string, string?]>) => void;
            unsub: ReturnType<typeof vi.fn>;
        }

        let streams: Stream[];
        let pushSlot: (data: Partial<SlotCardData>) => void;

        /**
         * Mount the card on a connection whose history streams the test drives.
         * `failStream` makes opening a stream fail, as when the recorder is
         * not loaded.
         */
        async function mountWithStreams(
            data: Partial<SlotCardData>,
            failStream = false
        ): Promise<ShadowRoot> {
            streams = [];
            const labels: Record<string, string> = { pin: 'PIN', rfid: 'RFID' };
            let slotCallback: ((data: SlotCardData) => void) | undefined;
            const hass = createMockHassWithConnection({ states: STATES });
            Object.assign(hass, {
                // Stands in for Home Assistant translating the event_type.
                formatEntityAttributeValue: (_state: unknown, _attribute: string, value: string) =>
                    labels[value]
            });
            Object.assign(hass.connection, {
                subscribeMessage: vi.fn(
                    (
                        callback: (message: unknown) => void,
                        msg: { [key: string]: unknown; type: string }
                    ) => {
                        if (msg.type !== 'history/stream') {
                            slotCallback = callback as (data: SlotCardData) => void;
                            return Promise.resolve(() => {});
                        }
                        if (failStream) return Promise.reject(new Error('recorder not loaded'));
                        const unsub = vi.fn();
                        const entityId = (msg.entity_ids as string[])[0];
                        streams.push({
                            msg,
                            send: (rows) =>
                                callback({
                                    states: {
                                        [entityId]: rows.map(([when, target, kind, operation]) => {
                                            return {
                                                a: {
                                                    credential_type: kind,
                                                    operation,
                                                    source: target,
                                                    target
                                                },
                                                s: when
                                            };
                                        })
                                    }
                                }),
                            unsub
                        });
                        return Promise.resolve(unsub);
                    }
                )
            });
            card = document.createElement('lcm-user') as SlotCard;
            card.setConfig({ config_entry_id: 'test-entry', name: 'Mom', type: 'custom:lcm-user' });
            card.hass = hass;
            document.body.appendChild(card);
            await settle();
            pushSlot = (next) => slotCallback!({ ...baseData, ...next });
            pushSlot(data);
            await settle();
            return card.shadowRoot!;
        }

        async function settle(): Promise<void> {
            await new Promise((r) => setTimeout(r, 0));
            await card?.updateComplete;
        }

        function rows(root: ShadowRoot): string[] {
            return [...root.querySelectorAll('.use-row')].map((row) =>
                row.textContent.replace(/\s+/g, ' ').trim()
            );
        }

        const USED = { event_entity_id: EVENT, last_used: SECOND, last_used_lock: 'Front Door' };

        it('clicking "Last used" streams each use with what it was used against', async () => {
            const root = await mountWithStreams(USED);
            expect(root.querySelector('.use-row')).toBeNull();

            (root.querySelector('.event-row') as HTMLElement).click();
            await settle();
            expect(streams).toHaveLength(1);
            expect(streams[0].msg).toMatchObject({ entity_ids: [EVENT], type: 'history/stream' });
            expect(root.querySelector('.uses-message')?.textContent).toContain('Loading');

            streams[0].send([
                [FIRST, 'lock.front_door', 'pin', 'unlock'],
                [SECOND, 'cover.side_gate', 'pin', 'unknown']
            ]);
            await settle();

            expect(rows(root)).toEqual([
                expect.stringContaining('cover.side_gate PIN'),
                expect.stringContaining('Front Door PIN · Unlocked')
            ]);
            expect(root.querySelector('.uses-full-history')?.textContent).toContain('Full history');
            expect(root.querySelector('.event-row')?.getAttribute('aria-expanded')).toBe('true');
        });

        it('uses the stream sends after the history join the list', async () => {
            const root = await mountWithStreams(USED);
            (root.querySelector('.event-row') as HTMLElement).click();
            await settle();
            streams[0].send([[FIRST, 'lock.front_door', 'pin', 'unlock']]);
            // Then the states the recorder had not committed, and a live one.
            streams[0].send([[SECOND, 'lock.front_door', 'pin', 'unlock']]);
            streams[0].send([[THIRD, 'lock.front_door', 'rfid', 'lock']]);
            await settle();

            const listed = rows(root);
            expect(listed).toHaveLength(3);
            expect(listed[0]).toContain('RFID · Locked');
        });

        it('collapsing closes the stream, and expanding opens a fresh one', async () => {
            const root = await mountWithStreams(USED);
            const row = root.querySelector('.event-row') as HTMLElement;
            row.click();
            await settle();
            streams[0].send([[FIRST, 'lock.front_door', 'pin', 'unlock']]);
            row.click();
            await settle();
            expect(streams[0].unsub).toHaveBeenCalledTimes(1);

            row.click();
            await settle();
            expect(streams).toHaveLength(2);
            // Nothing from the closed stream is shown before the new one answers.
            expect(root.querySelector('.use-row')).toBeNull();
        });

        it('a card taken off the page closes its stream', async () => {
            const root = await mountWithStreams(USED);
            (root.querySelector('.event-row') as HTMLElement).click();
            await settle();
            card!.remove();
            await settle();
            expect(streams[0].unsub).toHaveBeenCalledTimes(1);
        });

        it("switching the card to another user drops the previous user's uses", async () => {
            const root = await mountWithStreams(USED);
            (root.querySelector('.event-row') as HTMLElement).click();
            await settle();
            streams[0].send([[FIRST, 'lock.front_door', 'pin', 'unlock']]);
            await settle();
            expect(rows(root)).toHaveLength(1);

            card!.setConfig({
                config_entry_id: 'test-entry',
                name: 'Bob',
                type: 'custom:lcm-user'
            });
            await settle();

            expect(streams[0].unsub).toHaveBeenCalledTimes(1);
            expect(root.querySelector('.use-row')).toBeNull();
            expect(root.querySelector('.event-row')?.getAttribute('aria-expanded')).not.toBe(
                'true'
            );
        });

        it('follows the new entity when the slot comes to name someone else', async () => {
            const root = await mountWithStreams(USED);
            (root.querySelector('.event-row') as HTMLElement).click();
            await settle();
            streams[0].send([[FIRST, 'lock.front_door', 'pin', 'unlock']]);
            await settle();

            pushSlot({ ...USED, event_entity_id: BOB_EVENT, name: 'Bob' });
            await settle();

            expect(streams[0].unsub).toHaveBeenCalledTimes(1);
            expect(streams[1].msg).toMatchObject({ entity_ids: [BOB_EVENT] });
            // A late message on the old stream is not the new person's.
            streams[0].send([[SECOND, 'lock.front_door', 'pin', 'unlock']]);
            await settle();
            expect(root.querySelector('.use-row')).toBeNull();
        });

        it('says so when the history cannot be streamed', async () => {
            const root = await mountWithStreams(USED, true);
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
