import { describe, expect, it } from 'vitest';

import { generateView } from './generate-view';
import {
    ACTIVE_KEY,
    CODE_EVENT_KEY,
    CODE_SENSOR_KEY,
    CONDITION_KEYS,
    DIVIDER_CARD,
    IN_SYNC_KEY,
    compareAndSortEntities,
    createLockCodeManagerEntity,
    generateEntityCards,
    generateNewSlotCard,
    generateSlotCard,
    getEntityDisplayName,
    getSlotMapping,
    maybeGenerateFoldEntityRowCard,
    maybeGenerateFoldEntityRowConditionCard
} from './generate-view.internal';
import { EntityRegistryEntry, LovelaceResource } from './ha_type_stubs';
import { createMockHass } from './test/mock-hass';
import {
    ConfigEntryJSONFragment,
    LockCodeManagerConfigEntryData,
    LockCodeManagerEntityEntry,
    SlotMapping
} from './types';

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

// Shared test helpers and fixtures
const mockConfigEntry: ConfigEntryJSONFragment = {
    disabled_by: '',
    domain: 'lock_code_manager',
    entry_id: 'test123',
    pref_disable_new_entities: false,
    pref_disable_polling: false,
    reason: null,
    source: 'user',
    state: 'loaded',
    supports_options: true,
    supports_remove_device: false,
    supports_unload: true,
    title: 'Test Config'
};

function createTestEntity(
    slotNum: number,
    key: string,
    entityId: string,
    lockEntityId?: string
): LockCodeManagerEntityEntry {
    return {
        entity_id: entityId,
        key,
        lockEntityId,
        name: `Code slot ${slotNum} ${key}`,
        original_name: `Code slot ${slotNum} ${key}`,
        slotNum,
        unique_id: `config|${slotNum}|${key}${lockEntityId ? `|${lockEntityId}` : ''}`
    } as LockCodeManagerEntityEntry;
}

describe('generateEntityCards', () => {
    it('returns entity cards with names from getEntityDisplayName', () => {
        const hass = createMockHass();
        const entities = [createTestEntity(1, 'enabled', 'switch.slot_1_enabled')];

        const result = generateEntityCards(hass, mockConfigEntry, entities);

        expect(result).toHaveLength(1);
        expect(result[0]).toEqual({
            entity: 'switch.slot_1_enabled',
            name: 'Enabled'
        });
    });

    it('uses lock friendly_name for code sensor entities', () => {
        const hass = createMockHass({
            states: {
                'lock.front_door': {
                    attributes: { friendly_name: 'Front Door Lock' },
                    state: 'locked'
                }
            }
        });
        const entities = [
            createTestEntity(1, CODE_SENSOR_KEY, 'sensor.slot_1_code', 'lock.front_door')
        ];

        const result = generateEntityCards(hass, mockConfigEntry, entities);

        expect(result[0].name).toBe('Front Door Lock');
    });

    it('uses lock friendly_name for in_sync entities', () => {
        const hass = createMockHass({
            states: {
                'lock.back_door': {
                    attributes: { friendly_name: 'Back Door' },
                    state: 'locked'
                }
            }
        });
        const entities = [
            createTestEntity(1, IN_SYNC_KEY, 'binary_sensor.slot_1_in_sync', 'lock.back_door')
        ];

        const result = generateEntityCards(hass, mockConfigEntry, entities);

        expect(result[0].name).toBe('Back Door');
    });

    it('falls back to lock entity_id when friendly_name is missing', () => {
        const hass = createMockHass({ states: {} });
        const entities = [
            createTestEntity(1, CODE_SENSOR_KEY, 'sensor.slot_1_code', 'lock.front_door')
        ];

        const result = generateEntityCards(hass, mockConfigEntry, entities);

        expect(result[0].name).toBe('lock.front_door');
    });

    it('handles multiple entities', () => {
        const hass = createMockHass();
        const entities = [
            createTestEntity(1, 'enabled', 'switch.slot_1_enabled'),
            createTestEntity(1, 'pin', 'text.slot_1_pin'),
            createTestEntity(1, 'name', 'text.slot_1_name')
        ];

        const result = generateEntityCards(hass, mockConfigEntry, entities);

        expect(result).toHaveLength(3);
        expect(result.map((c) => c.entity)).toEqual([
            'switch.slot_1_enabled',
            'text.slot_1_pin',
            'text.slot_1_name'
        ]);
    });
});

