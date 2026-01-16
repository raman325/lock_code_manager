export const CODE_SENSOR_KEY = 'code';
export const CODE_EVENT_KEY = 'pin_used';
export const ACTIVE_KEY = 'active';
export const IN_SYNC_KEY = 'in_sync';

// Condition keys
export const CONDITION_NUMBER_OF_USES = 'number_of_uses';
export const CONDITION_CALENDAR = 'calendar';
export const CONDITION_ENTITY = 'condition_entity';
export const CONDITION_KEYS = [CONDITION_NUMBER_OF_USES, CONDITION_CALENDAR, CONDITION_ENTITY];

// Supported condition entity domains
export const CONDITION_ENTITY_DOMAINS = [
    'calendar',
    'binary_sensor',
    'switch',
    'schedule',
    'input_boolean'
] as const;

export type ConditionEntityDomain = (typeof CONDITION_ENTITY_DOMAINS)[number];
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

// Strategy defaults (new option names)
export const DEFAULT_CODE_DISPLAY = 'masked_with_reveal';
export const DEFAULT_SHOW_CODE_SENSORS = true;
export const DEFAULT_SHOW_CONDITIONS = true;
export const DEFAULT_SHOW_LOCK_COUNT = true;
export const DEFAULT_SHOW_PER_CONFIGURATION_LOCK_CARDS = true;
export const DEFAULT_SHOW_LOCK_CARDS = true;
export const DEFAULT_SHOW_LOCK_STATUS = true;
export const DEFAULT_SHOW_LOCK_SYNC = true;
export const DEFAULT_USE_SLOT_CARDS = true;
export const DEFAULT_SHOW_ALL_LOCK_CARDS_VIEW = true;

// Legacy defaults (deprecated, kept for backwards compatibility)
/** @deprecated Use DEFAULT_SHOW_ALL_LOCK_CARDS_VIEW */
export const DEFAULT_SHOW_ALL_CODES_FOR_LOCKS = DEFAULT_SHOW_ALL_LOCK_CARDS_VIEW;
/** @deprecated Use DEFAULT_CODE_DISPLAY */
export const DEFAULT_CODE_DATA_VIEW_CODE_DISPLAY = DEFAULT_CODE_DISPLAY;
/** @deprecated Use DEFAULT_SHOW_CODE_SENSORS */
export const DEFAULT_INCLUDE_CODE_SLOT_SENSORS = DEFAULT_SHOW_CODE_SENSORS;
/** @deprecated Use DEFAULT_SHOW_LOCK_SYNC */
export const DEFAULT_INCLUDE_IN_SYNC_SENSORS = DEFAULT_SHOW_LOCK_SYNC;

export const FOLD_ENTITY_ROW_SEARCH_STRING = 'fold-entity-row.js';
export const FOLD_ENTITY_ROW_REPO_NAME = 'thomasloven/lovelace-fold-entity-row';
