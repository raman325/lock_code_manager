import { CODE_SENSOR_KEY, CONDITION_KEYS, KEY_ORDER, PIN_SHOULD_BE_ENABLED_KEY } from './const';
import { EntityRegistryEntry, HomeAssistant } from './ha_type_stubs';
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
      } else if (entity.key !== PIN_SHOULD_BE_ENABLED_KEY) {
        mainEntityIds.push(entity.entity_id);
      }
    });
  const pinShouldBeEnabledEntity = lockCodeManagerEntities.find(
    (entity) => entity.key === PIN_SHOULD_BE_ENABLED_KEY
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
