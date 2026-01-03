import { describe, expect, it } from 'vitest';

import { CodeDisplayMode, LockCoordinatorSlotData } from './types';

// Test the logic that can be unit tested without full component instantiation

describe('LockCodeManagerLockDataCard logic', () => {
    describe('shouldReveal logic', () => {
        // Default changed from 'unmasked' to 'masked_with_reveal' for better security UX
        const DEFAULT_CODE_DISPLAY: CodeDisplayMode = 'masked_with_reveal';

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
            // With new default, codes are hidden until user clicks reveal
            expect(shouldReveal(undefined, false)).toBe(false);
            expect(shouldReveal(undefined, true)).toBe(true);
        });
    });

    describe('sync status logic', () => {
        type SyncState = 'synced' | 'not_synced' | 'unknown';

        function getSyncState(slot: LockCoordinatorSlotData): SyncState {
            if (slot.in_sync === true) return 'synced';
            if (slot.in_sync === false) return 'not_synced';
            return 'unknown';
        }

        it('returns synced when in_sync is true', () => {
            const slot: LockCoordinatorSlotData = { slot: 1, code: '1234', in_sync: true };
            expect(getSyncState(slot)).toBe('synced');
        });

        it('returns not_synced when in_sync is false', () => {
            const slot: LockCoordinatorSlotData = { slot: 1, code: '1234', in_sync: false };
            expect(getSyncState(slot)).toBe('not_synced');
        });

        it('returns unknown when in_sync is undefined', () => {
            const slot: LockCoordinatorSlotData = { slot: 1, code: '1234' };
            expect(getSyncState(slot)).toBe('unknown');
        });
    });

    describe('slot status classification', () => {
        type SlotStatus = 'active' | 'inactive' | 'disabled' | 'empty';

        function getSlotStatus(slot: LockCoordinatorSlotData): SlotStatus {
            const hasCode = slot.code !== null || (slot.code_length ?? 0) > 0;
            const hasConfiguredCode =
                slot.configured_code !== undefined || (slot.configured_code_length ?? 0) > 0;

            if (!hasCode && !hasConfiguredCode) return 'empty';
            if (slot.enabled === false) return 'disabled';
            if (hasCode) return 'active';
            // has configured code but not on lock yet
            return 'inactive';
        }

        it('returns active when slot has code on lock', () => {
            const slot: LockCoordinatorSlotData = { slot: 1, code: '1234', enabled: true };
            expect(getSlotStatus(slot)).toBe('active');
        });

        it('returns active when slot has masked code', () => {
            const slot: LockCoordinatorSlotData = {
                slot: 1,
                code: null,
                code_length: 4,
                enabled: true
            };
            expect(getSlotStatus(slot)).toBe('active');
        });

        it('returns disabled when enabled is false', () => {
            const slot: LockCoordinatorSlotData = { slot: 1, code: '1234', enabled: false };
            expect(getSlotStatus(slot)).toBe('disabled');
        });

        it('returns inactive when has configured code but not on lock', () => {
            const slot: LockCoordinatorSlotData = {
                slot: 1,
                code: null,
                configured_code: '1234',
                enabled: true
            };
            expect(getSlotStatus(slot)).toBe('inactive');
        });

        it('returns empty when no code configured or on lock', () => {
            const slot: LockCoordinatorSlotData = { slot: 1, code: null };
            expect(getSlotStatus(slot)).toBe('empty');
        });
    });

    describe('renderCode logic', () => {
        function getCodeDisplay(slot: LockCoordinatorSlotData): {
            type: 'code' | 'masked' | 'empty';
            value: string;
        } {
            if (slot.code !== null) {
                return { type: 'code', value: String(slot.code) };
            }
            if (slot.code_length) {
                return { type: 'masked', value: '•'.repeat(slot.code_length) };
            }
            return { type: 'empty', value: '' };
        }

        it('returns code when present', () => {
            const slot: LockCoordinatorSlotData = { slot: 1, code: '1234' };
            expect(getCodeDisplay(slot)).toEqual({ type: 'code', value: '1234' });
        });

        it('returns numeric code as string', () => {
            const slot: LockCoordinatorSlotData = { slot: 1, code: 5678 };
            expect(getCodeDisplay(slot)).toEqual({ type: 'code', value: '5678' });
        });

        it('returns masked bullets when code is null but code_length exists', () => {
            const slot: LockCoordinatorSlotData = { slot: 1, code: null, code_length: 4 };
            expect(getCodeDisplay(slot)).toEqual({ type: 'masked', value: '••••' });
        });

        it('returns masked bullets of correct length', () => {
            const slot: LockCoordinatorSlotData = { slot: 1, code: null, code_length: 6 };
            expect(getCodeDisplay(slot)).toEqual({ type: 'masked', value: '••••••' });
        });

        it('returns empty when code is null and no code_length', () => {
            const slot: LockCoordinatorSlotData = { slot: 1, code: null };
            expect(getCodeDisplay(slot)).toEqual({ type: 'empty', value: '' });
        });
    });

    describe('slot grouping logic', () => {
        interface SlotGroup {
            rangeLabel?: string;
            slots: LockCoordinatorSlotData[];
            type: 'active' | 'empty';
        }

        function groupSlots(slots: LockCoordinatorSlotData[]): SlotGroup[] {
            const groups: SlotGroup[] = [];
            let currentEmpty: LockCoordinatorSlotData[] = [];

            const flushEmpty = (): void => {
                if (currentEmpty.length > 0) {
                    groups.push({
                        type: 'empty',
                        slots: currentEmpty,
                        rangeLabel: formatSlotRange(currentEmpty)
                    });
                    currentEmpty = [];
                }
            };

            for (const slot of slots) {
                const hasCode = slot.code !== null || slot.code_length;
                if (hasCode) {
                    flushEmpty();
                    groups.push({ type: 'active', slots: [slot] });
                } else {
                    currentEmpty.push(slot);
                }
            }
            flushEmpty();

            return groups;
        }

        function formatSlotRange(slots: LockCoordinatorSlotData[]): string {
            if (slots.length === 0) return '';
            if (slots.length === 1) return `Slot ${slots[0].slot}`;

            const nums = slots.map((s) => Number(s.slot)).filter((n) => !isNaN(n));
            if (nums.length !== slots.length) {
                return `Slots ${slots.map((s) => s.slot).join(', ')}`;
            }

            const ranges: string[] = [];
            const [startValue] = nums;
            let start = startValue;
            let end = startValue;

            for (let i = 1; i < nums.length; i++) {
                if (nums[i] === end + 1) {
                    end = nums[i];
                } else {
                    ranges.push(start === end ? `${start}` : `${start}-${end}`);
                    start = nums[i];
                    end = nums[i];
                }
            }
            ranges.push(start === end ? `${start}` : `${start}-${end}`);

            return `Slots ${ranges.join(', ')}`;
        }

        it('groups active slots individually', () => {
            const slots: LockCoordinatorSlotData[] = [
                { slot: 1, code: '1234' },
                { slot: 2, code: '5678' }
            ];
            const groups = groupSlots(slots);
            expect(groups).toHaveLength(2);
            expect(groups[0].type).toBe('active');
            expect(groups[1].type).toBe('active');
        });

        it('groups consecutive empty slots together', () => {
            const slots: LockCoordinatorSlotData[] = [
                { slot: 1, code: null },
                { slot: 2, code: null },
                { slot: 3, code: null }
            ];
            const groups = groupSlots(slots);
            expect(groups).toHaveLength(1);
            expect(groups[0].type).toBe('empty');
            expect(groups[0].rangeLabel).toBe('Slots 1-3');
        });

        it('interleaves active and empty groups correctly', () => {
            const slots: LockCoordinatorSlotData[] = [
                { slot: 1, code: '1234' },
                { slot: 2, code: null },
                { slot: 3, code: null },
                { slot: 4, code: '5678' },
                { slot: 5, code: null }
            ];
            const groups = groupSlots(slots);
            expect(groups).toHaveLength(4);
            expect(groups[0].type).toBe('active');
            expect(groups[1].type).toBe('empty');
            expect(groups[1].rangeLabel).toBe('Slots 2-3');
            expect(groups[2].type).toBe('active');
            expect(groups[3].type).toBe('empty');
            expect(groups[3].rangeLabel).toBe('Slot 5');
        });

        it('handles non-consecutive empty slots', () => {
            const slots: LockCoordinatorSlotData[] = [
                { slot: 1, code: null },
                { slot: 3, code: null },
                { slot: 5, code: null }
            ];
            const groups = groupSlots(slots);
            expect(groups).toHaveLength(1);
            expect(groups[0].rangeLabel).toBe('Slots 1, 3, 5');
        });

        it('treats masked codes as active', () => {
            const slots: LockCoordinatorSlotData[] = [{ slot: 1, code: null, code_length: 4 }];
            const groups = groupSlots(slots);
            expect(groups).toHaveLength(1);
            expect(groups[0].type).toBe('active');
        });
    });

    describe('config validation', () => {
        function validateConfig(config: { lock_entity_id?: string }): {
            error?: string;
            valid: boolean;
        } {
            if (!config.lock_entity_id) {
                return { valid: false, error: 'lock_entity_id is required' };
            }
            return { valid: true };
        }

        it('requires lock_entity_id', () => {
            expect(validateConfig({})).toEqual({
                valid: false,
                error: 'lock_entity_id is required'
            });
        });

        it('rejects empty lock_entity_id', () => {
            expect(validateConfig({ lock_entity_id: '' })).toEqual({
                valid: false,
                error: 'lock_entity_id is required'
            });
        });

        it('accepts valid lock_entity_id', () => {
            expect(validateConfig({ lock_entity_id: 'lock.front_door' })).toEqual({ valid: true });
        });
    });

    describe('title resolution', () => {
        function resolveTitle(options: {
            configTitle?: string;
            dataLockName?: string;
            hassStateName?: string;
        }): string {
            const DEFAULT_TITLE = 'Lock Codes';
            return (
                options.configTitle ??
                options.dataLockName ??
                options.hassStateName ??
                DEFAULT_TITLE
            );
        }

        it('uses config title first', () => {
            expect(
                resolveTitle({
                    configTitle: 'Custom Title',
                    dataLockName: 'Front Door',
                    hassStateName: 'Lock 1'
                })
            ).toBe('Custom Title');
        });

        it('falls back to data lock_name', () => {
            expect(
                resolveTitle({
                    dataLockName: 'Front Door',
                    hassStateName: 'Lock 1'
                })
            ).toBe('Front Door');
        });

        it('falls back to hass state name', () => {
            expect(
                resolveTitle({
                    hassStateName: 'Lock 1'
                })
            ).toBe('Lock 1');
        });

        it('falls back to default title', () => {
            expect(resolveTitle({})).toBe('Lock Codes');
        });
    });
});
