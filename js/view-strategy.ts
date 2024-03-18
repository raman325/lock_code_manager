import { ReactiveElement } from 'lit';

import { DEFAULT_INCLUDE_CODE_SLOT_SENSORS, DEFAULT_INCLUDE_IN_SYNC_SENSORS } from './const';
import { generateView } from './generate-view';
import { HomeAssistant } from './ha_type_stubs';
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
            const { config_entry, entities } = await hass.callWS<LockCodeManagerEntitiesResponse>({
                config_entry_id,
                config_entry_title,
                type: 'lock_code_manager/get_config_entry_entities'
            });
            return generateView(
                hass,
                config_entry.entry_id,
                config_entry.title,
                entities,
                config.include_code_slot_sensors ?? DEFAULT_INCLUDE_CODE_SLOT_SENSORS,
                config.include_in_sync_sensors ?? DEFAULT_INCLUDE_IN_SYNC_SENSORS
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
