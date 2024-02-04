import { ConfigEntry, EntityRegistryEntry } from './ha_type_stubs';

export interface LockCodeManagerEntityEntry extends EntityRegistryEntry {
  key: string;
  lockEntityId?: string;
  slotNum: number;
}

export interface LockCodeManagerDashboardStrategyConfig {
  include_code_slot_sensors?: boolean;
  type: 'custom:lock-code-manager';
  use_fold_entity_row?: boolean;
}

export interface ConfigEntryToEntities {
  configEntry: ConfigEntry;
  entities: LockCodeManagerEntityEntry[];
}

export interface SlotMapping {
  codeEventEntityIds: string[];
  codeSensorEntityIds: string[];
  conditionEntityIds: string[];
  mainEntityIds: string[];
  pinShouldBeEnabledEntity: LockCodeManagerEntityEntry;
  slotNum: number;
}

export interface LockCodeManagerViewStrategyConfig {
  config_entry_id?: string;
  config_entry_title?: string;
  include_code_slot_sensors?: boolean;
  type: 'custom:lock-code-manager';
  use_fold_entity_row?: boolean;
}

export type LockCodeManagerEntitiesResponse = [string, string, EntityRegistryEntry[]];

export interface LockCodeManagerConfigEntryData {
  locks: string[];
  slots: { [key: number]: string | null };
}
