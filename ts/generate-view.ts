import {
    ACTIVE_KEY,
    CODE_EVENT_KEY,
    CODE_SENSOR_KEY,
    CONDITION_KEYS,
    DIVIDER_CARD,
    IN_SYNC_KEY,
    KEY_ORDER
} from './const';
import {
    EntityRegistryEntry,
    HomeAssistant,
    LovelaceCardConfig,
    LovelaceSectionConfig,
    LovelaceViewConfig
} from './ha_type_stubs';
// Note: FOLD_ENTITY_ROW_SEARCH_STRING and LovelaceResource are used by slot-section-strategy
// via the exported functions, keeping imports for re-export
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
    show_code_sensors: boolean,
    show_lock_sync: boolean,
    show_all_codes_for_locks: boolean,
    code_display: string,
    use_slot_cards: boolean,
    show_conditions = true,
    show_lock_status = true,
    collapsed_sections?: ('conditions' | 'lock_status')[]
): Promise<LovelaceViewConfig> {
    const configEntryData = await hass.callWS<LockCodeManagerConfigEntryData>({
        config_entry_id: configEntry.entry_id,
        type: 'lock_code_manager/get_slot_calendar_data'
    });

    const slots = Object.keys(configEntryData.slots).map((slotNum) => parseInt(slotNum, 10));

    // Build badges - lock state badges only
    // Note: Template badges are not supported by HA, so we only use entity badges
    // The slot cards already show detailed status (active, sync, conditions)
    const badges: Array<string | object> = [];

    // Lock state badges - show each lock with its current state
    // Entity badges automatically show friendly name and lock/unlock state
    configEntryData.locks
        .sort((a, b) => a.localeCompare(b))
        .forEach((lockEntityId) => {
            badges.push({
                entity: lockEntityId,
                type: 'entity'
            });
        });

    // Generate sections using section strategies (strategies handle both new and legacy card modes)
    const sections: LovelaceSectionConfig[] = slots.map((slotNum) => {
        return {
            strategy: {
                code_display,
                collapsed_sections,
                config_entry_id: configEntry.entry_id,
                show_code_sensors,
                show_conditions,
                show_lock_status,
                show_lock_sync,
                slot: slotNum,
                type: 'custom:lock-code-manager-slot',
                use_slot_cards
            }
        };
    });

    if (show_all_codes_for_locks) {
        const sortedLockIds = [...configEntryData.locks].sort((a, b) => {
            const nameA = hass.states[a]?.attributes?.friendly_name ?? a;
            const nameB = hass.states[b]?.attributes?.friendly_name ?? b;
            return nameA.localeCompare(nameB, undefined, { sensitivity: 'base' });
        });
        // Add lock codes sections using section strategy
        sortedLockIds.forEach((lockEntityId) => {
            sections.push({
                strategy: {
                    code_display,
                    lock_entity_id: lockEntityId,
                    type: 'custom:lock-code-manager-lock'
                }
            });
        });
    }

    return {
        badges,
        panel: false,
        path: slugify(configEntry.title),
        sections,
        title: configEntry.title,
        type: 'sections'
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
    show_code_sensors: boolean,
    show_lock_sync: boolean
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
                    ...(show_lock_sync
                        ? maybeGenerateFoldEntityRowCard(
                              hass,
                              configEntry,
                              slotMapping.inSyncEntities,
                              'Locks in sync',
                              useFoldEntityRow
                          )
                        : []),
                    ...(show_code_sensors
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
export function generateNewSlotCard(
    configEntry: ConfigEntryJSONFragment,
    slotNum: number,
    show_code_sensors: boolean,
    show_lock_sync: boolean,
    show_conditions = true,
    show_lock_status = true,
    collapsed_sections?: ('conditions' | 'lock_status')[]
): LovelaceCardConfig {
    // Use the new slot card with built-in header, inline editing, and websocket updates
    const card: LovelaceCardConfig = {
        config_entry_id: configEntry.entry_id,
        show_code_sensors,
        show_conditions,
        show_lock_status,
        show_lock_sync,
        slot: slotNum,
        type: 'custom:lcm-slot'
    };
    if (collapsed_sections && collapsed_sections.length > 0) {
        card.collapsed_sections = collapsed_sections;
    }
    return card;
}

/** @internal - exported for testing via generate-view.internal.ts */
export function getSlotMapping(
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
