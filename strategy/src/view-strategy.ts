import { ReactiveElement } from 'lit';

import { DOMAIN } from './const';
import { ConfigEntry, EntityRegistryEntry, HomeAssistant } from './ha_type_stubs';
import { createLockCodeManagerEntity, generateView } from './helpers';
import { LockCodeManagerEntityEntry, LockCodeManagerViewStrategyConfig } from './types';

export class LockCodeManagerViewStrategy extends ReactiveElement {
  static async generate(config: LockCodeManagerViewStrategyConfig, hass: HomeAssistant) {
    const { config_entry_title } = config;
    const [lockConfigEntries, entityEntries] = await Promise.all([
      hass.callWS<ConfigEntry[]>({
        domain: DOMAIN,
        type: 'config_entries/get'
      }),
      hass.callWS<EntityRegistryEntry[]>({
        type: 'config/entity_registry/list'
      })
    ]);

    const configEntry = lockConfigEntries.find((entry) => entry.title === config_entry_title);
    if (configEntry === undefined) {
      return {
        badges: [],
        cards: [
          {
            content: `# No Lock Code Manager configuration called \`${config_entry_title}\` found!`,
            type: 'markdown'
          }
        ],
        title: 'Lock Code Manager'
      };
    }

    const entities: LockCodeManagerEntityEntry[] = entityEntries
      .filter((entity) => configEntry.entry_id === entity.config_entry_id)
      .map((entity) => createLockCodeManagerEntity(entity));

    return generateView(hass, configEntry, entities);
  }
}
