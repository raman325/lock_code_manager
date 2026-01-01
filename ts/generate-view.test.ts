import { describe, expect, it } from 'vitest';

import { EntityRegistryEntry } from './ha_type_stubs';
import { ConfigEntryJSONFragment, LockCodeManagerEntityEntry } from './types';

// Import the functions we want to test
// Note: These are currently private in generate-view.ts
// We'll need to export them to test, or test through the public API

// For now, we'll recreate the logic here to test in isolation
// TODO: Export these functions from generate-view.ts for proper testing

function createLockCodeManagerEntity(entity: EntityRegistryEntry): LockCodeManagerEntityEntry {
    const split = entity.unique_id.split('|');
    return {
        ...entity,
        key: split[2],
        lockEntityId: split[3],
        slotNum: parseInt(split[1], 10)
    };
}

function capitalize(str: string): string {
    return str.charAt(0).toUpperCase() + str.slice(1);
}

function getEntityDisplayName(
    configEntry: ConfigEntryJSONFragment,
    entity: LockCodeManagerEntityEntry
): string {
    const baseName = entity.name ?? entity.original_name ?? '';
    const configTitle = configEntry.title ?? '';
    let name = baseName.replace(new RegExp(`^Code slot ${entity.slotNum}\\s*`, 'i'), '').trim();
    if (configTitle && name.toLowerCase().startsWith(configTitle.toLowerCase())) {
        name = name.slice(configTitle.length).trim();
    }
    if (!name) {
        name = baseName || entity.entity_id;
    }
    return capitalize(name);
}

const KEY_ORDER = [
    'enabled',
    'name',
    'pin',
    'active',
    'code_slot',
    'in_sync',
    'code_slot_event',
    'override',
    'include_code_in_event_log',
    'notify_on_use',
    'number_of_uses',
    'access_schedule',
    'access_count'
];

const CODE_EVENT_KEY = 'code_slot_event';
const CODE_SENSOR_KEY = 'code_slot';
const IN_SYNC_KEY = 'in_sync';

function compareAndSortEntities(
    entityA: LockCodeManagerEntityEntry,
    entityB: LockCodeManagerEntityEntry
): -1 | 1 {
    // sort by slot number
    if (entityA.slotNum < entityB.slotNum) return -1;
    if (entityA.slotNum > entityB.slotNum) return 1;
    // sort by key order
    if (KEY_ORDER.indexOf(entityA.key) < KEY_ORDER.indexOf(entityB.key)) return -1;
    if (KEY_ORDER.indexOf(entityA.key) > KEY_ORDER.indexOf(entityB.key)) return 1;
    // sort code sensors alphabetically based on the lock entity_id
    if (
        entityA.key === entityB.key &&
        [CODE_EVENT_KEY, CODE_SENSOR_KEY, IN_SYNC_KEY].includes(entityA.key) &&
        entityA.lockEntityId < entityB.lockEntityId
    )
        return -1;
    return 1;
}

describe('createLockCodeManagerEntity', () => {
    it('parses unique_id correctly', () => {
        const entity: EntityRegistryEntry = {
            entity_id: 'switch.test_slot_1_enabled',
            unique_id: 'config123|1|enabled',
            name: 'Test',
            original_name: 'Original'
        } as EntityRegistryEntry;

        const result = createLockCodeManagerEntity(entity);

        expect(result.slotNum).toBe(1);
        expect(result.key).toBe('enabled');
        expect(result.lockEntityId).toBeUndefined();
        expect(result.entity_id).toBe('switch.test_slot_1_enabled');
    });

    it('parses unique_id with lock entity_id', () => {
        const entity: EntityRegistryEntry = {
            entity_id: 'sensor.test_slot_1_code',
            unique_id: 'config123|1|code_slot|lock.front_door',
            name: null,
            original_name: 'Code Slot 1'
        } as EntityRegistryEntry;

        const result = createLockCodeManagerEntity(entity);

        expect(result.slotNum).toBe(1);
        expect(result.key).toBe('code_slot');
        expect(result.lockEntityId).toBe('lock.front_door');
    });

    it('handles multi-digit slot numbers', () => {
        const entity: EntityRegistryEntry = {
            entity_id: 'switch.test_slot_42_enabled',
            unique_id: 'config123|42|enabled',
            name: 'Slot 42',
            original_name: null
        } as EntityRegistryEntry;

        const result = createLockCodeManagerEntity(entity);

        expect(result.slotNum).toBe(42);
    });
});

