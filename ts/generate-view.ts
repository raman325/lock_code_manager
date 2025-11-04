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
import {
    ConfigEntryJSONFragment,
    LockCodeManagerConfigEntryData,
    LockCodeManagerEntityEntry,
    SlotMapping
} from './types';
import { capitalize } from './util';

export async function generateView(
    hass: HomeAssistant,
    configEntry: ConfigEntryJSONFragment,
    entities: EntityRegistryEntry[],
    include_code_slot_sensors: boolean,
    include_in_sync_sensors: boolean
): Promise<LovelaceViewConfig> {
    const callData = {
        type: 'lock_code_manager/get_config_entry_entities'
    };

    // Log diagnostic info
    // eslint-disable-next-line no-console
    console.debug('[Lock Code Manager] Generating view:', {
        configEntry: configEntry.title,
        entityCount: entities.length,
        entryId: configEntry.entry_id
    });

    const [configEntryData, lovelaceResources] = await Promise.all([
        hass.callWS<LockCodeManagerConfigEntryData>({
            config_entry_id: configEntry.entry_id,
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
            configEntry,
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
        path: slugify(configEntry.title),
        title: configEntry.title
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

function generateEntityCards(
    configEntry: ConfigEntryJSONFragment,
    entities: LockCodeManagerEntityEntry[]
): { entity: string }[] {
    return entities.map((entity) => {
        if ([IN_SYNC_KEY, CODE_SENSOR_KEY].includes(entity.key)) {
            return {
                entity: entity.entity_id
            };
        }
        const name = (entity.name || entity.original_name)
            .replace(`Code slot ${entity.slotNum}`, '')
            .replace('  ', ' ')
            .replace('  ', ' ')
            .trim()
            .replace(configEntry.title, '')
            .replace('  ', ' ')
            .replace('  ', ' ')
            .trim();
        return {
            entity: entity.entity_id,
            name: capitalize(name)
        };
    });
}

function generateSlotCard(
    configEntry: ConfigEntryJSONFragment,
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
                    ...generateEntityCards(configEntry, slotMapping.mainEntities),
                    DIVIDER_CARD,
                    {
                        entity: slotMapping.pinActiveEntity.entity_id,
                        name: 'PIN active'
                    },
                    {
                        entity: slotMapping.codeEventEntity.entity_id,
                        name: 'PIN last used'
                    },
                    ...maybeGenerateFoldEntityRowConditionCard(
                        configEntry,
                        slotMapping.conditionEntities,
                        slotMapping.calendarEntityId,
                        'Conditions',
                        useFoldEntityRow
                    ),
                    ...(include_in_sync_sensors
                        ? maybeGenerateFoldEntityRowCard(
                              configEntry,
                              slotMapping.inSyncEntities,
                              'Locks in sync',
                              useFoldEntityRow
                          )
                        : []),
                    ...(include_code_slot_sensors
                        ? maybeGenerateFoldEntityRowCard(
                              configEntry,
                              slotMapping.codeSensorEntities,
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
    const mainEntities: LockCodeManagerEntityEntry[] = [];
    const conditionEntities: LockCodeManagerEntityEntry[] = [];
    const codeSensorEntities: LockCodeManagerEntityEntry[] = [];
    const inSyncEntities: LockCodeManagerEntityEntry[] = [];
    let codeEventEntity: LockCodeManagerEntityEntry;
    lockCodeManagerEntities
        .filter((entity) => entity.slotNum === slotNum)
        .forEach((entity) => {
            if (entity.key === CODE_SENSOR_KEY) {
                codeSensorEntities.push(entity);
            } else if (entity.key === IN_SYNC_KEY) {
                inSyncEntities.push(entity);
            } else if (entity.key === CODE_EVENT_KEY) {
                codeEventEntity = entity;
            } else if (CONDITION_KEYS.includes(entity.key)) {
                conditionEntities.push(entity);
            } else if (![ACTIVE_KEY, IN_SYNC_KEY].includes(entity.key)) {
                mainEntities.push(entity);
            }
        });
    const pinActiveEntity = lockCodeManagerEntities.find(
        (entity) => entity.slotNum === slotNum && entity.key === ACTIVE_KEY
    );
    const calendarEntityId: string | null | undefined = configEntryData.slots[slotNum];

    return {
        calendarEntityId,
        codeEventEntity,
        codeSensorEntities,
        conditionEntities,
        inSyncEntities,
        mainEntities,
        pinActiveEntity,
        slotNum
    };
}

function maybeGenerateFoldEntityRowCard(
    configEntry: ConfigEntryJSONFragment,
    entities: LockCodeManagerEntityEntry[],
    label: string,
    useFoldEntityRow: boolean
) {
    if (entities.length === 0) return [];
    const entityCards = generateEntityCards(configEntry, entities);
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

function maybeGenerateFoldEntityRowConditionCard(
    configEntry: ConfigEntryJSONFragment,
    conditionEntities: LockCodeManagerEntityEntry[],
    calendarEntityId: string | null | undefined,
    label: string,
    useFoldEntityRow: boolean
) {
    if (conditionEntities.length === 0 && calendarEntityId == null) return [];
    const entityCards = generateEntityCards(configEntry, conditionEntities);
    if (calendarEntityId != null) {
        entityCards.unshift({
            entity: calendarEntityId
        });
    }

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
