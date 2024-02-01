import { ReactiveElement } from "lit";

import {
  compareAndSortEntities,
  createLockCodeManagerEntity,
  getSlotMapping,
} from "./helpers";
import {
  ConfigEntryToEntities,
  LockCodeManagerDashboardStrategyConfig,
  LockCodeManagerEntity,
  LockCodeManagerViewStrategyConfig,
  SlotMapping,
} from "./types";
import { getConfigEntries } from "../ha-frontend/src/data/config_entries";
import {
  EntityRegistryEntry,
  fetchEntityRegistry,
} from "../ha-frontend/src/data/entity_registry";
import { LovelaceConfig } from "../ha-frontend/src/data/lovelace/config/types";
import { LovelaceViewConfig } from "../ha-frontend/src/data/lovelace/config/view";
import { HomeAssistant } from "../ha-frontend/src/types";

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
          ...configEntryToEntities,
        },
      })),
    };
  }
}

class LockCodeManagerViewStrategy extends ReactiveElement {
  static async generate(
    config: LockCodeManagerViewStrategyConfig,
    hass: HomeAssistant, // eslint-disable-line @typescript-eslint/no-unused-vars
  ): Promise<LovelaceViewConfig> {
    const { configEntry, entities } = config.strategy;

    entities.sort((entityA, entityB) =>
      compareAndSortEntities(entityA, entityB),
    );
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