describe('getSlotMapping', () => {
    const configEntryData: LockCodeManagerConfigEntryData = {
        locks: ['lock.front', 'lock.back'],
        slots: { 1: 'calendar.slot_1', 2: null }
    };

    it('separates entities by category', () => {
        const entities: LockCodeManagerEntityEntry[] = [
            createTestEntity(1, 'enabled', 'switch.enabled'),
            createTestEntity(1, 'pin', 'text.pin'),
            createTestEntity(1, ACTIVE_KEY, 'binary_sensor.active'),
            createTestEntity(1, CODE_EVENT_KEY, 'event.code_used'),
            createTestEntity(1, CODE_SENSOR_KEY, 'sensor.code', 'lock.front'),
            createTestEntity(1, IN_SYNC_KEY, 'binary_sensor.in_sync', 'lock.front'),
            createTestEntity(1, CONDITION_KEYS[0], 'switch.condition')
        ];
        const result = getSlotMapping(1, entities, configEntryData);

        expect(result.slotNum).toBe(1);
        expect(result.mainEntities).toHaveLength(2);
        expect(result.conditionEntities).toHaveLength(1);
        expect(result.codeSensorEntities).toHaveLength(1);
        expect(result.inSyncEntities).toHaveLength(1);
        expect(result.pinActiveEntity?.key).toBe(ACTIVE_KEY);
        expect(result.codeEventEntity?.key).toBe(CODE_EVENT_KEY);
        expect(result.calendarEntityId).toBe('calendar.slot_1');
    });

    it('returns null calendar for slots without calendar', () => {
        const entities: LockCodeManagerEntityEntry[] = [
            createTestEntity(2, ACTIVE_KEY, 'binary_sensor.active'),
            createTestEntity(2, CODE_EVENT_KEY, 'event.code')
        ];
        const result = getSlotMapping(2, entities, configEntryData);

        expect(result.calendarEntityId).toBeNull();
    });

    it('only includes entities for the specified slot', () => {
        const entities: LockCodeManagerEntityEntry[] = [
            createTestEntity(1, 'enabled', 'switch.slot_1_enabled'),
            createTestEntity(2, 'enabled', 'switch.slot_2_enabled'),
            createTestEntity(1, ACTIVE_KEY, 'binary_sensor.active_1'),
            createTestEntity(1, CODE_EVENT_KEY, 'event.code_1')
        ];
        const result = getSlotMapping(1, entities, configEntryData);

        expect(result.mainEntities).toHaveLength(1);
        expect(result.mainEntities[0].entity_id).toBe('switch.slot_1_enabled');
    });
});

describe('maybeGenerateFoldEntityRowCard', () => {
    it('returns empty array when entities are empty', () => {
        const hass = createMockHass();

        const result = maybeGenerateFoldEntityRowCard(hass, mockConfigEntry, [], 'Test', false);

        expect(result).toEqual([]);
    });

    it('returns section with entities when not using fold-entity-row', () => {
        const hass = createMockHass();
        const entities = [createTestEntity(1, 'enabled', 'switch.enabled')];

        const result = maybeGenerateFoldEntityRowCard(
            hass,
            mockConfigEntry,
            entities,
            'Test Section',
            false
        );

        expect(result).toEqual([
            { label: 'Test Section', type: 'section' },
            { entity: 'switch.enabled', name: 'Enabled' }
        ]);
    });

    it('returns fold-entity-row when useFoldEntityRow is true', () => {
        const hass = createMockHass();
        const entities = [createTestEntity(1, 'enabled', 'switch.enabled')];

        const result = maybeGenerateFoldEntityRowCard(
            hass,
            mockConfigEntry,
            entities,
            'Test Section',
            true
        );

        expect(result).toEqual([
            DIVIDER_CARD,
            {
                entities: [{ entity: 'switch.enabled', name: 'Enabled' }],
                head: { label: 'Test Section', type: 'section' },
                type: 'custom:fold-entity-row'
            }
        ]);
    });
});

