/**
 * Getting Home Assistant's own elements into a card.
 *
 * HA splits its frontend into chunks and a Lovelace view pulls in only
 * some of them. An element HA has not registered is not an error: the
 * browser treats the tag as unknown and renders nothing, so a control
 * built from one silently disappears. The Lovelace *editor* loads the
 * form components eagerly, which is why the same markup behaves in a card
 * editor and vanishes in a card.
 *
 * `ha-entity-picker` is worth the trouble because nothing we can write by
 * hand replaces it -- picking an entity means searching every entity in
 * the instance. The rest of HA's form components are not: a plain input
 * styled to match is smaller and cannot vanish.
 */

/** The subset of HA's `loadCardHelpers()` return value the cards depend on. */
export interface CardHelpers {
    createCardElement: (config: { entities: string[]; type: string }) => HTMLElement & {
        constructor: { getConfigElement?: () => Promise<unknown> };
    };
    createRowElement: (config: { entity: string }) => HTMLElement;
}

declare global {
    interface Window {
        loadCardHelpers?: () => Promise<CardHelpers>;
    }
}

/**
 * Force `ha-entity-picker` to register, by asking for the entities card's
 * config element -- which uses the picker internally, so requesting it
 * registers the picker as a side effect.
 *
 * Idempotent, and safe to call when it cannot work: a failure leaves the
 * picker unregistered, which callers must handle by rendering something
 * else. Never let an `ha-entity-picker` tag be the only thing on screen.
 */
export async function ensureEntityPickerLoaded(): Promise<boolean> {
    if (customElements.get('ha-entity-picker')) return true;
    const loadHelpers = window.loadCardHelpers;
    if (!loadHelpers) return false;
    try {
        const helpers = await loadHelpers();
        const cardElement = helpers.createCardElement({ entities: [], type: 'entities' });
        await cardElement.constructor.getConfigElement?.();
    } catch (err) {
        // eslint-disable-next-line no-console
        console.warn('lock_code_manager: failed to lazy-load ha-entity-picker', err);
    }
    return Boolean(customElements.get('ha-entity-picker'));
}

/** The domain half of an entity id -- `lock` in `lock.front_door`. */
function entityDomain(entityId: string): string {
    return entityId.split('.')[0];
}

/**
 * True when the entity is an actual lock.
 *
 * A config entry's lock list also carries credential readers, which are
 * distinguished from locks only by their domain.
 */
export function isLockEntity(entityId: string): boolean {
    return entityDomain(entityId) === 'lock';
}

/** Entity domains that can gate a PIN. */
export const CONDITION_DOMAINS = [
    'calendar',
    'schedule',
    'binary_sensor',
    'switch',
    'input_boolean'
] as const;

/** True when the entity belongs to a domain that can gate a PIN. */
export function isConditionEntity(entityId: string): boolean {
    return (CONDITION_DOMAINS as readonly string[]).includes(entityDomain(entityId));
}
