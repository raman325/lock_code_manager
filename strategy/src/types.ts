import { ConfigEntry } from "../ha-frontend/src/data/config_entries";
import { EntityRegistryEntry } from "../ha-frontend/src/data/entity_registry";
import { LovelaceDashboardStrategyConfig } from "../ha-frontend/src/data/lovelace/config/types";
import { LovelaceStrategyViewConfig } from "../ha-frontend/src/data/lovelace/config/view";

export interface LockCodeManagerEntity extends EntityRegistryEntry {
  slotNum: number;
  key: string;
  lockEntityId?: string;
}

export interface LockCodeManagerDashboardStrategyConfig
  extends LovelaceDashboardStrategyConfig {
  strategy: {
    type: "lock-code-manager";
  };
}

export type ConfigEntryToEntities = {
  configEntry: ConfigEntry;
  entities: LockCodeManagerEntity[];
};

export type SlotMapping = {
  slotNum: number;
  mainEntities: LockCodeManagerEntity[]; // primary entities to always show
  pinShouldBeEnabledEntity: LockCodeManagerEntity;
  conditionEntities: LockCodeManagerEntity[]; // conditional entities
  codeSensorEntities: LockCodeManagerEntity[]; // code sensor entities
};

export interface LockCodeManagerViewStrategyConfig
  extends LovelaceStrategyViewConfig {
  strategy: {
    type: "lock-code-manager";
    configEntry: ConfigEntry;
    entities: LockCodeManagerEntity[];
  };
}
