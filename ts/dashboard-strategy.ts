import { ReactiveElement } from 'lit';

import { DEFAULT_CODE_DISPLAY, DEFAULT_SHOW_ALL_CODES_FOR_LOCKS } from './const';
import { HomeAssistant } from './ha_type_stubs';
import { slugify } from './slugify';
import { createErrorView } from './strategy-utils';
import {
    GetConfigEntriesResponse,
    LockCodeManagerConfigEntryData,
    LockCodeManagerDashboardStrategyConfig
} from './types';

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
                    code_display: config.code_display,
                    collapsed_sections: config.collapsed_sections,
                    config_entry_id: configEntry.entry_id,
                    show_code_sensors: config.show_code_sensors,
                    show_conditions: config.show_conditions,
                    show_lock_status: config.show_lock_status,
                    show_lock_sync: config.show_lock_sync,
                    type: 'custom:lock-code-manager',
                    use_slot_cards: config.use_slot_cards
                },
                title: configEntry.title
            };
        });

        const showAllCodesForLocks =
            config.show_all_codes_for_locks ?? DEFAULT_SHOW_ALL_CODES_FOR_LOCKS;
        if (showAllCodesForLocks) {
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

            const sortedLockEntityIds = Array.from(lockEntityIds).sort((a, b) => {
                const nameA = hass.states[a]?.attributes?.friendly_name ?? a;
                const nameB = hass.states[b]?.attributes?.friendly_name ?? b;
                return nameA.localeCompare(nameB, undefined, { sensitivity: 'base' });
            });

            const lockCards = sortedLockEntityIds.map((lockEntityId) => {
                return {
                    code_display: config.code_display ?? DEFAULT_CODE_DISPLAY,
                    lock_entity_id: lockEntityId,
                    type: 'custom:lcm-lock-codes-card'
                };
            });

            // Wrap in a grid for responsive multi-column layout
            const cards =
                lockCards.length > 0
                    ? [
                          {
                              cards: lockCards,
                              columns: Math.min(lockCards.length, 3),
                              square: false,
                              type: 'grid'
                          }
                      ]
                    : [
                          {
                              content: '# No locks found to display.',
                              type: 'markdown'
                          }
                      ];

            views.push({
                cards,
                icon: 'mdi:lock-smart',
                path: 'user-codes',
                title: 'User Codes'
            });
        }

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
