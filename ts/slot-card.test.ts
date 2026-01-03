import { describe, expect, it } from 'vitest';

import { CodeDisplayMode, LockCodeManagerSlotCardConfig, SlotCardData } from './types';

// Test the logic that can be unit tested without full component instantiation

const DEFAULT_CODE_DISPLAY: CodeDisplayMode = 'masked_with_reveal';

describe('LockCodeManagerSlotCard logic', () => {
    describe('config validation', () => {
        interface ValidationResult {
            error?: string;
            valid: boolean;
        }

        function validateConfig(config: Partial<LockCodeManagerSlotCardConfig>): ValidationResult {
            if (!config.config_entry_id && !config.config_entry_title) {
                return { valid: false, error: 'config_entry_id or config_entry_title is required' };
            }
            if (typeof config.slot !== 'number' || config.slot < 1) {
                return { valid: false, error: 'slot must be a positive number' };
            }
            return { valid: true };
        }

        it('requires config_entry_id or config_entry_title', () => {
            expect(validateConfig({ slot: 1 })).toEqual({
                valid: false,
                error: 'config_entry_id or config_entry_title is required'
            });
        });

        it('rejects empty config_entry_id without config_entry_title', () => {
            expect(validateConfig({ config_entry_id: '', slot: 1 })).toEqual({
                valid: false,
                error: 'config_entry_id or config_entry_title is required'
            });
        });

        it('accepts config_entry_title instead of config_entry_id', () => {
            expect(
                validateConfig({
                    config_entry_title: 'My Lock Manager',
                    slot: 1,
                    type: 'custom:lock-code-manager-slot'
                })
            ).toEqual({ valid: true });
        });

        it('requires slot to be a number', () => {
            expect(validateConfig({ config_entry_id: 'abc123' })).toEqual({
                valid: false,
                error: 'slot must be a positive number'
            });
        });

        it('rejects slot of 0', () => {
            expect(validateConfig({ config_entry_id: 'abc123', slot: 0 })).toEqual({
                valid: false,
                error: 'slot must be a positive number'
            });
        });

        it('rejects negative slot', () => {
            expect(validateConfig({ config_entry_id: 'abc123', slot: -1 })).toEqual({
                valid: false,
                error: 'slot must be a positive number'
            });
        });

        it('accepts valid config', () => {
            expect(
                validateConfig({
                    config_entry_id: 'abc123',
                    slot: 1,
                    type: 'custom:lock-code-manager-slot'
                })
            ).toEqual({ valid: true });
        });

        it('accepts config with optional fields', () => {
            expect(
                validateConfig({
                    code_display: 'unmasked',
                    collapsed_sections: ['conditions'],
                    config_entry_id: 'abc123',
                    show_code_sensors: true,
                    slot: 5,
                    type: 'custom:lock-code-manager-slot'
                })
            ).toEqual({ valid: true });
        });
    });

    describe('shouldReveal logic', () => {
        function shouldReveal(mode: CodeDisplayMode | undefined, revealed: boolean): boolean {
            const effectiveMode = mode ?? DEFAULT_CODE_DISPLAY;
            return (
                effectiveMode === 'unmasked' || (effectiveMode === 'masked_with_reveal' && revealed)
            );
        }

        it('returns false for masked mode regardless of toggle', () => {
            expect(shouldReveal('masked', false)).toBe(false);
            expect(shouldReveal('masked', true)).toBe(false);
        });

        it('returns true for unmasked mode regardless of toggle', () => {
            expect(shouldReveal('unmasked', false)).toBe(true);
            expect(shouldReveal('unmasked', true)).toBe(true);
        });

        it('returns toggle state for masked_with_reveal mode', () => {
            expect(shouldReveal('masked_with_reveal', false)).toBe(false);
            expect(shouldReveal('masked_with_reveal', true)).toBe(true);
        });

        it('defaults to masked_with_reveal when mode is undefined', () => {
            // Default is masked_with_reveal, so it follows revealed state
            expect(shouldReveal(undefined, false)).toBe(false);
            expect(shouldReveal(undefined, true)).toBe(true);
        });
    });

    describe('PIN display logic', () => {
        interface PinDisplayResult {
            displayValue: string;
            hasPin: boolean;
            shouldShowMasked: boolean;
        }

        function getPinDisplay(
            pin: string | null,
            pinLength: number | undefined,
            shouldMask: boolean
        ): PinDisplayResult {
            const hasPin = pin !== null || pinLength !== undefined;
            let displayValue: string;

            if (pin) {
                displayValue = shouldMask ? '•'.repeat(pin.length) : pin;
            } else if (pinLength !== undefined) {
                displayValue = '•'.repeat(pinLength);
            } else {
                displayValue = '—';
            }

            return {
                displayValue,
                hasPin,
                shouldShowMasked: shouldMask && hasPin
            };
        }

        it('shows actual PIN when not masked', () => {
            const result = getPinDisplay('1234', undefined, false);
            expect(result).toEqual({
                displayValue: '1234',
                hasPin: true,
                shouldShowMasked: false
            });
        });

        it('shows masked dots when PIN exists and should mask', () => {
            const result = getPinDisplay('1234', undefined, true);
            expect(result).toEqual({
                displayValue: '••••',
                hasPin: true,
                shouldShowMasked: true
            });
        });

        it('shows masked dots of correct length', () => {
            const result = getPinDisplay('123456', undefined, true);
            expect(result).toEqual({
                displayValue: '••••••',
                hasPin: true,
                shouldShowMasked: true
            });
        });

        it('shows masked dots from pinLength when no actual PIN', () => {
            const result = getPinDisplay(null, 4, true);
            expect(result).toEqual({
                displayValue: '••••',
                hasPin: true,
                shouldShowMasked: true
            });
        });

        it('shows masked dots from pinLength when not masked (reveal still shows length)', () => {
            // When we have pinLength but no PIN, it means the PIN is masked on the server
            const result = getPinDisplay(null, 6, false);
            expect(result).toEqual({
                displayValue: '••••••',
                hasPin: true,
                shouldShowMasked: false
            });
        });

        it('shows dash when no PIN and no pinLength', () => {
            const result = getPinDisplay(null, undefined, false);
            expect(result).toEqual({
                displayValue: '—',
                hasPin: false,
                shouldShowMasked: false
            });
        });

        it('shows dash when no PIN and no pinLength even when masking', () => {
            const result = getPinDisplay(null, undefined, true);
            expect(result).toEqual({
                displayValue: '—',
                hasPin: false,
                shouldShowMasked: false
            });
        });
    });

    describe('status determination', () => {
        interface StatusResult {
            statusClass: string;
            statusDetail: string;
            statusText: string;
        }

        function getStatus(enabled: boolean | null, active: boolean | null): StatusResult {
            if (enabled === false) {
                return {
                    statusClass: 'disabled',
                    statusText: 'Disabled',
                    statusDetail: 'Slot is disabled by user'
                };
            } else if (active === true) {
                return {
                    statusClass: 'active',
                    statusText: 'Active',
                    statusDetail: ''
                };
            } else if (active === false) {
                return {
                    statusClass: 'inactive',
                    statusText: 'Inactive',
                    statusDetail: 'Blocked by conditions'
                };
            }
            return {
                statusClass: 'disabled',
                statusText: 'Unknown',
                statusDetail: 'Status unavailable'
            };
        }

        it('returns disabled when enabled is false', () => {
            expect(getStatus(false, true)).toEqual({
                statusClass: 'disabled',
                statusText: 'Disabled',
                statusDetail: 'Slot is disabled by user'
            });
        });

        it('disabled takes precedence over active', () => {
            expect(getStatus(false, true)).toEqual({
                statusClass: 'disabled',
                statusText: 'Disabled',
                statusDetail: 'Slot is disabled by user'
            });
        });

        it('returns active when enabled and active', () => {
            expect(getStatus(true, true)).toEqual({
                statusClass: 'active',
                statusText: 'Active',
                statusDetail: ''
            });
        });

        it('returns inactive when enabled but not active', () => {
            expect(getStatus(true, false)).toEqual({
                statusClass: 'inactive',
                statusText: 'Inactive',
                statusDetail: 'Blocked by conditions'
            });
        });

        it('returns active when enabled is null but active is true', () => {
            // When enabled is unknown (null) but active is true, show active
            expect(getStatus(null, true)).toEqual({
                statusClass: 'active',
                statusText: 'Active',
                statusDetail: ''
            });
        });

        it('returns inactive when enabled is null but active is false', () => {
            // When enabled is unknown (null) but active is false, show inactive
            expect(getStatus(null, false)).toEqual({
                statusClass: 'inactive',
                statusText: 'Inactive',
                statusDetail: 'Blocked by conditions'
            });
        });

        it('returns unknown when active is null and enabled is true', () => {
            expect(getStatus(true, null)).toEqual({
                statusClass: 'disabled',
                statusText: 'Unknown',
                statusDetail: 'Status unavailable'
            });
        });

        it('returns unknown when both are null', () => {
            expect(getStatus(null, null)).toEqual({
                statusClass: 'disabled',
                statusText: 'Unknown',
                statusDetail: 'Status unavailable'
            });
        });
    });

    describe('lock sync status', () => {
        interface LockSyncResult {
            iconClass: string;
            statusText: string;
        }

        function getLockSyncStatus(inSync: boolean | null): LockSyncResult {
            if (inSync === true) {
                return { iconClass: 'synced', statusText: 'Synced' };
            } else if (inSync === false) {
                return { iconClass: 'pending', statusText: 'Pending' };
            }
            return { iconClass: 'unknown', statusText: 'Unknown' };
        }

        it('returns synced for inSync true', () => {
            expect(getLockSyncStatus(true)).toEqual({
                iconClass: 'synced',
                statusText: 'Synced'
            });
        });

        it('returns pending for inSync false', () => {
            expect(getLockSyncStatus(false)).toEqual({
                iconClass: 'pending',
                statusText: 'Pending'
            });
        });

        it('returns unknown for inSync null', () => {
            expect(getLockSyncStatus(null)).toEqual({
                iconClass: 'unknown',
                statusText: 'Unknown'
            });
        });
    });

    describe('lock sync counting', () => {
        interface LockStatus {
            inSync: boolean | null;
            name: string;
        }

        function countSyncedLocks(locks: LockStatus[]): { synced: number; total: number } {
            return {
                synced: locks.filter((l) => l.inSync === true).length,
                total: locks.length
            };
        }

        it('counts all synced when all are synced', () => {
            const locks = [
                { name: 'Lock 1', inSync: true },
                { name: 'Lock 2', inSync: true }
            ];
            expect(countSyncedLocks(locks)).toEqual({ synced: 2, total: 2 });
        });

        it('counts none synced when all are pending', () => {
            const locks = [
                { name: 'Lock 1', inSync: false },
                { name: 'Lock 2', inSync: false }
            ];
            expect(countSyncedLocks(locks)).toEqual({ synced: 0, total: 2 });
        });

        it('counts correctly with mixed states', () => {
            const locks = [
                { name: 'Lock 1', inSync: true },
                { name: 'Lock 2', inSync: false },
                { name: 'Lock 3', inSync: null },
                { name: 'Lock 4', inSync: true }
            ];
            expect(countSyncedLocks(locks)).toEqual({ synced: 2, total: 4 });
        });

        it('handles empty array', () => {
            expect(countSyncedLocks([])).toEqual({ synced: 0, total: 0 });
        });

        it('does not count null as synced', () => {
            const locks = [
                { name: 'Lock 1', inSync: null },
                { name: 'Lock 2', inSync: null }
            ];
            expect(countSyncedLocks(locks)).toEqual({ synced: 0, total: 2 });
        });
    });

    describe('entity pattern matching', () => {
        function findEntityBySlotKey(
            states: Record<string, unknown>,
            domain: string,
            slot: number,
            key: string
        ): string | undefined {
            const pattern = new RegExp(`^${domain}\\.(.+)_code_slot_(\\d+)_${key}$`);

            return Object.keys(states).find((entityId) => {
                const match = entityId.match(pattern);
                return match && parseInt(match[2], 10) === slot;
            });
        }

        it('finds enabled switch entity', () => {
            const states = {
                'switch.my_entry_code_slot_1_enabled': {},
                'switch.other_code_slot_2_enabled': {}
            };
            expect(findEntityBySlotKey(states, 'switch', 1, 'enabled')).toBe(
                'switch.my_entry_code_slot_1_enabled'
            );
        });

        it('finds name text entity', () => {
            const states = {
                'text.front_door_code_slot_5_name': {},
                'text.front_door_code_slot_5_pin': {}
            };
            expect(findEntityBySlotKey(states, 'text', 5, 'name')).toBe(
                'text.front_door_code_slot_5_name'
            );
        });

        it('finds pin text entity', () => {
            const states = {
                'text.front_door_code_slot_3_name': {},
                'text.front_door_code_slot_3_pin': {}
            };
            expect(findEntityBySlotKey(states, 'text', 3, 'pin')).toBe(
                'text.front_door_code_slot_3_pin'
            );
        });

        it('returns undefined when no match', () => {
            const states = {
                'switch.my_entry_code_slot_1_enabled': {}
            };
            expect(findEntityBySlotKey(states, 'switch', 2, 'enabled')).toBeUndefined();
        });

        it('matches correct slot number only', () => {
            const states = {
                'switch.entry_code_slot_1_enabled': {},
                'switch.entry_code_slot_11_enabled': {},
                'switch.entry_code_slot_111_enabled': {}
            };
            expect(findEntityBySlotKey(states, 'switch', 1, 'enabled')).toBe(
                'switch.entry_code_slot_1_enabled'
            );
            expect(findEntityBySlotKey(states, 'switch', 11, 'enabled')).toBe(
                'switch.entry_code_slot_11_enabled'
            );
        });

        it('handles entities with underscores in title', () => {
            const states = {
                'text.my_lock_code_manager_code_slot_1_name': {}
            };
            expect(findEntityBySlotKey(states, 'text', 1, 'name')).toBe(
                'text.my_lock_code_manager_code_slot_1_name'
            );
        });

        it('matches domain correctly', () => {
            const states = {
                'switch.entry_code_slot_1_enabled': {},
                'binary_sensor.entry_code_slot_1_active': {}
            };
            expect(findEntityBySlotKey(states, 'switch', 1, 'enabled')).toBe(
                'switch.entry_code_slot_1_enabled'
            );
            expect(findEntityBySlotKey(states, 'binary_sensor', 1, 'active')).toBe(
                'binary_sensor.entry_code_slot_1_active'
            );
        });
    });

    describe('collapsed sections logic', () => {
        type CollapsibleSection = 'conditions' | 'lock_status';

        function getInitialExpandedState(collapsedSections: CollapsibleSection[] | undefined): {
            conditionsExpanded: boolean;
            lockStatusExpanded: boolean;
        } {
            const collapsed = collapsedSections ?? ['conditions', 'lock_status'];
            return {
                conditionsExpanded: !collapsed.includes('conditions'),
                lockStatusExpanded: !collapsed.includes('lock_status')
            };
        }

        it('defaults to both collapsed', () => {
            expect(getInitialExpandedState(undefined)).toEqual({
                conditionsExpanded: false,
                lockStatusExpanded: false
            });
        });

        it('expands conditions when not in collapsed list', () => {
            expect(getInitialExpandedState(['lock_status'])).toEqual({
                conditionsExpanded: true,
                lockStatusExpanded: false
            });
        });

        it('expands lock_status when not in collapsed list', () => {
            expect(getInitialExpandedState(['conditions'])).toEqual({
                conditionsExpanded: false,
                lockStatusExpanded: true
            });
        });

        it('expands both when empty array', () => {
            expect(getInitialExpandedState([])).toEqual({
                conditionsExpanded: true,
                lockStatusExpanded: true
            });
        });

        it('collapses both when both in list', () => {
            expect(getInitialExpandedState(['conditions', 'lock_status'])).toEqual({
                conditionsExpanded: false,
                lockStatusExpanded: false
            });
        });
    });

    describe('condition counting', () => {
        function countConditions(conditions: { number_of_uses?: number }): number {
            let count = 0;
            if (conditions.number_of_uses !== undefined && conditions.number_of_uses !== null) {
                count += 1;
            }
            return count;
        }

        it('returns 0 when no conditions', () => {
            expect(countConditions({})).toBe(0);
        });

        it('returns 1 when number_of_uses is set', () => {
            expect(countConditions({ number_of_uses: 5 })).toBe(1);
        });

        it('returns 1 when number_of_uses is 0', () => {
            expect(countConditions({ number_of_uses: 0 })).toBe(1);
        });

        it('returns 0 when number_of_uses is undefined', () => {
            expect(countConditions({ number_of_uses: undefined })).toBe(0);
        });
    });

    describe('SlotCardData transformation', () => {
        function transformLocks(
            locks: SlotCardData['locks']
        ): Array<{ entityId: string; inSync: boolean; lockEntityId: string; name: string }> {
            return locks.map((lock) => {
                return {
                    entityId: lock.entity_id,
                    inSync: lock.in_sync,
                    lockEntityId: lock.entity_id,
                    name: lock.name
                };
            });
        }

        it('transforms lock data correctly', () => {
            const locks: SlotCardData['locks'] = [
                { entity_id: 'lock.front_door', name: 'Front Door', in_sync: true, code: '1234' },
                { entity_id: 'lock.back_door', name: 'Back Door', in_sync: false, code: null }
            ];

            expect(transformLocks(locks)).toEqual([
                {
                    entityId: 'lock.front_door',
                    inSync: true,
                    lockEntityId: 'lock.front_door',
                    name: 'Front Door'
                },
                {
                    entityId: 'lock.back_door',
                    inSync: false,
                    lockEntityId: 'lock.back_door',
                    name: 'Back Door'
                }
            ]);
        });

        it('handles empty lock array', () => {
            expect(transformLocks([])).toEqual([]);
        });
    });

    describe('error message handling', () => {
        function formatError(err: unknown): string {
            if (err instanceof Error) {
                return err.message;
            } else if (typeof err === 'object' && err !== null && 'message' in err) {
                return String((err as { message: unknown }).message);
            }
            return `Failed to subscribe: ${JSON.stringify(err)}`;
        }

        it('extracts message from Error instance', () => {
            const error = new Error('Connection failed');
            expect(formatError(error)).toBe('Connection failed');
        });

        it('extracts message from object with message property', () => {
            const error = { message: 'API error', code: 500 };
            expect(formatError(error)).toBe('API error');
        });

        it('stringifies unknown error types', () => {
            expect(formatError({ code: 'UNKNOWN' })).toBe(
                'Failed to subscribe: {"code":"UNKNOWN"}'
            );
        });

        it('handles string errors', () => {
            expect(formatError('simple error')).toBe('Failed to subscribe: "simple error"');
        });

        it('handles null', () => {
            expect(formatError(null)).toBe('Failed to subscribe: null');
        });

        it('handles undefined', () => {
            expect(formatError(undefined)).toBe('Failed to subscribe: undefined');
        });
    });

    describe('stub config', () => {
        function getStubConfig(): Partial<LockCodeManagerSlotCardConfig> {
            return { config_entry_id: '', slot: 1 };
        }

        it('provides default stub config', () => {
            expect(getStubConfig()).toEqual({
                config_entry_id: '',
                slot: 1
            });
        });
    });
});
