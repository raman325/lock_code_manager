import { STATE_NOT_RUNNING } from 'home-assistant-js-websocket';
import { ReactiveElement } from 'lit';

import {
    DEFAULT_CODE_DISPLAY,
    DEFAULT_SHOW_ALL_CODES_FOR_LOCKS,
    DEFAULT_SHOW_CODE_SENSORS,
    DEFAULT_SHOW_CONDITIONS,
    DEFAULT_SHOW_LOCK_STATUS,
    DEFAULT_SHOW_LOCK_SYNC,
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

            // Map legacy options to new names (new options take precedence)
            const showCodeSensors =
                config.show_code_sensors ??
                config.include_code_slot_sensors ??
                DEFAULT_SHOW_CODE_SENSORS;
            const showLockSync =
                config.show_lock_sync ?? config.include_in_sync_sensors ?? DEFAULT_SHOW_LOCK_SYNC;
            const codeDisplay =
                config.code_display ?? config.code_data_view_code_display ?? DEFAULT_CODE_DISPLAY;

            return generateView(
                hass,
                config_entry,
                entities,
                showCodeSensors,
                showLockSync,
                config.show_all_codes_for_locks ?? DEFAULT_SHOW_ALL_CODES_FOR_LOCKS,
                codeDisplay,
                config.use_slot_cards ?? DEFAULT_USE_SLOT_CARDS,
                config.show_conditions ?? DEFAULT_SHOW_CONDITIONS,
                config.show_lock_status ?? DEFAULT_SHOW_LOCK_STATUS,
                config.collapsed_sections
            );
        } catch {
            return createErrorView(
                formatConfigEntryNotFoundError(config_entry_id, config_entry_title)
            );
        }
    }
}
