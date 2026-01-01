import { ReactiveElement } from 'lit';

import { DEFAULT_INCLUDE_CODE_DATA_VIEW } from './const';
import { HomeAssistant } from './ha_type_stubs';
import { slugify } from './slugify';
import {
    GetConfigEntriesResponse,
    LockCodeManagerConfigEntryData,
    LockCodeManagerDashboardStrategyConfig
} from './types';

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
                        include_code_data_view: config.include_code_data_view,
                        include_code_slot_sensors: config.include_code_slot_sensors,
                        include_in_sync_sensors: config.include_in_sync_sensors,
                        type: 'custom:lock-code-manager'
                    },
                    title: configEntry.title
                };
            })
        );

        const includeCodeDataView = config.include_code_data_view ?? DEFAULT_INCLUDE_CODE_DATA_VIEW;
        if (includeCodeDataView) {
            const lockEntityIds = new Set<string>();
            await Promise.all(
                configEntries.map(async (configEntry) => {
                    const data = await hass.callWS<LockCodeManagerConfigEntryData>({
                        config_entry_id: configEntry.entry_id,
                        type: 'lock_code_manager/get_slot_calendar_data'
                    });
                    data.locks.forEach((lockEntityId) => lockEntityIds.add(lockEntityId));
                })
            );

            const cards = Array.from(lockEntityIds).map((lockEntityId) => {
                return {
                    lock_entity_id: lockEntityId,
                    type: 'custom:lock-code-manager-lock-data'
                };
            });
            views.push({
                cards:
                    cards.length > 0
                        ? cards
                        : [
                              {
                                  content: '# No locks found to display.',
                                  type: 'markdown'
                              }
                          ],
                path: 'lock-codes',
                title: 'Lock Codes'
            });
        }

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
