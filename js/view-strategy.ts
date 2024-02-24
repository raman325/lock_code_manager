import { ReactiveElement } from 'lit';

import { DEFAULT_INCLUDE_CODE_SLOT_SENSORS } from './const';
import { HomeAssistant } from './ha_type_stubs';
import { generateView } from './helpers';
import { LockCodeManagerEntitiesResponse, LockCodeManagerViewStrategyConfig } from './types';

export class LockCodeManagerViewStrategy extends ReactiveElement {
    static async generate(config: LockCodeManagerViewStrategyConfig, hass: HomeAssistant) {
        const { config_entry_id, config_entry_title } = config;
        if (
            (config_entry_id === undefined && config_entry_title === undefined) ||
            (config_entry_id !== undefined && config_entry_title !== undefined)
        ) {
            return {
                badges: [],
                cards: [
                    {
                        content:
                            '## ERROR: Either `config_entry_title` or `config_entry_id` must ' +
                            'be provided in the view config, but not both!',
                        type: 'markdown'
                    }
                ],
                title: 'Lock Code Manager'
            };
        }
        try {
            const [configEntryId, configEntryTitle, entities] =
                await hass.callWS<LockCodeManagerEntitiesResponse>({
                    config_entry_id,
                    config_entry_title,
                    type: 'lock_code_manager/get_config_entry_entities'
                });
            return generateView(
                hass,
                configEntryId,
                configEntryTitle,
                entities,
                config.include_code_slot_sensors ?? DEFAULT_INCLUDE_CODE_SLOT_SENSORS
            );
        } catch (err) {
            const content =
                config_entry_id !== undefined
                    ? `with ID \`${config_entry_id}\``
                    : `called \`${config_entry_title}\``;
            return {
                badges: [],
                cards: [
                    {
                        content: `## ERROR: No Lock Code Manager configuration ${content} found!`,
                        type: 'markdown'
                    }
                ],
                title: 'Lock Code Manager'
            };
        }
    }
}
