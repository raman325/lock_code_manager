import { ReactiveElement } from 'lit';

import { HomeAssistant } from './ha_type_stubs';
import { slugify } from './slugify';
import { createErrorView } from './strategy-utils';
import { GetConfigEntriesResponse, LockCodeManagerDashboardStrategyConfig } from './types';

/** Message shown when no LCM configurations exist */
export const NO_CONFIG_MESSAGE = '# No Lock Code Manager configurations found!';

/** Zero-width space used as placeholder title for single-view hack */
export const ZERO_WIDTH_SPACE = '​';

export class LockCodeManagerDashboardStrategy extends ReactiveElement {
    static async generate(config: LockCodeManagerDashboardStrategyConfig, hass: HomeAssistant) {
        const configEntries = await hass.callWS<GetConfigEntriesResponse>({
            domain: 'lock_code_manager',
            type: 'config_entries/get'
        });

        if (configEntries.length === 0) {
            return {
                title: 'Lock Code Manager',
                views: [createErrorView(NO_CONFIG_MESSAGE)]
            };
        }

        const views: object[] = configEntries.map((configEntry) => {
            return {
                path: slugify(configEntry.title),
                strategy: {
                    config_entry_id: configEntry.entry_id,
                    include_code_slot_sensors: config.include_code_slot_sensors,
                    include_in_sync_sensors: config.include_in_sync_sensors,
                    type: 'custom:lock-code-manager'
                },
                title: configEntry.title
            };
        });

        // Single view hack: add placeholder to force tab visibility
        if (views.length === 1) {
            views.push({ title: ZERO_WIDTH_SPACE });
        }

        return {
            title: 'Lock Code Manager',
            views
        };
    }
}
