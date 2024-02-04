import { ReactiveElement } from 'lit';

import { DEFAULT_INCLUDE_CODE_SLOT_SENSORS, DEFAULT_USE_FOLD_ENTITY_ROW } from './const';
import { HomeAssistant } from './ha_type_stubs';
import { generateView } from './helpers';
import { LockCodeManagerEntitiesResponse, LockCodeManagerViewStrategyConfig } from './types';

export class LockCodeManagerViewStrategy extends ReactiveElement {
  static async generate(config: LockCodeManagerViewStrategyConfig, hass: HomeAssistant) {
    try {
      const [configEntryId, configEntryTitle, entities] =
        await hass.callWS<LockCodeManagerEntitiesResponse>({
          config_entry_title: config.config_entry_title,
          type: 'lock_code_manager/get_config_entry_entities'
        });
      return generateView(
        hass,
        configEntryId,
        configEntryTitle,
        entities,
        config.use_fold_entity_row ?? DEFAULT_USE_FOLD_ENTITY_ROW,
        config.include_code_slot_sensors ?? DEFAULT_INCLUDE_CODE_SLOT_SENSORS
      );
    } catch (err) {
      return {
        badges: [],
        cards: [
          {
            content: `# No Lock Code Manager configuration called \`${config.config_entry_title}\` found!`,
            type: 'markdown'
          }
        ],
        title: 'Lock Code Manager'
      };
    }
  }
}
