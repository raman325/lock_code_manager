import { ReactiveElement } from 'lit';

import { DOMAIN } from './const';
import { ConfigEntry, EntityRegistryEntry, HomeAssistant } from './ha_type_stubs';
import { createLockCodeManagerEntity, generateView } from './helpers';
import {
  ConfigEntryToEntities,
  LockCodeManagerDashboardStrategyConfig,
  LockCodeManagerEntityEntry
} from './types';

export class LockCodeManagerDashboardStrategy extends ReactiveElement {
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

    if (configEntriesToEntities.length === 0) {
      return {
        title: 'Lock Code Manager',
        views: [
          {
            badges: [],
            cards: [
              {
                content: '# No Lock Code Manager configurations found!',
                type: 'markdown'
              }
            ],
            title: 'Lock Code Manager'
          }
        ]
      };
    }

    const views = configEntriesToEntities.map((configEntryToEntities) =>
      generateView(hass, configEntryToEntities.configEntry, configEntryToEntities.entities)
    );

    if (views.length === 1) {
      views.push({
        // Title is zero width space as a hack to get the view title to show
        title: '​'
      });
    }

    return {
      title: 'Lock Code Manager',
      views
    };
  }
}
