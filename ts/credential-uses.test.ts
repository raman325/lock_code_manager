import { describe, expect, it } from 'vitest';

import {
    CREDENTIAL_USE_LIMIT,
    HistoryResponse,
    buildCredentialUsesQuery,
    describeCredentialUse,
    parseCredentialUses
} from './credential-uses';
import { HomeAssistant } from './ha_type_stubs';

const EVENT = 'event.mock_title_alice_credential_used';

function use(
    when: string,
    target = 'lock.front_door',
    extra: Record<string, unknown> = {}
): { a: Record<string, unknown>; s: string } {
    return {
        a: { credential_type: 'pin', operation: 'unlock', target, ...extra },
        s: when
    };
}

const STATES = {
    'lock.front_door': { attributes: { friendly_name: 'Front Door' }, state: 'locked' }
} as unknown as HomeAssistant['states'];

describe('buildCredentialUsesQuery', () => {
    it('asks for every state of the one entity, attributes included', () => {
        const now = new Date('2026-10-28T12:00:00Z');
        const query = buildCredentialUsesQuery(EVENT, now);
        expect(query).toMatchObject({
            entity_ids: [EVENT],
            minimal_response: false,
            no_attributes: false,
            significant_changes_only: false,
            type: 'history/history_during_period'
        });
        expect(Date.parse(query.start_time as string)).toBeLessThan(now.getTime());
    });
});

describe('parseCredentialUses', () => {
    it('lists uses newest first, naming the target by its friendly name', () => {
        const response: HistoryResponse = {
            [EVENT]: [
                use('2026-10-27T08:00:00.000+00:00'),
                use('2026-10-28T09:00:00.000+00:00', 'cover.side_gate')
            ]
        };
        const uses = parseCredentialUses(response, EVENT, STATES);
        expect(uses.map((u) => u.targetName)).toEqual(['cover.side_gate', 'Front Door']);
        expect(uses[1].when).toBe('2026-10-27T08:00:00.000+00:00');
    });

    it('skips states that are not uses and lists a restored use once', () => {
        const response: HistoryResponse = {
            [EVENT]: [
                { s: 'unknown' },
                use('2026-10-27T08:00:00.000+00:00'),
                { s: 'unavailable' },
                // Home Assistant restarted: the restored state repeats the use.
                use('2026-10-27T08:00:00.000+00:00')
            ]
        };
        expect(parseCredentialUses(response, EVENT, STATES)).toHaveLength(1);
    });

    it('skips a use recorded before 6.0, whose payload named no target', () => {
        const response: HistoryResponse = {
            // The old payload's event_type was the lock, not the kind.
            [EVENT]: [{ a: { event_type: 'lock.front_door' }, s: '2026-09-01T08:00:00.000+00:00' }]
        };
        expect(parseCredentialUses(response, EVENT, STATES)).toEqual([]);
    });

    it('keeps only the most recent uses', () => {
        const rows = Array.from({ length: CREDENTIAL_USE_LIMIT + 5 }, (_, i) =>
            use(new Date(Date.UTC(2026, 9, 1, i)).toISOString())
        );
        const uses = parseCredentialUses({ [EVENT]: rows }, EVENT, STATES);
        expect(uses).toHaveLength(CREDENTIAL_USE_LIMIT);
        expect(uses[0].when).toBe(rows[rows.length - 1].s);
    });

    it('never reads the source, which can hold the typed code', () => {
        const states = {
            ...STATES,
            'sensor.keypad': { attributes: { friendly_name: '4815' }, state: '4815' }
        } as unknown as HomeAssistant['states'];
        const response: HistoryResponse = {
            [EVENT]: [
                use('2026-10-27T08:00:00.000+00:00', 'lock.front_door', { source: 'sensor.keypad' })
            ]
        };
        const [parsed] = parseCredentialUses(response, EVENT, states);
        expect(JSON.stringify(parsed)).not.toContain('4815');
    });

    it('returns nothing for a missing or empty response', () => {
        expect(parseCredentialUses(undefined, EVENT)).toEqual([]);
        expect(parseCredentialUses({}, EVENT)).toEqual([]);
    });
});

describe('describeCredentialUse', () => {
    it('names the kind and, when known, what the device did', () => {
        const [parsed] = parseCredentialUses(
            { [EVENT]: [use('2026-10-27T08:00:00.000+00:00')] },
            EVENT,
            STATES
        );
        expect(describeCredentialUse(parsed)).toBe('PIN · Unlocked');
        expect(describeCredentialUse({ ...parsed, operation: 'unknown' })).toBe('PIN');
        expect(
            describeCredentialUse({ ...parsed, credentialType: 'fingerprint', operation: 'lock' })
        ).toBe('Fingerprint · Locked');
    });
});
