import { describe, expect, it } from 'vitest';

import {
    CREDENTIAL_USE_LIMIT,
    buildCredentialUsesStream,
    describeCredentialUse,
    mergeUseRows,
    toCredentialUses
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

describe('buildCredentialUsesStream', () => {
    it('streams every state change of the one entity, attributes included', () => {
        const now = new Date('2026-10-28T12:00:00Z');
        const msg = buildCredentialUsesStream(EVENT, now);
        expect(msg).toMatchObject({
            entity_ids: [EVENT],
            minimal_response: false,
            no_attributes: false,
            significant_changes_only: true,
            type: 'history/stream'
        });
        expect(Date.parse(msg.start_time as string)).toBeLessThan(now.getTime());
    });
});

describe('mergeUseRows', () => {
    it('keeps only uses, one per time, newest first, and never more than are listed', () => {
        const many = Array.from({ length: CREDENTIAL_USE_LIMIT + 5 }, (_, i) =>
            use(new Date(Date.UTC(2026, 9, 1, i)).toISOString())
        );
        const kept = mergeUseRows([{ s: 'unknown' }, ...many.slice(0, 3)], many.slice(3));
        expect(kept).toHaveLength(CREDENTIAL_USE_LIMIT);
        expect(kept[0].s).toBe(many[many.length - 1].s);
    });

    it('lists an attribute-only repeat of a use once, as its latest row', () => {
        const first = use('2026-10-27T08:00:00.000+00:00');
        const repeat = use('2026-10-27T08:00:00.000+00:00', 'lock.front_door', {
            event_types: ['pin', 'rfid']
        });
        expect(mergeUseRows([first], [repeat])).toEqual([repeat]);
    });
});

describe('mergeUseRows, which states are uses', () => {
    it('skips states that are not uses and lists a restored use once', () => {
        const rows = mergeUseRows(
            [],
            [
                { s: 'unknown' },
                use('2026-10-27T08:00:00.000+00:00'),
                { s: 'unavailable' },
                // Home Assistant restarted: the restored state repeats the use.
                use('2026-10-27T08:00:00.000+00:00')
            ]
        );
        expect(rows).toHaveLength(1);
    });

    it('skips a use recorded before 6.0, whose payload named no target', () => {
        // The old payload's event_type was the lock, not the kind.
        const old = { a: { event_type: 'lock.front_door' }, s: '2026-09-01T08:00:00.000+00:00' };
        expect(mergeUseRows([], [old])).toEqual([]);
    });
});

describe('toCredentialUses', () => {
    it('names each target by its friendly name, its entity ID when it has none', () => {
        const rows = mergeUseRows(
            [],
            [
                use('2026-10-27T08:00:00.000+00:00'),
                use('2026-10-28T09:00:00.000+00:00', 'cover.side_gate')
            ]
        );
        const uses = toCredentialUses(rows, STATES);
        expect(uses.map((u) => u.targetName)).toEqual(['cover.side_gate', 'Front Door']);
        expect(uses[1].when).toBe('2026-10-27T08:00:00.000+00:00');
    });

    it('never reads the source, which can hold the typed code', () => {
        const states = {
            ...STATES,
            'sensor.keypad': { attributes: { friendly_name: '4815' }, state: '4815' }
        } as unknown as HomeAssistant['states'];
        const [parsed] = toCredentialUses(
            [use('2026-10-27T08:00:00.000+00:00', 'lock.front_door', { source: 'sensor.keypad' })],
            states
        );
        expect(JSON.stringify(parsed)).not.toContain('4815');
    });
});

describe('describeCredentialUse', () => {
    it('names the kind and, when known, what the device did', () => {
        const [parsed] = toCredentialUses([use('2026-10-27T08:00:00.000+00:00')], STATES);
        const labels: Record<string, string> = { fingerprint: 'Fingerprint', pin: 'PIN' };
        const formatKind = (kind: string): string => labels[kind];
        expect(describeCredentialUse(parsed, formatKind)).toBe('PIN · Unlocked');
        expect(describeCredentialUse({ ...parsed, operation: 'unknown' }, formatKind)).toBe('PIN');
        expect(describeCredentialUse(parsed)).toBe('pin · Unlocked');
        expect(
            describeCredentialUse(
                { ...parsed, credentialType: 'fingerprint', operation: 'lock' },
                formatKind
            )
        ).toBe('Fingerprint · Locked');
    });
});
