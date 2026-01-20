import { ReactiveElement } from 'lit';

import {
    DEFAULT_CODE_DISPLAY,
    DEFAULT_SHOW_ALL_LOCK_CARDS_VIEW,
    DEFAULT_SHOW_PER_CONFIGURATION_LOCK_CARDS
} from './const';
import { HomeAssistant } from './ha_type_stubs';
import { slugify } from './slugify';
import { createErrorView } from './strategy-utils';
import {
    GetConfigEntriesResponse,
    LockCodeManagerConfigEntryDataResponse,
    LockCodeManagerDashboardStrategyConfig,
    LockInfo
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

        // show_per_configuration_lock_cards controls whether lock cards appear in per-config views
        // It feeds show_lock_cards to each view strategy
        // Support legacy show_all_codes_for_locks option for backwards compatibility
        const showLockCardsInViews =
            config.show_per_configuration_lock_cards ??
            config.show_all_codes_for_locks ??
            DEFAULT_SHOW_PER_CONFIGURATION_LOCK_CARDS;

        const views: object[] = configEntries.map((configEntry) => {
            return {
                path: slugify(configEntry.title),
                strategy: {
                    code_display: config.code_display,
                    collapsed_sections: config.collapsed_sections,
                    config_entry_id: configEntry.entry_id,
                    show_code_sensors: config.show_code_sensors,
                    show_conditions: config.show_conditions,
                    show_lock_cards: showLockCardsInViews,
                    show_lock_status: config.show_lock_status,
                    show_lock_sync: config.show_lock_sync,
                    type: 'custom:lock-code-manager',
                    use_slot_cards: config.use_slot_cards
                },
                title: configEntry.title
            };
        });

        // show_all_lock_cards_view controls whether the "User Codes" view is added
        // Support legacy show_all_codes_for_locks option for backwards compatibility
        const showAllLockCardsView =
            config.show_all_lock_cards_view ??
            config.show_all_codes_for_locks ??
            DEFAULT_SHOW_ALL_LOCK_CARDS_VIEW;
        if (showAllLockCardsView) {
            const locksMap = new Map<string, LockInfo>();
            await Promise.all(
                configEntries.map(async (configEntry) => {
                    const data = await hass.callWS<LockCodeManagerConfigEntryDataResponse>({
                        config_entry_id: configEntry.entry_id,
                        type: 'lock_code_manager/get_config_entry_data'
                    });
                    data.locks.forEach((lock) => locksMap.set(lock.entity_id, lock));
                })
            );

            const sortedLocks = Array.from(locksMap.values()).sort((a, b) =>
                a.name.localeCompare(b.name, undefined, { sensitivity: 'base' })
            );

            // Create sections for each lock (same layout as view strategy)
            const sections =
                sortedLocks.length > 0
                    ? sortedLocks.map((lock) => {
                          return {
                              cards: [
                                  {
                                      code_display: config.code_display ?? DEFAULT_CODE_DISPLAY,
                                      lock_entity_id: lock.entity_id,
                                      type: 'custom:lcm-lock-codes'
                                  }
                              ],
                              title: lock.name,
                              type: 'grid'
                          };
                      })
                    : undefined;

            const cards =
                sortedLocks.length === 0
                    ? [
                          {
                              content: '# No locks found to display.',
                              type: 'markdown'
                          }
                      ]
                    : undefined;

            views.push({
                ...(cards && { cards }),
                icon: 'mdi:lock-smart',
                path: 'user-codes',
                ...(sections && { sections }),
                title: 'User Codes',
                ...(sections && { type: 'sections' })
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