describe('maybeGenerateFoldEntityRowConditionCard', () => {
    it('returns empty array when no conditions and no calendar', () => {
        const hass = createMockHass();

        const result = maybeGenerateFoldEntityRowConditionCard(
            hass,
            mockConfigEntry,
            [],
            null,
            'Conditions',
            false
        );

        expect(result).toEqual([]);
    });

    it('includes calendar entity at the start when provided', () => {
        const hass = createMockHass();
        const entities = [createTestEntity(1, CONDITION_KEYS[0], 'switch.condition')];

        const result = maybeGenerateFoldEntityRowConditionCard(
            hass,
            mockConfigEntry,
            entities,
            'calendar.slot_1',
            'Conditions',
            false
        );

        expect(result[0]).toEqual({ label: 'Conditions', type: 'section' });
        expect(result[1]).toEqual({ entity: 'calendar.slot_1' });
    });

    it('works with only calendar (no condition entities)', () => {
        const hass = createMockHass();

        const result = maybeGenerateFoldEntityRowConditionCard(
            hass,
            mockConfigEntry,
            [],
            'calendar.slot_1',
            'Conditions',
            false
        );

        expect(result).toHaveLength(2);
        expect(result[1]).toEqual({ entity: 'calendar.slot_1' });
    });

    it('uses fold-entity-row when useFoldEntityRow is true', () => {
        const hass = createMockHass();

        const result = maybeGenerateFoldEntityRowConditionCard(
            hass,
            mockConfigEntry,
            [],
            'calendar.slot_1',
            'Conditions',
            true
        );

        expect(result[0]).toEqual(DIVIDER_CARD);
        expect(result[1]).toMatchObject({
            entities: [{ entity: 'calendar.slot_1' }],
            head: { label: 'Conditions', type: 'section' },
            type: 'custom:fold-entity-row'
        });
    });
});

describe('generateSlotCard', () => {
    function createMinimalSlotMapping(slotNum: number): SlotMapping {
        return {
            calendarEntityId: null,
            codeEventEntity: createTestEntity(slotNum, CODE_EVENT_KEY, `event.slot_${slotNum}`),
            codeSensorEntities: [],
            conditionEntities: [],
            inSyncEntities: [],
            mainEntities: [createTestEntity(slotNum, 'enabled', `switch.slot_${slotNum}_enabled`)],
            pinActiveEntity: createTestEntity(slotNum, ACTIVE_KEY, `binary_sensor.slot_${slotNum}`),
            slotNum
        };
    }

    it('generates vertical-stack card with markdown header and entities', () => {
        const hass = createMockHass();
        const slotMapping = createMinimalSlotMapping(1);

        const result = generateSlotCard(hass, mockConfigEntry, slotMapping, false, false, false);

        expect(result.type).toBe('vertical-stack');
        expect(result.cards).toHaveLength(2);
        expect(result.cards[0]).toEqual({
            content: '## Code Slot 1',
            type: 'markdown'
        });
        expect(result.cards[1].type).toBe('entities');
    });

    it('includes PIN active and code event entities', () => {
        const hass = createMockHass();
        const slotMapping = createMinimalSlotMapping(1);

        const result = generateSlotCard(hass, mockConfigEntry, slotMapping, false, false, false);

        const entitiesCard = result.cards[1] as {
            entities: Array<{ entity: string; name: string }>;
        };
        const entityIds = entitiesCard.entities
            .filter((e) => typeof e === 'object' && 'entity' in e)
            .map((e) => (e as { entity: string }).entity);

        expect(entityIds).toContain('binary_sensor.slot_1');
        expect(entityIds).toContain('event.slot_1');
    });

    it('includes in_sync sensors when include_in_sync_sensors is true', () => {
        const hass = createMockHass({
            states: {
                'lock.front': { attributes: { friendly_name: 'Front Lock' }, state: 'locked' }
            }
        });
        const slotMapping = createMinimalSlotMapping(1);
        slotMapping.inSyncEntities = [
            createTestEntity(1, IN_SYNC_KEY, 'binary_sensor.in_sync', 'lock.front')
        ];

        const result = generateSlotCard(hass, mockConfigEntry, slotMapping, false, false, true);

        const entitiesCard = result.cards[1] as { entities: unknown[] };
        const hasInSync = entitiesCard.entities.some(
            (e) => typeof e === 'object' && 'entity' in e && e.entity === 'binary_sensor.in_sync'
        );
        expect(hasInSync).toBe(true);
    });

    it('excludes in_sync sensors when include_in_sync_sensors is false', () => {
        const hass = createMockHass();
        const slotMapping = createMinimalSlotMapping(1);
        slotMapping.inSyncEntities = [
            createTestEntity(1, IN_SYNC_KEY, 'binary_sensor.in_sync', 'lock.front')
        ];

        const result = generateSlotCard(hass, mockConfigEntry, slotMapping, false, false, false);

        const entitiesCard = result.cards[1] as { entities: unknown[] };
        const hasInSync = entitiesCard.entities.some(
            (e) => typeof e === 'object' && 'entity' in e && e.entity === 'binary_sensor.in_sync'
        );
        expect(hasInSync).toBe(false);
    });

    it('includes code slot sensors when include_code_slot_sensors is true', () => {
        const hass = createMockHass({
            states: {
                'lock.front': { attributes: { friendly_name: 'Front Lock' }, state: 'locked' }
            }
        });
        const slotMapping = createMinimalSlotMapping(1);
        slotMapping.codeSensorEntities = [
            createTestEntity(1, CODE_SENSOR_KEY, 'sensor.code', 'lock.front')
        ];

        const result = generateSlotCard(hass, mockConfigEntry, slotMapping, false, true, false);

        const entitiesCard = result.cards[1] as { entities: unknown[] };
        const hasCodeSensor = entitiesCard.entities.some(
            (e) => typeof e === 'object' && 'entity' in e && e.entity === 'sensor.code'
        );
        expect(hasCodeSensor).toBe(true);
    });
});

