import {
  CODE_SENSOR_KEY,
  CONDITION_KEYS,
  KEY_ORDER,
  PIN_SHOULD_BE_ENABLED_KEY,
} from "./const";
import { LockCodeManagerEntity, SlotMapping } from "./types";
import { EntityRegistryEntry } from "../ha-frontend/src/data/entity_registry";

export function createLockCodeManagerEntity(
  entity: EntityRegistryEntry,
): LockCodeManagerEntity {
  const split = entity.unique_id.split("|");
  return {
    ...entity,
    slotNum: parseInt(split[1]),
    key: split[2],
    lockEntityId: split.length === 4 ? split[3] : undefined,
  };
}

export function getSlotMapping(
  slotNum: number,
  entities: LockCodeManagerEntity[],
): SlotMapping {
  const mainEntities: LockCodeManagerEntity[] = [];
  const conditionEntities: LockCodeManagerEntity[] = [];
  const codeSensorEntities: LockCodeManagerEntity[] = [];
  entities
    .filter((entity) => entity.slotNum === slotNum)
    .forEach((entity) => {
      if (entity.key === CODE_SENSOR_KEY) {
        codeSensorEntities.push(entity);
      } else if (CONDITION_KEYS.includes(entity.key)) {
        conditionEntities.push(entity);
      } else if (entity.key !== PIN_SHOULD_BE_ENABLED_KEY) {
        mainEntities.push(entity);
      }
    });
  const pinShouldBeEnabledEntity = entities.find(
    (entity) => entity.key === PIN_SHOULD_BE_ENABLED_KEY,
  ) as LockCodeManagerEntity;
  return {
    slotNum,
    mainEntities,
    pinShouldBeEnabledEntity,
    conditionEntities,
    codeSensorEntities,
  };
}

export function compareAndSortEntities(
  entityA: LockCodeManagerEntity,
  entityB: LockCodeManagerEntity,
) {
  // sort by slot number
  if (entityA.slotNum < entityB.slotNum) return -1;
  // sort by key order
  if (
    entityA.slotNum == entityB.slotNum &&
    KEY_ORDER.indexOf(entityA.key) < KEY_ORDER.indexOf(entityB.key)
  )
    return -1;
  // sort code sensors alphabetically based on the lock entity_id
  if (
    entityA.slotNum == entityB.slotNum &&
    entityA.key == entityB.key &&
    entityA.key == "code" &&
    (entityA.lockEntityId as string) < (entityB.lockEntityId as string)
  )
    return -1;
  return 1;
}
