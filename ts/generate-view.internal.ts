/**
 * Internal exports for testing purposes only.
 *
 * DO NOT import from this module in production code.
 * These functions are implementation details and may change without notice.
 */

export { CODE_EVENT_KEY, CODE_SENSOR_KEY, IN_SYNC_KEY, KEY_ORDER } from './const';
export {
    compareAndSortEntities,
    createLockCodeManagerEntity,
    getEntityDisplayName
} from './generate-view';
