import { afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest';

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