describe('generateView', () => {
    const testConfigEntry: ConfigEntryJSONFragment = {
        disabled_by: '',
        domain: 'lock_code_manager',
        entry_id: 'entry123',
        pref_disable_new_entities: false,
        pref_disable_polling: false,
        reason: null,
        source: 'user',
        state: 'loaded',
        supports_options: true,
        supports_remove_device: false,
        supports_unload: true,
        title: 'Test Lock'
    };

    it('generates a view with badges and cards', async () => {
        const configEntryData: LockCodeManagerConfigEntryData = {
            locks: ['lock.front'],
            slots: { 1: null }
        };
        const lovelaceResources: LovelaceResource[] = [];

        const hass = createMockHass({
            callWS: (msg) => {
                if (msg.type === 'lock_code_manager/get_slot_calendar_data') {
                    return configEntryData;
                }
                if (msg.type === 'lovelace/resources') {
                    return lovelaceResources;
                }
                return undefined;
            }
        });

        const result = await generateView(
            hass,
            testConfigEntry,
            false,
            false,
            false,
            'unmasked',
            // use legacy entities cards for test
            false
        );

        expect(result.title).toBe('Test Lock');
        expect(result.path).toBe('test-lock');
        expect(result.panel).toBe(false);
        // Lock badges are now entity objects
        const lockBadge = result.badges.find(
            (badge): badge is { entity: string; type: string } =>
                typeof badge === 'object' &&
                badge !== null &&
                'type' in badge &&
                badge.type === 'entity' &&
                'entity' in badge &&
                badge.entity === 'lock.front'
        );
        expect(lockBadge).toBeDefined();
        expect(result.type).toBe('sections');
        expect(result.sections).toHaveLength(1);
        // Sections now contain strategies, not rendered cards
        const strategy = result.sections[0].strategy as {
            slot: number;
            type: string;
        };
        expect(strategy.type).toBe('custom:lock-code-manager-slot');
        expect(strategy.slot).toBe(1);
    });

    it('only includes lock entity badges (no template badges)', async () => {
        const configEntryData: LockCodeManagerConfigEntryData = {
            locks: ['lock.front'],
            slots: { 1: null, 2: null }
        };

        const hass = createMockHass({
            callWS: (msg) => {
                if (msg.type === 'lock_code_manager/get_slot_calendar_data') {
                    return configEntryData;
                }
                if (msg.type === 'lovelace/resources') {
                    return [];
                }
                return undefined;
            }
        });

        const result = await generateView(
            hass,
            testConfigEntry,
            false,
            false,
            false,
            'unmasked',
            // use legacy entities cards for test
            false
        );

        // Should only have entity badges for locks (no template badges)
        const entityBadges = result.badges.filter(
            (badge): badge is { entity: string; type: string } =>
                typeof badge === 'object' &&
                badge !== null &&
                'type' in badge &&
                badge.type === 'entity'
        );
        expect(entityBadges).toHaveLength(1);
        expect(entityBadges[0].entity).toBe('lock.front');

        // No template badges (not supported by HA)
        const templateBadges = result.badges.filter(
            (badge): badge is { type: string } =>
                typeof badge === 'object' &&
                badge !== null &&
                'type' in badge &&
                badge.type === 'template'
        );
        expect(templateBadges).toHaveLength(0);
    });

    it('generates one card per slot', async () => {
        const configEntryData: LockCodeManagerConfigEntryData = {
            locks: ['lock.front'],
            slots: { 1: null, 2: null, 3: null }
        };

        const hass = createMockHass({
            callWS: (msg) => {
                if (msg.type === 'lock_code_manager/get_slot_calendar_data') {
                    return configEntryData;
                }
                if (msg.type === 'lovelace/resources') {
                    return [];
                }
                return undefined;
            }
        });

        const result = await generateView(
            hass,
            testConfigEntry,
            false,
            false,
            false,
            'unmasked',
            // use legacy entities cards for test
            false
        );

        expect(result.sections).toHaveLength(3);
    });

    it('passes use_slot_cards=false to section strategy for legacy mode', async () => {
        const configEntryData: LockCodeManagerConfigEntryData = {
            locks: ['lock.front'],
            slots: { 1: null }
        };

        const hass = createMockHass({
            callWS: (msg) => {
                if (msg.type === 'lock_code_manager/get_slot_calendar_data') {
                    return configEntryData;
                }
                return undefined;
            },
            states: {
                'lock.front': { attributes: { friendly_name: 'Front Lock' }, state: 'locked' }
            }
        });

        const result = await generateView(
            hass,
            testConfigEntry,
            false,
            true,
            false,
            'unmasked',
            // use legacy entities cards for test
            false
        );

        // Verify the section strategy is configured for legacy mode
        const [strategy] = result.sections.map((s) => s.strategy) as Array<{
            show_lock_sync: boolean;
            type: string;
            use_slot_cards: boolean;
        }>;
        expect(strategy.type).toBe('custom:lock-code-manager-slot');
        expect(strategy.use_slot_cards).toBe(false);
        expect(strategy.show_lock_sync).toBe(true);
    });

    it('sorts locks alphabetically in badges', async () => {
        const configEntryData: LockCodeManagerConfigEntryData = {
            locks: ['lock.z_back', 'lock.a_front'],
            slots: { 1: null }
        };

        const hass = createMockHass({
            callWS: (msg) => {
                if (msg.type === 'lock_code_manager/get_slot_calendar_data') {
                    return configEntryData;
                }
                if (msg.type === 'lovelace/resources') {
                    return [];
                }
                return undefined;
            }
        });

        const result = await generateView(
            hass,
            testConfigEntry,
            false,
            false,
            false,
            'unmasked',
            // use legacy entities cards for test
            false
        );

        // Lock badges are now entity objects, not strings
        const lockBadges = result.badges.filter(
            (badge): badge is { entity: string; type: string } =>
                typeof badge === 'object' &&
                badge !== null &&
                'type' in badge &&
                badge.type === 'entity'
        );
        const lockEntityIds = lockBadges.map((badge) => badge.entity);
        expect(lockEntityIds).toEqual(['lock.a_front', 'lock.z_back']);
    });

    it('uses new slot cards when use_slot_cards is true', async () => {
        const configEntryData: LockCodeManagerConfigEntryData = {
            locks: ['lock.front'],
            slots: { 1: null, 2: null }
        };

        const hass = createMockHass({
            callWS: (msg) => {
                if (msg.type === 'lock_code_manager/get_slot_calendar_data') {
                    return configEntryData;
                }
                if (msg.type === 'lovelace/resources') {
                    return [];
                }
                return undefined;
            }
        });

        const result = await generateView(
            hass,
            testConfigEntry,
            false,
            true,
            false,
            'masked_with_reveal',
            // use_slot_cards = true
            true
        );

        expect(result.sections).toHaveLength(2);
        // Verify sections use slot section strategies
        const strategies = result.sections.map((s) => s.strategy) as Array<{
            config_entry_id: string;
            slot: number;
            type: string;
        }>;
        expect(strategies[0].type).toBe('custom:lock-code-manager-slot');
        expect(strategies[0].slot).toBe(1);
        expect(strategies[0].config_entry_id).toBe('entry123');
        expect(strategies[1].type).toBe('custom:lock-code-manager-slot');
        expect(strategies[1].slot).toBe(2);
    });
});

