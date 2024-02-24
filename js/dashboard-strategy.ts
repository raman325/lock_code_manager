import { ReactiveElement } from 'lit';

import { DEFAULT_INCLUDE_CODE_SLOT_SENSORS } from './const';
import { HomeAssistant } from './ha_type_stubs';
import { generateView } from './helpers';
import { LockCodeManagerDashboardStrategyConfig, LockCodeManagerEntitiesResponse } from './types';

export class LockCodeManagerDashboardStrategy extends ReactiveElement {
    static async generate(config: LockCodeManagerDashboardStrategyConfig, hass: HomeAssistant) {
        const configEntriesAndEntities = await hass.callWS<LockCodeManagerEntitiesResponse[]>({
            type: 'lock_code_manager/get_config_entries_to_entities'
        });

        if (configEntriesAndEntities.length === 0) {
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

        const views = await Promise.all(
            configEntriesAndEntities.map(([configEntryId, configEntryTitle, entities]) =>
                generateView(
                    hass,
                    configEntryId,
                    configEntryTitle,
                    entities,
                    config.include_code_slot_sensors ?? DEFAULT_INCLUDE_CODE_SLOT_SENSORS
                )
            )
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
