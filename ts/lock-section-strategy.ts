import { ReactiveElement } from 'lit';

import { DEFAULT_CODE_DISPLAY } from './const';
import { HomeAssistant, LovelaceSectionConfig } from './ha_type_stubs';
import { LockCodeManagerLockSectionStrategyConfig } from './types';

export class LockCodeManagerLockSectionStrategy extends ReactiveElement {
    static async generate(
        config: LockCodeManagerLockSectionStrategyConfig,
        hass: HomeAssistant
    ): Promise<LovelaceSectionConfig> {
        const { code_display = DEFAULT_CODE_DISPLAY, lock_entity_id } = config;

        // Get the lock's friendly name for the section title
        const lockState = hass.states[lock_entity_id];
        const lockName = lockState?.attributes?.friendly_name ?? lock_entity_id;

        return {
            cards: [
                {
                    code_display,
                    lock_entity_id,
                    type: 'custom:lcm-lock-codes'
                }
            ],
            title: lockName,
            type: 'grid'
        };
    }
}
