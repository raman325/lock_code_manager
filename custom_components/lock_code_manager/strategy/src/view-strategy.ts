import { ReactiveElement } from 'lit';

import { HomeAssistant } from './ha_type_stubs';
import { compareAndSortEntities, getSlotMapping } from './helpers';
import { LockCodeManagerViewStrategyConfig, SlotMapping } from './types';

export class LockCodeManagerViewStrategy extends ReactiveElement {
  static async generate(config: LockCodeManagerViewStrategyConfig, hass: HomeAssistant) {
    const { configEntry, entities } = config;

    const sortedEntities = [...entities].sort((entityA, entityB) =>
      compareAndSortEntities(entityA, entityB)
    );
    const slots = [
      ...new Set(sortedEntities.map((entity) => parseInt(entity.unique_id.split('|')[1], 10)))
    ];
    const slotMappings: SlotMapping[] = slots.map((slotNum) =>
      getSlotMapping(hass, slotNum, sortedEntities)
    );

    return {
      badges: sortedEntities
        .filter((entity) => entity.key === 'pin_enabled')
        .map((entity) => entity.entity_id),
      cards: slotMappings.map((slotMapping) => {
        return {
          cards: [
            {
              content: `## Code Slot ${slotMapping.slotNum}`,
              type: 'markdown'
            },
            {
              entities: [
                ...slotMapping.mainEntityIds.map((entityId) => {
                  return {
                    entity: entityId
                  };
                }),
                {
                  type: 'divider'
                },
                {
                  entity: slotMapping.pinShouldBeEnabledEntity.entity_id
                },
                {
                  entities: slotMapping.conditionEntityIds.map((entityId) => {
                    return {
                      entity: entityId
                    };
                  }),

                  head: {
                    label: 'Conditions',
                    type: 'section'
                  },
                  type: 'custom:fold-entity-row'
                },
                {
                  entities: slotMapping.codeSensorEntityIds.map((entityId) => {
                    return {
                      entity: entityId
                    };
                  }),
                  head: {
                    label: 'Code sensors for each lock',
                    type: 'section'
                  },
                  type: 'custom:fold-entity-row'
                }
              ],
              show_header_toggle: false,
              type: 'entities'
            }
          ],
          type: 'vertical-stack'
        };
      }),
      panel: false,
      path: configEntry.entry_id,
      title: configEntry.title
    };
  }
}
