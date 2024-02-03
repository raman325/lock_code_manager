import { CODE_SENSOR_KEY, CONDITION_KEYS, KEY_ORDER, PIN_SYNCED_TO_LOCKS_KEY } from './const';
import {
  ConfigEntry,
  EntityRegistryEntry,
  HomeAssistant,
  LovelaceViewConfig
} from './ha_type_stubs';
import { LockCodeManagerEntityEntry, SlotMapping } from './types';

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

export function getSlotMapping(
  hass: HomeAssistant,
  slotNum: number,
  lockCodeManagerEntities: LockCodeManagerEntityEntry[]
): SlotMapping {
  const mainEntityIds: string[] = [];
  const conditionEntityIds: string[] = [];
  const codeSensorEntityIds: string[] = [];
  lockCodeManagerEntities
    .filter((entity) => entity.slotNum === slotNum)
    .forEach((entity) => {
      if (entity.key === CODE_SENSOR_KEY) {
        codeSensorEntityIds.push(entity.entity_id);
      } else if (CONDITION_KEYS.includes(entity.key)) {
        conditionEntityIds.push(entity.entity_id);
      } else if (entity.key !== PIN_SYNCED_TO_LOCKS_KEY) {
        mainEntityIds.push(entity.entity_id);
      }
    });
  const pinShouldBeEnabledEntity = lockCodeManagerEntities.find(
    (entity) => entity.key === PIN_SYNCED_TO_LOCKS_KEY
  );
  const calendarEntityId: string | undefined =
    hass.states[pinShouldBeEnabledEntity.entity_id]?.attributes.calendar;
  if (calendarEntityId) conditionEntityIds.unshift(calendarEntityId);
  return {
    codeSensorEntityIds,
    conditionEntityIds,
    mainEntityIds,
    pinShouldBeEnabledEntity,
    slotNum
  };
}

export function compareAndSortEntities(
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

export function generateView(
  hass: HomeAssistant,
  configEntry: ConfigEntry,
  entities: LockCodeManagerEntityEntry[]
): LovelaceViewConfig {
  const sortedEntities = [...entities].sort((entityA, entityB) =>
    compareAndSortEntities(entityA, entityB)
  );
  const slots = [
    ...new Set(sortedEntities.map((entity) => parseInt(entity.unique_id.split('|')[1], 10)))
  ];
  const slotMappings: SlotMapping[] = slots.map((slotNum) =>
    getSlotMapping(hass, slotNum, sortedEntities)
  );

  return {
    badges: sortedEntities
      .filter((entity) => entity.key === 'pin_synced_to_locks')
      .map((entity) => entity.entity_id),
    cards: slotMappings.map((slotMapping) => {
      return {
        cards: [
          {
            content: `## Code Slot ${slotMapping.slotNum}`,
            type: 'markdown'
          },
          {
            entities: [
              ...slotMapping.mainEntityIds.map((entityId) => {
                return {
                  entity: entityId
                };
              }),
              {
                type: 'divider'
              },
              {
                entity: slotMapping.pinShouldBeEnabledEntity.entity_id
              },
              {
                entities: slotMapping.conditionEntityIds.map((entityId) => {
                  return {
                    entity: entityId
                  };
                }),

                head: {
                  label: 'Conditions',
                  type: 'section'
                },
                type: 'custom:fold-entity-row'
              },
              {
                entities: slotMapping.codeSensorEntityIds.map((entityId) => {
                  return {
                    entity: entityId
                  };
                }),
                head: {
                  label: 'Code sensors for each lock',
                  type: 'section'
                },
                type: 'custom:fold-entity-row'
              }
            ],
            show_header_toggle: false,
            type: 'entities'
          }
        ],
        type: 'vertical-stack'
      };
    }),
    panel: false,
    path: slugify(configEntry.title),
    title: configEntry.title
  };
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
