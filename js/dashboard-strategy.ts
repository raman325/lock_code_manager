import { ReactiveElement } from 'lit';

import { DEFAULT_INCLUDE_CODE_SLOT_SENSORS } from './const';
import { HomeAssistant } from './ha_type_stubs';
import { generateView, slugify } from './helpers';
import { GetConfigEntriesResponse, LockCodeManagerDashboardStrategyConfig } from './types';

export class LockCodeManagerDashboardStrategy extends ReactiveElement {
    static async generate(config: LockCodeManagerDashboardStrategyConfig, hass: HomeAssistant) {
        const configEntries = await hass.callWS<GetConfigEntriesResponse>({
            domain: 'lock_code_manager',
            type: 'config_entries/get'
        });

        if (configEntries.length === 0) {
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

        const views: object[] = await Promise.all(
            configEntries.map((configEntry) => {
                return {
                    path: slugify(configEntry.title),
                    strategy: {
                        config_entry_id: configEntry.entry_id,
                        include_code_slot_sensors:
                            config.include_code_slot_sensors ?? DEFAULT_INCLUDE_CODE_SLOT_SENSORS,
                        type: 'custom:lock-code-manager'
                    },
                    title: configEntry.title
                };
            })
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