describe('generateView lock codes cards', () => {
    const testConfigEntry: ConfigEntryJSONFragment = {
        disabled_by: '',
        domain: 'lock_code_manager',
        entry_id: 'entry123',
        pref_disable_new_entities: false,
        pref_disable_polling: false,
        reason: null,
        source: 'user',
        state: 'loaded',
        supports_options: true,
        supports_remove_device: false,
        supports_unload: true,
        title: 'Test Lock'
    };

    it('adds lock codes cards directly to cards array (not wrapped in grid)', async () => {
        const configEntryData: LockCodeManagerConfigEntryData = {
            locks: ['lock.front', 'lock.back'],
            slots: { 1: null }
        };

        const hass = createMockHass({
            callWS: (msg) => {
                if (msg.type === 'lock_code_manager/get_slot_calendar_data') {
                    return configEntryData;
                }
                if (msg.type === 'lovelace/resources') {
                    return [];
                }
                return undefined;
            },
            states: {
                'lock.front': { attributes: { friendly_name: 'Front Lock' }, state: 'locked' },
                'lock.back': { attributes: { friendly_name: 'Back Lock' }, state: 'locked' }
            }
        });

        const result = await generateView(
            hass,
            testConfigEntry,
            false,
            false,
            // show_all_codes_for_locks
            true,
            'unmasked',
            true
        );

        // Should have 1 slot section + 2 lock sections
        expect(result.sections).toHaveLength(3);

        // Extract strategies from sections
        const strategies = result.sections.map((s) => s.strategy) as Array<{ type: string }>;

        // Lock codes should use lock section strategies
        const lockStrategies = strategies.filter((s) => s.type === 'custom:lock-code-manager-lock');
        expect(lockStrategies).toHaveLength(2);

        // Slot should use slot section strategy
        const slotStrategies = strategies.filter((s) => s.type === 'custom:lock-code-manager-slot');
        expect(slotStrategies).toHaveLength(1);
    });

    it('sorts lock codes cards alphabetically by friendly name', async () => {
        const configEntryData: LockCodeManagerConfigEntryData = {
            locks: ['lock.z_garage', 'lock.a_front', 'lock.m_back'],
            slots: { 1: null }
        };

        const hass = createMockHass({
            callWS: (msg) => {
                if (msg.type === 'lock_code_manager/get_slot_calendar_data') {
                    return configEntryData;
                }
                if (msg.type === 'lovelace/resources') {
                    return [];
                }
                return undefined;
            },
            states: {
                'lock.z_garage': { attributes: { friendly_name: 'Garage' }, state: 'locked' },
                'lock.a_front': { attributes: { friendly_name: 'Front Door' }, state: 'locked' },
                'lock.m_back': { attributes: { friendly_name: 'Back Door' }, state: 'locked' }
            }
        });

        const result = await generateView(
            hass,
            testConfigEntry,
            false,
            false,
            // show_all_codes_for_locks
            true,
            'unmasked',
            true
        );

        // Extract lock strategies from sections (skip the slot strategy at index 0)
        const lockStrategies = result.sections
            .map((s) => s.strategy)
            .filter((s) => s?.type === 'custom:lock-code-manager-lock') as Array<{
            lock_entity_id: string;
            type: string;
        }>;

        // Should be sorted by friendly name: Back Door, Front Door, Garage
        expect(lockStrategies[0].lock_entity_id).toBe('lock.m_back');
        expect(lockStrategies[1].lock_entity_id).toBe('lock.a_front');
        expect(lockStrategies[2].lock_entity_id).toBe('lock.z_garage');
    });

    it('includes code_display in lock codes cards', async () => {
        const configEntryData: LockCodeManagerConfigEntryData = {
            locks: ['lock.front'],
            slots: { 1: null }
        };

        const hass = createMockHass({
            callWS: (msg) => {
                if (msg.type === 'lock_code_manager/get_slot_calendar_data') {
                    return configEntryData;
                }
                if (msg.type === 'lovelace/resources') {
                    return [];
                }
                return undefined;
            }
        });

        const result = await generateView(
            hass,
            testConfigEntry,
            false,
            false,
            // show_all_codes_for_locks
            true,
            'masked_with_reveal',
            true
        );

        // Extract lock strategy from sections
        const lockStrategy = result.sections
            .map((s) => s.strategy)
            .find((s) => s?.type === 'custom:lock-code-manager-lock') as {
            code_display?: string;
            type: string;
        };
        expect(lockStrategy?.code_display).toBe('masked_with_reveal');
    });

    it('does not add lock codes cards when show_all_codes_for_locks is false', async () => {
        const configEntryData: LockCodeManagerConfigEntryData = {
            locks: ['lock.front', 'lock.back'],
            slots: { 1: null }
        };

        const hass = createMockHass({
            callWS: (msg) => {
                if (msg.type === 'lock_code_manager/get_slot_calendar_data') {
                    return configEntryData;
                }
                if (msg.type === 'lovelace/resources') {
                    return [];
                }
                return undefined;
            }
        });

        const result = await generateView(
            hass,
            testConfigEntry,
            false,
            false,
            // show_all_codes_for_locks = false
            false,
            'unmasked',
            true
        );

        // Should only have 1 slot section, no lock sections
        expect(result.sections).toHaveLength(1);
        const strategies = result.sections.map((s) => s.strategy) as Array<{ type: string }>;
        const lockStrategies = strategies.filter(
            (s) => s?.type === 'custom:lock-code-manager-lock'
        );
        expect(lockStrategies).toHaveLength(0);
        // Verify it's a slot strategy
        expect(strategies[0].type).toBe('custom:lock-code-manager-slot');
    });
});

