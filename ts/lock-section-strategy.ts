import { ReactiveElement } from 'lit';

import { DEFAULT_CODE_DISPLAY } from './const';
import { HomeAssistant, LovelaceSectionConfig } from './ha_type_stubs';
import { LockCodeManagerLockSectionStrategyConfig } from './types';

export class LockCodeManagerLockSectionStrategy extends ReactiveElement {
    static async generate(
        config: LockCodeManagerLockSectionStrategyConfig,
        _hass: HomeAssistant
    ): Promise<LovelaceSectionConfig> {
        const { code_display = DEFAULT_CODE_DISPLAY, lock_entity_id } = config;

        return {
            cards: [
                {
                    code_display,
                    lock_entity_id,
                    type: 'custom:lcm-lock-codes'
                }
            ],
            type: 'grid'
        };
    }
}
