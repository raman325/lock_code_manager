import { HomeAssistant } from './ha_type_stubs';

/**
 * A user's recent credential uses, read from the recorder's history of their
 * credential-used event entity.
 *
 * Each recorded use is one state of that entity: the state is the time of the
 * use and the attributes are the use's payload. Nothing new is stored for
 * this; the history Home Assistant already keeps is read back.
 */

/** How many uses the card lists. */
export const CREDENTIAL_USE_LIMIT = 10;

/** How far back the card asks; the recorder may have kept less. */
const CREDENTIAL_USE_WINDOW_DAYS = 30;

export interface CredentialUse {
    /** Kind of credential presented: `pin`, `rfid`, `fingerprint`, ... */
    credentialType: string;
    /** What the device did: `unlock`, `lock` or `unknown` */
    operation: string;
    /** Entity the credential was used against */
    target: string;
    /** Display name of the target, falling back to its entity ID */
    targetName: string;
    /** When the use was recorded (ISO timestamp, the event entity's state) */
    when: string;
}

/** A state in Home Assistant's compressed history format. */
export interface CompressedState {
    a?: Record<string, unknown>;
    s: string;
}

export type HistoryResponse = Record<string, CompressedState[] | undefined>;

const OPERATION_LABELS: Record<string, string> = {
    lock: 'Locked',
    unlock: 'Unlocked'
};

/**
 * The history stream for one event entity, attributes included.
 *
 * A stream rather than a one-off read: Home Assistant sends what the recorder
 * holds, then the states it had not yet committed, then every new state as
 * it happens. A one-off read misses uses still waiting on the recorder's next
 * commit, and every use after it.
 */
export function buildCredentialUsesStream(
    eventEntityId: string,
    now: Date = new Date()
): { [key: string]: unknown; type: string } {
    const start = new Date(now.getTime() - CREDENTIAL_USE_WINDOW_DAYS * 24 * 60 * 60 * 1000);
    return {
        entity_ids: [eventEntityId],
        include_start_time_state: false,
        minimal_response: false,
        no_attributes: false,
        // Every state, not only those whose state changed. The entity also
        // writes attribute-only states (its event_types move as locks are
        // probed), which repeat the last use's time; `mergeUseRows` keys
        // rows by time, so each use is still listed once.
        significant_changes_only: false,
        start_time: start.toISOString(),
        type: 'history/stream'
    };
}

/** One message on the history stream. */
export interface HistoryStreamMessage {
    states?: HistoryResponse;
}

/**
 * Whether a recorded state is a use.
 *
 * A state that is not a timestamp (`unknown`, `unavailable`) is not, and
 * neither is one recorded before 6.0, whose payload named no `target`.
 */
function isUse(row: CompressedState): boolean {
    return !Number.isNaN(Date.parse(row.s)) && typeof row.a?.target === 'string';
}

/**
 * Fold rows from the stream into the rows kept, holding only what is listed.
 *
 * Keeps the newest `CREDENTIAL_USE_LIMIT` uses, one row per use time (a later
 * row for the same time wins), so the kept rows never grow past what the
 * card shows however long it stays open.
 */
export function mergeUseRows(
    kept: CompressedState[],
    incoming: CompressedState[]
): CompressedState[] {
    const byTime = new Map<string, CompressedState>();
    for (const row of [...kept, ...incoming]) {
        if (isUse(row)) byTime.set(row.s, row);
    }
    return [...byTime.values()]
        .sort((a, b) => Date.parse(b.s) - Date.parse(a.s))
        .slice(0, CREDENTIAL_USE_LIMIT);
}

/**
 * Turn the history response into uses, newest first.
 *
 * Only uses are listed (see `isUse`). A restart writes the last use's state
 * again, so uses are keyed by their time and each is listed once.
 *
 * Only the target is looked up. The source can be a keypad entity whose state
 * is the code that was typed, so it is never read.
 */
export function parseCredentialUses(
    response: HistoryResponse | undefined,
    eventEntityId: string,
    states: HomeAssistant['states'] = {}
): CredentialUse[] {
    const byTime = new Map<string, CredentialUse>();
    const uses = (response?.[eventEntityId] ?? []).filter(isUse);
    for (const row of uses) {
        const attributes = row.a ?? {};
        const target = attributes.target as string;
        const friendlyName = states[target]?.attributes?.friendly_name;
        byTime.set(row.s, {
            credentialType: String(attributes.credential_type ?? attributes.event_type ?? ''),
            operation: String(attributes.operation ?? 'unknown'),
            target,
            targetName: typeof friendlyName === 'string' && friendlyName ? friendlyName : target,
            when: row.s
        });
    }
    return [...byTime.values()]
        .sort((a, b) => Date.parse(b.when) - Date.parse(a.when))
        .slice(0, CREDENTIAL_USE_LIMIT);
}

/**
 * One line saying what was used and, when the device said, what it did.
 *
 * `formatKind` turns a kind into its translated label (the event entity's
 * `event_type` translations), so the card names a kind the way the entity's
 * own history does.
 */
export function describeCredentialUse(
    use: CredentialUse,
    formatKind: (kind: string) => string = (kind) => kind
): string {
    const kind = formatKind(use.credentialType);
    const operation = OPERATION_LABELS[use.operation];
    return operation ? `${kind} · ${operation}` : kind;
}
