import { ConfigEntry, EntityRegistryEntry } from './ha_type_stubs';

export interface LockCodeManagerEntityEntry extends EntityRegistryEntry {
  key: string;
  lockEntityId?: string;
  slotNum: number;
}

export interface LockCodeManagerDashboardStrategyConfig {
  strategy: {
    type: 'lock-code-manager';
  };
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
  strategy: {
    configEntry: ConfigEntry;
    entities: LockCodeManagerEntityEntry[];
    type: 'lock-code-manager';
  };
}
