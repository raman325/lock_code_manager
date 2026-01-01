import { describe, expect, it } from 'vitest';

import { CodeDisplayMode, LockCoordinatorSlotData } from './types';

// Test the logic that can be unit tested without full component instantiation

describe('LockCodeManagerLockDataCard logic', () => {
    describe('shouldReveal logic', () => {
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
            expect(shouldReveal(undefined, false)).toBe(false);
            expect(shouldReveal(undefined, true)).toBe(true);
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
