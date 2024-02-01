import { ReactiveElement } from "lit";
import {
  LovelaceStrategyViewConfig,
  LovelaceViewConfig,
} from "../ha-frontend/src/data/lovelace/config/view";
import { HomeAssistant } from "../ha-frontend/src/types";
import {
  EntityRegistryEntry,
  fetchEntityRegistry,
} from "../ha-frontend/src/data/entity_registry";
import {
  ConfigEntry,
  getConfigEntries,
} from "../ha-frontend/src/data/config_entries";
import {
  LovelaceConfig,
  LovelaceDashboardStrategyConfig,
} from "../ha-frontend/src/data/lovelace/config/types";

const codeKey = "code";
const pinShouldBeEnabledKey = "pin_should_be_enabled";
const conditionKeys = ["calendar", "number_of_uses"];
const keyOrder = [
  "name",
  "enabled",
  "pin",
  pinShouldBeEnabledKey,
  ...conditionKeys,
  codeKey,
];

interface LockCodeManagerEntity extends EntityRegistryEntry {
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

type ConfigEntryToEntities = {
  configEntry: ConfigEntry;
  entities: LockCodeManagerEntity[];
};

function createLockCodeManagerEntity(
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

class LockCodeManagerDashboard extends ReactiveElement {
  static async generate(
    config: LockCodeManagerDashboardStrategyConfig,
    hass: HomeAssistant,
  ): Promise<LovelaceConfig> {
    const configEntriesToEntities: ConfigEntryToEntities[] = await Promise.all([
      getConfigEntries(hass, { domain: "lock_code_manager" }),
      fetchEntityRegistry(hass.connection) as Promise<EntityRegistryEntry[]>,
    ]).then(([configEntries, entities]) => {
      const lockCodeManagerEntities: LockCodeManagerEntity[] = entities.map(
        (entity) => createLockCodeManagerEntity(entity),
      );
      return configEntries.map((configEntry) => {
        return {
          configEntry,
          entities: lockCodeManagerEntities.filter(
            (entity) => configEntry.entry_id === entity.config_entry_id,
          ),
        };
      });
    });

    return {
      title: "Lock Code Manager",
      views: configEntriesToEntities.map((configEntryToEntities) => ({
        strategy: {
          type: "lock-code-manager",
          ...configEntriesToEntities,
        },
      })),
    };
  }
}

function getUniqueId(config_entry: ConfigEntry, slot_num: number, key: string) {
  return `${config_entry.entry_id}|${slot_num}|${key}`;
}

type SlotMapping = {
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

function getSlotMapping(
  slotNum: number,
  entities: LockCodeManagerEntity[],
): SlotMapping {
  const mainEntities: LockCodeManagerEntity[] = [];
  const conditionEntities: LockCodeManagerEntity[] = [];
  const codeSensorEntities: LockCodeManagerEntity[] = [];
  entities
    .filter((entity) => entity.slotNum === slotNum)
    .forEach((entity) => {
      if (entity.key === codeKey) {
        codeSensorEntities.push(entity);
      } else if (conditionKeys.includes(entity.key)) {
        conditionEntities.push(entity);
      } else if (entity.key !== pinShouldBeEnabledKey) {
        mainEntities.push(entity);
      }
    });
  const pinShouldBeEnabledEntity = entities.find(
    (entity) => entity.key === pinShouldBeEnabledKey,
  ) as LockCodeManagerEntity;
  return {
    slotNum,
    mainEntities,
    pinShouldBeEnabledEntity,
    conditionEntities,
    codeSensorEntities,
  };
}

function compareEntities(
  entityA: LockCodeManagerEntity,
  entityB: LockCodeManagerEntity,
) {
  // sort by slot number
  if (entityA.slotNum < entityB.slotNum) return -1;
  // sort by key order
  if (
    entityA.slotNum == entityB.slotNum &&
    keyOrder.indexOf(entityA.key) < keyOrder.indexOf(entityB.key)
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

class LockCodeManagerViewStrategy extends ReactiveElement {
  static async generate(
    config: LockCodeManagerViewStrategyConfig,
    hass: HomeAssistant,
  ): Promise<LovelaceViewConfig> {
    const { configEntry, entities } = config.strategy;

    entities.sort((entityA, entityB) => compareEntities(entityA, entityB));
    const slots = [
      ...new Set(
        entities.map((entity) => parseInt(entity.unique_id.split("|")[1])),
      ),
    ];
    const slotMappings: SlotMapping[] = slots.map((slotNum) =>
      getSlotMapping(slotNum, entities),
    );

    return {
      title: configEntry.title,
      path: configEntry.entry_id,
      panel: false,
      badges: entities
        .filter((entity) => entity.key === "pin_enabled")
        .map((entity) => entity.entity_id),
      cards: slotMappings.map((slotMapping) => {
        return {
          type: "vertical-stack",
          cards: [
            {
              type: "markdown",
              content: `## Code Slot ${slotMapping.slotNum}`,
            },
            {
              type: "entities",
              show_header_toggle: false,
              entities: [
                ...slotMapping.mainEntities.map((entity) => ({
                  entity: entity.entity_id,
                })),
                {
                  type: "divider",
                },
                {
                  entity: slotMapping.pinShouldBeEnabledEntity.entity_id,
                },
                {
                  type: "custom:fold-entity-row",
                  head: {
                    type: "section",
                    label: "Conditions",
                  },
                  entities: slotMapping.conditionEntities.map((entity) => ({
                    entity: entity.entity_id,
                  })),
                },
                {
                  type: "custom:fold-entity-row",
                  head: {
                    type: "section",
                    label: "Code sensors for each lock",
                  },
                  entities: slotMapping.codeSensorEntities.map((entity) => ({
                    entity: entity.entity_id,
                  })),
                },
              ],
            },
          ],
        };
      }),
    };
  }
}

declare global {
  interface HTMLElementTagNameMap {
    "ll-strategy-dashboard-lock-code-manager": LockCodeManagerDashboard;
    "ll-strategy-view-lock-code-manager": LockCodeManagerViewStrategy;
  }
}

customElements.define(
  "ll-strategy-dashboard-lock-code-manager",
  LockCodeManagerDashboard,
);
customElements.define(
  "ll-strategy-view-lock-code-manager",
  LockCodeManagerViewStrategy,
);
