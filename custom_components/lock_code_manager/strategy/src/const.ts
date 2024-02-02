export const CODE_SENSOR_KEY = 'code';
export const PIN_SHOULD_BE_ENABLED_KEY = 'pin_should_be_enabled';
export const CONDITION_KEYS = ['number_of_uses'];
export const KEY_ORDER = [
  'name',
  'enabled',
  'pin',
  PIN_SHOULD_BE_ENABLED_KEY,
  ...CONDITION_KEYS,
  CODE_SENSOR_KEY
];
export const DOMAIN = 'lock_code_manager';
