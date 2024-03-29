import {
    ACTIVE_KEY,
    CODE_EVENT_KEY,
    CODE_SENSOR_KEY,
    CONDITION_KEYS,
    DIVIDER_CARD,
    FOLD_ENTITY_ROW_SEARCH_STRING,
    IN_SYNC_KEY,
    KEY_ORDER
} from './const';
import {
    EntityRegistryEntry,
    HomeAssistant,
    LovelaceCardConfig,
    LovelaceResource,
    LovelaceViewConfig
} from './ha_type_stubs';
import { slugify } from './slugify';
import { LockCodeManagerConfigEntryData, LockCodeManagerEntityEntry, SlotMapping } from './types';

export async function generateView(
    hass: HomeAssistant,
    configEntryId: string,
    configEntryTitle: string,
    entities: EntityRegistryEntry[],
    include_code_slot_sensors: boolean,
    include_in_sync_sensors: boolean
): Promise<LovelaceViewConfig> {
    const [configEntryData, lovelaceResources] = await Promise.all([
        hass.callWS<LockCodeManagerConfigEntryData>({
            config_entry_id: configEntryId,
            type: 'lock_code_manager/get_slot_calendar_data'
        }),
        hass.callWS<LovelaceResource[]>({
            type: 'lovelace/resources'
        })
    ]);

    const sortedEntities = entities
        .map((entity) => createLockCodeManagerEntity(entity))
        .sort(compareAndSortEntities);
    const slots = Object.keys(configEntryData.slots).map((slotNum) => parseInt(slotNum, 10));
    const slotMappings: SlotMapping[] = slots.map((slotNum) =>
        getSlotMapping(hass, slotNum, sortedEntities, configEntryData)
    );

    const badges = [
        ...configEntryData.locks.sort((a, b) => a.localeCompare(b)),
        ...sortedEntities
            .filter((entity) => entity.key === 'active')
            .map((entity) => {
                return {
                    entity: entity.entity_id,
                    name: `Slot ${entity.slotNum.toString()} active`,
                    type: 'state-label'
                };
            })
    ];

    const useFoldEntityRow =
        lovelaceResources.filter((resource) => resource.url.includes(FOLD_ENTITY_ROW_SEARCH_STRING))
            .length > 0;

    const cards = slotMappings.map((slotMapping) =>
        generateSlotCard(
            slotMapping,
            useFoldEntityRow,
            include_code_slot_sensors,
            include_in_sync_sensors
        )
    );

    return {
        badges,
        cards,
        panel: false,
        path: slugify(configEntryTitle),
        title: configEntryTitle
    };
}

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

function createLockCodeManagerEntity(entity: EntityRegistryEntry): LockCodeManagerEntityEntry {
    const split = entity.unique_id.split('|');
    return {
        ...entity,
        key: split[2],
        lockEntityId: split[3],
        slotNum: parseInt(split[1], 10)
    };
}

function generateEntityCards(entities: string[]): { entity: string }[] {
    return entities.map((entityId) => {
        return {
            entity: entityId
        };
    });
}

function generateSlotCard(
    slotMapping: SlotMapping,
    useFoldEntityRow: boolean,
    include_code_slot_sensors: boolean,
    include_in_sync_sensors: boolean
): LovelaceCardConfig {
    return {
        cards: [
            {
                content: `## Code Slot ${slotMapping.slotNum}`,
                type: 'markdown'
            },
            {
                entities: [
                    ...generateEntityCards(slotMapping.mainEntityIds),
                    DIVIDER_CARD,
                    {
                        entity: slotMapping.pinActiveEntity.entity_id,
                        name: 'PIN active'
                    },
                    {
                        entity: slotMapping.codeEventEntityId,
                        name: 'PIN last used'
                    },
                    ...maybeGenerateFoldEntityRowCard(
                        slotMapping.conditionEntityIds,
                        'Conditions',
                        useFoldEntityRow
                    ),
                    ...(include_in_sync_sensors
                        ? maybeGenerateFoldEntityRowCard(
                              slotMapping.inSyncEntityIds,
                              'Locks in sync',
                              useFoldEntityRow
                          )
                        : []),
                    ...(include_code_slot_sensors
                        ? maybeGenerateFoldEntityRowCard(
                              slotMapping.codeSensorEntityIds,
                              'Code slot sensors',
                              useFoldEntityRow
                          )
                        : [])
                ],
                show_header_toggle: false,
                type: 'entities'
            }
        ],
        type: 'vertical-stack'
    };
}

function getSlotMapping(
    hass: HomeAssistant,
    slotNum: number,
    lockCodeManagerEntities: LockCodeManagerEntityEntry[],
    configEntryData: LockCodeManagerConfigEntryData
): SlotMapping {
    const mainEntityIds: string[] = [];
    const conditionEntityIds: string[] = [];
    const codeSensorEntityIds: string[] = [];
    const inSyncEntityIds: string[] = [];
    let codeEventEntityId: string;
    lockCodeManagerEntities
        .filter((entity) => entity.slotNum === slotNum)
        .forEach((entity) => {
            if (entity.key === CODE_SENSOR_KEY) {
                codeSensorEntityIds.push(entity.entity_id);
            } else if (entity.key === IN_SYNC_KEY) {
                inSyncEntityIds.push(entity.entity_id);
            } else if (entity.key === CODE_EVENT_KEY) {
                codeEventEntityId = entity.entity_id;
            } else if (CONDITION_KEYS.includes(entity.key)) {
                conditionEntityIds.push(entity.entity_id);
            } else if (![ACTIVE_KEY, IN_SYNC_KEY].includes(entity.key)) {
                mainEntityIds.push(entity.entity_id);
            }
        });
    const pinActiveEntity = lockCodeManagerEntities.find(
        (entity) => entity.slotNum === slotNum && entity.key === ACTIVE_KEY
    );
    const calendarEntityId = configEntryData.slots[slotNum];
    if (calendarEntityId) conditionEntityIds.unshift(calendarEntityId);
    return {
        codeEventEntityId,
        codeSensorEntityIds,
        conditionEntityIds,
        inSyncEntityIds,
        mainEntityIds,
        pinActiveEntity,
        slotNum
    };
}

function maybeGenerateFoldEntityRowCard(
    entities: string[],
    label: string,
    useFoldEntityRow: boolean
) {
    if (entities.length === 0) return [];
    const entityCards = generateEntityCards(entities);
    return useFoldEntityRow
        ? [
              DIVIDER_CARD,
              {
                  entities: entityCards,
                  head: {
                      label,
                      type: 'section'
                  },
                  type: 'custom:fold-entity-row'
              }
          ]
        : [
              {
                  label,
                  type: 'section'
              },
              ...entityCards
          ];
}
