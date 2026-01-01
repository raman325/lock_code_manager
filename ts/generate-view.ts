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
            hass,
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

/** @internal - exported for testing via generate-view.internal.ts */
export function compareAndSortEntities(
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

/** @internal - exported for testing via generate-view.internal.ts */
export function createLockCodeManagerEntity(
    entity: EntityRegistryEntry
): LockCodeManagerEntityEntry {
    const split = entity.unique_id.split('|');
    return {
        ...entity,
        key: split[2],
        lockEntityId: split[3],
        slotNum: parseInt(split[1], 10)
    };
}

/** @internal - exported for testing via generate-view.internal.ts */
export function generateEntityCards(
    hass: HomeAssistant,
    configEntry: ConfigEntryJSONFragment,
    entities: LockCodeManagerEntityEntry[]
): { entity: string; name?: string }[] {
    return entities.map((entity) => {
        if ([IN_SYNC_KEY, CODE_SENSOR_KEY].includes(entity.key)) {
            return {
                entity: entity.entity_id,
                name:
                    hass.states[entity.lockEntityId]?.attributes?.friendly_name ??
                    entity.lockEntityId
            };
        }
        return {
            entity: entity.entity_id,
            name: getEntityDisplayName(configEntry, entity)
        };
    });
}

/** @internal - exported for testing via generate-view.internal.ts */
export function getEntityDisplayName(
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

/** @internal - exported for testing via generate-view.internal.ts */
export function generateSlotCard(
    hass: HomeAssistant,
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
                    ...generateEntityCards(hass, configEntry, slotMapping.mainEntities),
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
                        hass,
                        configEntry,
                        slotMapping.conditionEntities,
                        slotMapping.calendarEntityId,
                        'Conditions',
                        useFoldEntityRow
                    ),
                    ...(include_in_sync_sensors
                        ? maybeGenerateFoldEntityRowCard(
                              hass,
                              configEntry,
                              slotMapping.inSyncEntities,
                              'Locks in sync',
                              useFoldEntityRow
                          )
                        : []),
                    ...(include_code_slot_sensors
                        ? maybeGenerateFoldEntityRowCard(
                              hass,
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

/** @internal - exported for testing via generate-view.internal.ts */
export function getSlotMapping(
    hass: HomeAssistant,
    slotNum: number,
    lockCodeManagerEntities: LockCodeManagerEntityEntry[],
    configEntryData: LockCodeManagerConfigEntryData
): SlotMapping {
    const mainEntities: LockCodeManagerEntityEntry[] = [];
    const conditionEntities: LockCodeManagerEntityEntry[] = [];
    const codeSensorEntities: LockCodeManagerEntityEntry[] = [];
    const inSyncEntities: LockCodeManagerEntityEntry[] = [];
    lockCodeManagerEntities
        .filter((entity) => entity.slotNum === slotNum)
        .forEach((entity) => {
            if (entity.key === CODE_SENSOR_KEY) {
                codeSensorEntities.push(entity);
            } else if (entity.key === IN_SYNC_KEY) {
                inSyncEntities.push(entity);
            } else if (CONDITION_KEYS.includes(entity.key)) {
                conditionEntities.push(entity);
            } else if (![ACTIVE_KEY, IN_SYNC_KEY, CODE_EVENT_KEY].includes(entity.key)) {
                mainEntities.push(entity);
            }
        });
    const codeEventEntity = lockCodeManagerEntities.find(
        (entity) => entity.slotNum === slotNum && entity.key === CODE_EVENT_KEY
    );
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

/** @internal - exported for testing via generate-view.internal.ts */
export function maybeGenerateFoldEntityRowCard(
    hass: HomeAssistant,
    configEntry: ConfigEntryJSONFragment,
    entities: LockCodeManagerEntityEntry[],
    label: string,
    useFoldEntityRow: boolean
) {
    if (entities.length === 0) return [];
    const entityCards = generateEntityCards(hass, configEntry, entities);
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

/** @internal - exported for testing via generate-view.internal.ts */
export function maybeGenerateFoldEntityRowConditionCard(
    hass: HomeAssistant,
    configEntry: ConfigEntryJSONFragment,
    conditionEntities: LockCodeManagerEntityEntry[],
    calendarEntityId: string | null | undefined,
    label: string,
    useFoldEntityRow: boolean
) {
    if (conditionEntities.length === 0 && calendarEntityId == null) return [];
    const entityCards = generateEntityCards(hass, configEntry, conditionEntities);
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
