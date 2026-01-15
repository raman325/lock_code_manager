import { ReactiveElement } from 'lit';

import {
    DEFAULT_CODE_DISPLAY,
    DEFAULT_SHOW_CODE_SENSORS,
    DEFAULT_SHOW_CONDITIONS,
    DEFAULT_SHOW_LOCK_STATUS,
    DEFAULT_SHOW_LOCK_SYNC,
    DEFAULT_USE_SLOT_CARDS,
    FOLD_ENTITY_ROW_SEARCH_STRING
} from './const';
import { createLockCodeManagerEntity, generateSlotCard, getSlotMapping } from './generate-view';
import {
    EntityRegistryEntry,
    HomeAssistant,
    LovelaceCardConfig,
    LovelaceResource,
    LovelaceSectionConfig
} from './ha_type_stubs';
import {
    LockCodeManagerConfigEntryDataResponse,
    LockCodeManagerSlotSectionStrategyConfig
} from './types';

export class LockCodeManagerSlotSectionStrategy extends ReactiveElement {
    static async generate(
        config: LockCodeManagerSlotSectionStrategyConfig,
        hass: HomeAssistant
    ): Promise<LovelaceSectionConfig> {
        const {
            code_display = DEFAULT_CODE_DISPLAY,
            collapsed_sections,
            config_entry_id,
            show_code_sensors = DEFAULT_SHOW_CODE_SENSORS,
            show_conditions = DEFAULT_SHOW_CONDITIONS,
            show_lock_status = DEFAULT_SHOW_LOCK_STATUS,
            show_lock_sync = DEFAULT_SHOW_LOCK_SYNC,
            slot,
            use_slot_cards = DEFAULT_USE_SLOT_CARDS
        } = config;

        // Use new slot card by default
        if (use_slot_cards) {
            const card: LovelaceCardConfig = {
                code_display,
                config_entry_id,
                show_code_sensors,
                show_conditions,
                show_lock_status,
                show_lock_sync,
                slot,
                type: 'custom:lcm-slot'
            };

            if (collapsed_sections && collapsed_sections.length > 0) {
                card.collapsed_sections = collapsed_sections;
            }

            return {
                cards: [card],
                type: 'grid'
            };
        }

        // Legacy entities card mode - fetch data and generate
        const [configEntryData, lovelaceResources] = await Promise.all([
            hass.callWS<LockCodeManagerConfigEntryDataResponse>({
                config_entry_id,
                type: 'lock_code_manager/get_config_entry_data'
            }),
            hass.callWS<LovelaceResource[]>({
                type: 'lovelace/resources'
            })
        ]);

        const sortedEntities = configEntryData.entities
            .map((entity: EntityRegistryEntry) => createLockCodeManagerEntity(entity))
            .sort((a, b) => a.slotNum - b.slotNum);

        const slotMapping = getSlotMapping(slot, sortedEntities, configEntryData);

        const useFoldEntityRow =
            lovelaceResources.filter((resource) =>
                resource.url.includes(FOLD_ENTITY_ROW_SEARCH_STRING)
            ).length > 0;

        const card = generateSlotCard(
            hass,
            configEntryData.config_entry,
            slotMapping,
            useFoldEntityRow,
            show_code_sensors,
            show_lock_sync
        );

        return {
            cards: [card],
            type: 'grid'
        };
    }
}
