export const CODE_SENSOR_KEY = "code";
export const CODE_EVENT_KEY = "pin_used";
export const PIN_SYNCED_TO_LOCKS_KEY = "pin_synced_to_locks";
export const CONDITION_KEYS = ["number_of_uses"];
export const KEY_ORDER = [
    "name",
    "enabled",
    "pin",
    PIN_SYNCED_TO_LOCKS_KEY,
    ...CONDITION_KEYS,
    CODE_SENSOR_KEY,
    CODE_EVENT_KEY,
];
export const DOMAIN = "lock_code_manager";
export const DEFAULT_INCLUDE_CODE_SLOT_SENSORS = false;

export const FOLD_ENTITY_ROW_SEARCH_STRING = "fold-entity-row.js";
export const FOLD_ENTITY_ROW_REPO_NAME = "thomasloven/lovelace-fold-entity-row";
