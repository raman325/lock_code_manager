import { ReactiveElement } from 'lit';

import { DOMAIN } from './const';
import { ConfigEntry, EntityRegistryEntry, HomeAssistant } from './ha_type_stubs';
import { compareAndSortEntities, createLockCodeManagerEntity, getSlotMapping } from './helpers';
import {
  ConfigEntryToEntities,
  LockCodeManagerDashboardStrategyConfig,
  LockCodeManagerEntityEntry,
  LockCodeManagerViewStrategyConfig,
  SlotMapping
} from './types';

class LockCodeManagerDashboard extends ReactiveElement {
  static async generate(config: LockCodeManagerDashboardStrategyConfig, hass: HomeAssistant) {
    const [lockConfigEntries, entityEntries] = await Promise.all([
      hass.callWS<ConfigEntry[]>({
        domain: DOMAIN,
        type: 'config_entries/get'
      }),
      hass.callWS<EntityRegistryEntry[]>({
        type: 'config/entity_registry/list'
      })
    ]);
    const lockConfigEntryIDs = lockConfigEntries.map((configEntry) => configEntry.entry_id);
    const lockEntityEntries: LockCodeManagerEntityEntry[] = entityEntries
      .filter((entity) => lockConfigEntryIDs.includes(entity.config_entry_id))
      .map((entity) => createLockCodeManagerEntity(entity));

    const configEntriesToEntities: ConfigEntryToEntities[] = lockConfigEntries.map(
      (configEntry) => {
        return {
          configEntry,
          entities: lockEntityEntries.filter(
            (entity) => configEntry.entry_id === entity.config_entry_id
          )
        };
      }
    );

    return {
      title: 'Lock Code Manager',
      views: configEntriesToEntities.map((configEntryToEntities) => {
        return {
          strategy: {
            type: 'lock-code-manager',
            ...configEntryToEntities
          }
        };
      })
    };
  }
}

class LockCodeManagerViewStrategy extends ReactiveElement {
  static async generate(config: LockCodeManagerViewStrategyConfig, hass: HomeAssistant) {
    const { configEntry, entities } = config.strategy;

    entities.sort((entityA, entityB) => compareAndSortEntities(entityA, entityB));
    const slots = [
      ...new Set(entities.map((entity) => parseInt(entity.unique_id.split('|')[1], 10)))
    ];
    const slotMappings: SlotMapping[] = slots.map((slotNum) =>
      getSlotMapping(hass, slotNum, entities)
    );

    return {
      badges: entities
        .filter((entity) => entity.key === 'pin_enabled')
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
      path: configEntry.entry_id,
      title: configEntry.title
    };
  }
}

declare global {
  interface HTMLElementTagNameMap {
    'll-strategy-dashboard-lock-code-manager': LockCodeManagerDashboard;
    'll-strategy-view-lock-code-manager': LockCodeManagerViewStrategy;
  }
}

customElements.define('ll-strategy-dashboard-lock-code-manager', LockCodeManagerDashboard);
customElements.define('ll-strategy-view-lock-code-manager', LockCodeManagerViewStrategy);
