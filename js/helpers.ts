import {
  CODE_EVENT_KEY,
  CODE_SENSOR_KEY,
  CONDITION_KEYS,
  KEY_ORDER,
  PIN_SYNCED_TO_LOCKS_KEY
} from './const';
import { EntityRegistryEntry, HomeAssistant, LovelaceViewConfig } from './ha_type_stubs';
import { LockCodeManagerConfigEntryData, LockCodeManagerEntityEntry, SlotMapping } from './types';

const DIVIDER_CARD = {
  type: 'divider'
};

export async function generateView(
  hass: HomeAssistant,
  configEntryId: string,
  configEntryTitle: string,
  entities: EntityRegistryEntry[],
  use_fold_entity_row: boolean,
  include_code_slot_sensors: boolean
): Promise<LovelaceViewConfig> {
  const sortedEntities = entities
    .map((entity) => createLockCodeManagerEntity(entity))
    .sort((entityA, entityB) => compareAndSortEntities(entityA, entityB));
  const configEntryData = await hass.callWS<LockCodeManagerConfigEntryData>({
    config_entry_id: configEntryId,
    type: 'lock_code_manager/get_config_entry_data'
  });
  const slots = Object.keys(configEntryData.slots).map((slotNum) => parseInt(slotNum, 10));
  const slotMappings: SlotMapping[] = slots.map((slotNum) =>
    getSlotMapping(hass, slotNum, sortedEntities, configEntryData)
  );

  return {
    badges: [
      ...configEntryData.locks.sort((a, b) => a.localeCompare(b)),
      ...sortedEntities
        .filter((entity) => entity.key === 'pin_synced_to_locks')
        .map((entity) => {
          return {
            entity: entity.entity_id,
            name: (entity.name ? entity.name : entity.original_name)
              .replace('PIN synced to locks', 'synced')
              .replace('Code slot', 'Slot'),
            type: 'state-label'
          };
        })
    ],
    cards: slotMappings.map((slotMapping) =>
      generateSlotCard(slotMapping, use_fold_entity_row, include_code_slot_sensors)
    ),
    panel: false,
    path: slugify(configEntryTitle),
    title: configEntryTitle
  };
}

function compareAndSortEntities(
  entityA: LockCodeManagerEntityEntry,
  entityB: LockCodeManagerEntityEntry
) {
  // sort by slot number
  if (entityA.slotNum < entityB.slotNum) return -1;
  // sort by key order
  if (
    entityA.slotNum === entityB.slotNum &&
    KEY_ORDER.indexOf(entityA.key) < KEY_ORDER.indexOf(entityB.key)
  )
    return -1;
  // sort code sensors alphabetically based on the lock entity_id
  if (
    entityA.slotNum === entityB.slotNum &&
    entityA.key === entityB.key &&
    entityA.key === 'code' &&
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

function generateEntityCards(entities: string[]) {
  return entities.map((entityId) => {
    return {
      entity: entityId
    };
  });
}

function generateSlotCard(
  slotMapping: SlotMapping,
  use_fold_entity_row: boolean,
  include_code_slot_sensors: boolean
) {
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
            entity: slotMapping.pinShouldBeEnabledEntity.entity_id
          },
          ...maybeGenerateFoldEntityRowCard(
            slotMapping.codeEventEntityIds,
            'Unlock Events for this Slot',
            use_fold_entity_row
          ),
          ...maybeGenerateFoldEntityRowCard(
            slotMapping.conditionEntityIds,
            'Conditions',
            use_fold_entity_row
          ),
          ...(include_code_slot_sensors
            ? maybeGenerateFoldEntityRowCard(
                slotMapping.codeSensorEntityIds,
                'Code Slot Sensors',
                use_fold_entity_row
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
  const codeEventEntityIds: string[] = [];
  lockCodeManagerEntities
    .filter((entity) => entity.slotNum === slotNum)
    .forEach((entity) => {
      if (entity.key === CODE_SENSOR_KEY) {
        codeSensorEntityIds.push(entity.entity_id);
      } else if (entity.key === CODE_EVENT_KEY) {
        codeEventEntityIds.push(entity.entity_id);
      } else if (CONDITION_KEYS.includes(entity.key)) {
        conditionEntityIds.push(entity.entity_id);
      } else if (entity.key !== PIN_SYNCED_TO_LOCKS_KEY) {
        mainEntityIds.push(entity.entity_id);
      }
    });
  const pinShouldBeEnabledEntity = lockCodeManagerEntities.find(
    (entity) => entity.key === PIN_SYNCED_TO_LOCKS_KEY
  );
  const calendarEntityId = configEntryData.slots[slotNum];
  if (calendarEntityId) conditionEntityIds.unshift(calendarEntityId);
  return {
    codeEventEntityIds,
    codeSensorEntityIds,
    conditionEntityIds,
    mainEntityIds,
    pinShouldBeEnabledEntity,
    slotNum
  };
}

function maybeGenerateFoldEntityRowCard(
  entities: string[],
  label: string,
  use_fold_entity_row: boolean
) {
  if (entities.length === 0) return [];
  const entityCards = generateEntityCards(entities);
  return use_fold_entity_row
    ? [
        DIVIDER_CARD,
        {
          entities: entityCards,
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

// https://gist.github.com/hagemann/382adfc57adbd5af078dc93feef01fe1
function slugify(value: string, delimiter = '-'): string {
  const a = 'àáâäæãåāăąçćčđďèéêëēėęěğǵḧîïíīįìıİłḿñńǹňôöòóœøōõőṕŕřßśšşșťțûüùúūǘůűųẃẍÿýžźż·';
  const b = `aaaaaaaaaacccddeeeeeeeegghiiiiiiiilmnnnnoooooooooprrsssssttuuuuuuuuuwxyyzzz${delimiter}`;
  const p = new RegExp(a.split('').join('|'), 'g');

  let slugified;

  if (value === '') {
    slugified = '';
  } else {
    slugified = value
      .toString()
      .toLowerCase()
      // Replace special characters
      .replace(p, (c) => b.charAt(a.indexOf(c)))
      // Remove Commas between numbers
      .replace(/(\d),(?=\d)/g, '$1')
      // Replace all non-word characters
      .replace(/[^a-z0-9]+/g, delimiter)
      // Replace multiple delimiters with single delimiter
      .replace(new RegExp(`(${delimiter})\\1+`, 'g'), '$1')
      // Trim delimiter from start of text
      .replace(new RegExp(`^${delimiter}+`), '')
      // Trim delimiter from end of text
      .replace(new RegExp(`${delimiter}+$`), '');

    if (slugified === '') {
      slugified = 'unknown';
    }
  }

  return slugified;
}
