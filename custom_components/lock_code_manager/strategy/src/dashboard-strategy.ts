import { ReactiveElement } from 'lit';
// import { customElement } from 'lit/decorators';

import { DOMAIN } from './const';
import { ConfigEntry, EntityRegistryEntry, HomeAssistant } from './ha_type_stubs';
import { createLockCodeManagerEntity } from './helpers';
import {
  ConfigEntryToEntities,
  LockCodeManagerDashboardStrategyConfig,
  LockCodeManagerEntityEntry
} from './types';

// @customElement('ll-strategy-dashboard-lock-code-manager')
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

declare global {
  interface HTMLElementTagNameMap {
    'll-strategy-dashboard-lock-code-manager': LockCodeManagerDashboardStrategy;
  }
}
