import { STATE_NOT_RUNNING } from 'home-assistant-js-websocket';
import { ReactiveElement } from 'lit';

import {
    DEFAULT_CODE_DATA_VIEW_CODE_DISPLAY,
    DEFAULT_INCLUDE_CODE_DATA_VIEW,
    DEFAULT_INCLUDE_CODE_SLOT_SENSORS,
    DEFAULT_INCLUDE_IN_SYNC_SENSORS,
    DEFAULT_USE_SLOT_CARDS
} from './const';
import { generateView } from './generate-view';
import { HomeAssistant } from './ha_type_stubs';
import {
    createErrorView,
    createStartingView,
    formatConfigEntryNotFoundError,
    validateViewStrategyConfig
} from './strategy-utils';
import { LockCodeManagerEntitiesResponse, LockCodeManagerViewStrategyConfig } from './types';

export class LockCodeManagerViewStrategy extends ReactiveElement {
    static async generate(config: LockCodeManagerViewStrategyConfig, hass: HomeAssistant) {
        const { config_entry_id, config_entry_title } = config;

        if (hass.config.state === STATE_NOT_RUNNING) {
            return createStartingView();
        }

        const validation = validateViewStrategyConfig(config);
        if (!validation.valid) {
            return createErrorView(
                '## ERROR: Either `config_entry_title` or `config_entry_id` must ' +
                    'be provided in the view config, but not both!'
            );
        }

        try {
            const { config_entry, entities } = await hass.callWS<LockCodeManagerEntitiesResponse>({
                config_entry_id,
                config_entry_title,
                type: 'lock_code_manager/get_config_entry_entities'
            });
            return generateView(
                hass,
                config_entry,
                entities,
                config.include_code_slot_sensors ?? DEFAULT_INCLUDE_CODE_SLOT_SENSORS,
                config.include_in_sync_sensors ?? DEFAULT_INCLUDE_IN_SYNC_SENSORS,
                config.include_code_data_view ?? DEFAULT_INCLUDE_CODE_DATA_VIEW,
                config.code_data_view_code_display ?? DEFAULT_CODE_DATA_VIEW_CODE_DISPLAY,
                config.use_slot_cards ?? DEFAULT_USE_SLOT_CARDS
            );
        } catch {
            return createErrorView(
                formatConfigEntryNotFoundError(config_entry_id, config_entry_title)
            );
        }
    }
}
