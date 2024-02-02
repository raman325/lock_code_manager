import { ConfigEntry, EntityRegistryEntry } from './ha_type_stubs';

export interface LockCodeManagerEntityEntry extends EntityRegistryEntry {
  key: string;
  lockEntityId?: string;
  slotNum: number;
}

export interface LockCodeManagerDashboardStrategyConfig {
  type: 'custom:lock-code-manager';
}

export interface ConfigEntryToEntities {
  configEntry: ConfigEntry;
  entities: LockCodeManagerEntityEntry[];
}

export interface SlotMapping {
  codeSensorEntityIds: string[];
  conditionEntityIds: string[];
  mainEntityIds: string[];
  pinShouldBeEnabledEntity: LockCodeManagerEntityEntry;
  slotNum: number;
}

export interface LockCodeManagerViewStrategyConfig {
  configEntry: ConfigEntry;
  entities: LockCodeManagerEntityEntry[];
  type: 'custom:lock-code-manager';
}