describe('generateNewSlotCard', () => {
    const testConfigEntry: ConfigEntryJSONFragment = {
        disabled_by: '',
        domain: 'lock_code_manager',
        entry_id: 'entry456',
        pref_disable_new_entities: false,
        pref_disable_polling: false,
        reason: null,
        source: 'user',
        state: 'loaded',
        supports_options: true,
        supports_remove_device: false,
        supports_unload: true,
        title: 'Test Config'
    };

    it('generates slot card with correct type and slot number', () => {
        const result = generateNewSlotCard(testConfigEntry, 3, false, false);

        expect(result.type).toBe('custom:lcm-slot');
        expect(result.slot).toBe(3);
        expect(result.config_entry_id).toBe('entry456');
    });

    it('passes show_code_sensors option', () => {
        const result = generateNewSlotCard(testConfigEntry, 1, true, false);

        expect(result.show_code_sensors).toBe(true);
    });

    it('passes show_lock_sync option', () => {
        const result = generateNewSlotCard(testConfigEntry, 1, false, true);

        expect(result.show_lock_sync).toBe(true);
    });

    it('passes both options when enabled', () => {
        const result = generateNewSlotCard(testConfigEntry, 5, true, true);

        expect(result.type).toBe('custom:lcm-slot');
        expect(result.slot).toBe(5);
        expect(result.show_code_sensors).toBe(true);
        expect(result.show_lock_sync).toBe(true);
    });
});
