import { describe, expect, it } from 'vitest';

import {
    compareAndSortEntities,
    createLockCodeManagerEntity,
    getEntityDisplayName
} from './generate-view.internal';
import { EntityRegistryEntry } from './ha_type_stubs';
import { ConfigEntryJSONFragment, LockCodeManagerEntityEntry } from './types';

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
        // Note: CODE_SENSOR_KEY is 'code', not 'code_slot'
        const entityA = createEntity(1, 'code', 'lock.alpha');
        const entityB = createEntity(1, 'code', 'lock.beta');

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
