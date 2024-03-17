export const CODE_SENSOR_KEY = 'code';
export const CODE_EVENT_KEY = 'pin_used';
export const ACTIVE_KEY = 'active';
export const IN_SYNC_KEY = 'in_sync';
export const CONDITION_KEYS = ['number_of_uses'];
export const DIVIDER_CARD = {
    type: 'divider'
};
export const KEY_ORDER = [
    'name',
    'enabled',
    'pin',
    ACTIVE_KEY,
    ...CONDITION_KEYS,
    IN_SYNC_KEY,
    CODE_SENSOR_KEY,
    CODE_EVENT_KEY
];
export const DOMAIN = 'lock_code_manager';
export const DEFAULT_INCLUDE_CODE_SLOT_SENSORS = false;

export const FOLD_ENTITY_ROW_SEARCH_STRING = 'fold-entity-row.js';
export const FOLD_ENTITY_ROW_REPO_NAME = 'thomasloven/lovelace-fold-entity-row';