describe('getEntityDisplayName', () => {
    const mockConfigEntry: ConfigEntryJSONFragment = {
        title: 'Test Config',
        entry_id: 'test123',
        domain: 'lock_code_manager',
        disabled_by: '',
        pref_disable_new_entities: false,
        pref_disable_polling: false,
        reason: null,
        source: 'user',
        state: 'loaded',
        supports_options: true,
        supports_remove_device: false,
        supports_unload: true
    };

    it('removes slot prefix from name', () => {
        const entity: LockCodeManagerEntityEntry = {
            entity_id: 'switch.test',
            unique_id: 'test|1|enabled',
            name: 'Code slot 1 enabled',
            original_name: 'Code slot 1 enabled',
            key: 'enabled',
            slotNum: 1
        } as LockCodeManagerEntityEntry;

        expect(getEntityDisplayName(mockConfigEntry, entity)).toBe('Enabled');
    });

    it('removes config title prefix from name', () => {
        const entity: LockCodeManagerEntityEntry = {
            entity_id: 'switch.test',
            unique_id: 'test|1|enabled',
            name: 'Code slot 1 Test Config enabled',
            original_name: null,
            key: 'enabled',
            slotNum: 1
        } as LockCodeManagerEntityEntry;

        expect(getEntityDisplayName(mockConfigEntry, entity)).toBe('Enabled');
    });

    it('falls back to entity_id if name is empty', () => {
        const entity: LockCodeManagerEntityEntry = {
            entity_id: 'switch.my_switch',
            unique_id: 'test|1|enabled',
            name: null,
            original_name: null,
            key: 'enabled',
            slotNum: 1
        } as LockCodeManagerEntityEntry;

        expect(getEntityDisplayName(mockConfigEntry, entity)).toBe('Switch.my_switch');
    });

    it('uses original_name when name is null', () => {
        const entity: LockCodeManagerEntityEntry = {
            entity_id: 'switch.test',
            unique_id: 'test|1|enabled',
            name: null,
            original_name: 'Code slot 1 Override',
            key: 'override',
            slotNum: 1
        } as LockCodeManagerEntityEntry;

        expect(getEntityDisplayName(mockConfigEntry, entity)).toBe('Override');
    });
});

describe('compareAndSortEntities', () => {
    const createEntity = (
        slotNum: number,
        key: string,
        lockEntityId?: string
    ): LockCodeManagerEntityEntry =>
        ({
            entity_id: `test.entity_${slotNum}_${key}`,
            unique_id: `test|${slotNum}|${key}${lockEntityId ? `|${lockEntityId}` : ''}`,
            slotNum,
            key,
            lockEntityId
        }) as LockCodeManagerEntityEntry;

    it('sorts by slot number first', () => {
        const entityA = createEntity(1, 'enabled');
        const entityB = createEntity(2, 'enabled');

        expect(compareAndSortEntities(entityA, entityB)).toBe(-1);
        expect(compareAndSortEntities(entityB, entityA)).toBe(1);
    });

    it('sorts by key order within same slot', () => {
        const entityA = createEntity(1, 'enabled');
        const entityB = createEntity(1, 'pin');

        expect(compareAndSortEntities(entityA, entityB)).toBe(-1);
        expect(compareAndSortEntities(entityB, entityA)).toBe(1);
    });

    it('sorts code sensors by lock entity_id', () => {
        const entityA = createEntity(1, 'code_slot', 'lock.alpha');
        const entityB = createEntity(1, 'code_slot', 'lock.beta');

        expect(compareAndSortEntities(entityA, entityB)).toBe(-1);
        expect(compareAndSortEntities(entityB, entityA)).toBe(1);
    });

    it('sorts in_sync entities by lock entity_id', () => {
        const entityA = createEntity(1, 'in_sync', 'lock.front');
        const entityB = createEntity(1, 'in_sync', 'lock.back');

        expect(compareAndSortEntities(entityA, entityB)).toBe(1);
        expect(compareAndSortEntities(entityB, entityA)).toBe(-1);
    });
});
