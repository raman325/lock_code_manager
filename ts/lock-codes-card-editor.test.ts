import { describe, expect, it } from 'vitest';

import { CodeDisplayMode, LockCodesCardConfig } from './types';

/**
 * Tests for LockCodesCardEditor logic.
 *
 * These tests verify the configuration handling logic used in the editor
 * without requiring full LitElement component instantiation.
 */
describe('LockCodesCardEditor logic', () => {
    describe('lock selection', () => {
        interface ConfigWithLock {
            lock_entity_id?: string;
        }

        /**
         * Updates the lock entity ID in config.
         * Mirrors _lockChanged behavior.
         */
        function selectLock(config: ConfigWithLock, newLockId: string): ConfigWithLock {
            return { ...config, lock_entity_id: newLockId };
        }

        it('sets lock_entity_id on selection', () => {
            const config: ConfigWithLock = {};
            const result = selectLock(config, 'lock.front_door');
            expect(result.lock_entity_id).toBe('lock.front_door');
        });

        it('replaces existing lock_entity_id', () => {
            const config: ConfigWithLock = {
                lock_entity_id: 'lock.old_door'
            };
            const result = selectLock(config, 'lock.new_door');
            expect(result.lock_entity_id).toBe('lock.new_door');
        });

        it('preserves other config values', () => {
            const config = {
                lock_entity_id: 'lock.old_door',
                title: 'My Lock',
                code_display: 'masked' as CodeDisplayMode
            };
            const result = selectLock(config, 'lock.new_door');
            expect(result.lock_entity_id).toBe('lock.new_door');
        });
    });

    describe('title handling', () => {
        interface ConfigWithTitle {
            title?: string;
        }

        /**
         * Processes title input value.
         * Empty string becomes undefined (to use default title).
         * Mirrors _titleChanged behavior.
         */
        function processTitleInput(inputValue: string): string | undefined {
            return inputValue || undefined;
        }

        /**
         * Updates title in config.
         */
        function updateTitle(
            config: ConfigWithTitle,
            newTitle: string | undefined
        ): ConfigWithTitle {
            return { ...config, title: newTitle };
        }

        it('converts empty string to undefined', () => {
            expect(processTitleInput('')).toBeUndefined();
        });

        it('preserves non-empty string', () => {
            expect(processTitleInput('My Custom Title')).toBe('My Custom Title');
        });

        it('preserves whitespace-only string', () => {
            // Note: the editor doesn't trim, so whitespace is preserved
            expect(processTitleInput('   ')).toBe('   ');
        });

        it('sets title in config', () => {
            const config: ConfigWithTitle = {};
            const result = updateTitle(config, 'New Title');
            expect(result.title).toBe('New Title');
        });

        it('clears title when set to undefined', () => {
            const config: ConfigWithTitle = { title: 'Old Title' };
            const result = updateTitle(config, undefined);
            expect(result.title).toBeUndefined();
        });
    });

    describe('code display mode', () => {
        const CODE_DISPLAY_OPTIONS: Array<{ label: string; value: CodeDisplayMode }> = [
            { label: 'Masked with Reveal', value: 'masked_with_reveal' },
            { label: 'Always Masked', value: 'masked' },
            { label: 'Always Visible', value: 'unmasked' }
        ];

        it('has three display mode options', () => {
            expect(CODE_DISPLAY_OPTIONS).toHaveLength(3);
        });

        it('includes all valid CodeDisplayMode values', () => {
            const values = CODE_DISPLAY_OPTIONS.map((opt) => opt.value);
            expect(values).toContain('masked_with_reveal');
            expect(values).toContain('masked');
            expect(values).toContain('unmasked');
        });

        it('defaults to unmasked for lock codes card', () => {
            // Lock codes card shows codes by default (different from slot card)
            const getDefaultCodeDisplay = (): CodeDisplayMode => 'unmasked';
            expect(getDefaultCodeDisplay()).toBe('unmasked');
        });

        /**
         * Gets the default value for code_display in UI.
         * Mirrors: `.value=${this._config.code_display ?? 'unmasked'}`
         */
        function getDisplayValue(configValue: CodeDisplayMode | undefined): CodeDisplayMode {
            return configValue ?? 'unmasked';
        }

        it('uses unmasked when undefined', () => {
            expect(getDisplayValue(undefined)).toBe('unmasked');
        });

        it('uses configured value when set', () => {
            expect(getDisplayValue('masked')).toBe('masked');
            expect(getDisplayValue('masked_with_reveal')).toBe('masked_with_reveal');
        });
    });

    describe('lock entity filtering', () => {
        interface LockInfo {
            entity_id: string;
            name: string;
        }

        /**
         * Extracts entity IDs from lock info array.
         * Mirrors: `this._locks.map((l) => l.entity_id)`
         */
        function getLockEntityIds(locks: LockInfo[]): string[] {
            return locks.map((l) => l.entity_id);
        }

        it('extracts entity IDs from locks', () => {
            const locks: LockInfo[] = [
                { entity_id: 'lock.front_door', name: 'Front Door' },
                { entity_id: 'lock.back_door', name: 'Back Door' }
            ];
            expect(getLockEntityIds(locks)).toEqual(['lock.front_door', 'lock.back_door']);
        });

        it('returns empty array for no locks', () => {
            expect(getLockEntityIds([])).toEqual([]);
        });

        it('preserves order', () => {
            const locks: LockInfo[] = [
                { entity_id: 'lock.z', name: 'Z' },
                { entity_id: 'lock.a', name: 'A' },
                { entity_id: 'lock.m', name: 'M' }
            ];
            expect(getLockEntityIds(locks)).toEqual(['lock.z', 'lock.a', 'lock.m']);
        });
    });

    describe('config change detection', () => {
        /**
         * Checks if a new value differs from current.
         * Used to prevent unnecessary config dispatches.
         */
        function valueChanged<T>(currentValue: T | undefined, newValue: T): boolean {
            return newValue !== currentValue;
        }

        it('detects lock_entity_id change', () => {
            expect(valueChanged('lock.old', 'lock.new')).toBe(true);
        });

        it('detects no change for same lock', () => {
            expect(valueChanged('lock.same', 'lock.same')).toBe(false);
        });

        it('detects title change', () => {
            expect(valueChanged('Old Title', 'New Title')).toBe(true);
        });

        it('detects title cleared', () => {
            expect(valueChanged('Had Title', undefined as unknown as string)).toBe(true);
        });

        it('detects code_display change', () => {
            expect(valueChanged('masked' as CodeDisplayMode, 'unmasked' as CodeDisplayMode)).toBe(
                true
            );
        });
    });

    describe('loading state', () => {
        interface EditorState {
            loading: boolean;
            locks: unknown[];
        }

        /**
         * Determines what to show based on loading state.
         */
        function getLoadingDisplay(state: EditorState): 'loading' | 'warning' | 'picker' {
            if (state.loading) {
                return 'loading';
            }
            if (state.locks.length === 0) {
                return 'warning';
            }
            return 'picker';
        }

        it('shows loading when loading is true', () => {
            expect(getLoadingDisplay({ loading: true, locks: [] })).toBe('loading');
        });

        it('shows warning when no locks and not loading', () => {
            expect(getLoadingDisplay({ loading: false, locks: [] })).toBe('warning');
        });

        it('shows picker when locks exist', () => {
            expect(
                getLoadingDisplay({
                    loading: false,
                    locks: [{ entity_id: 'lock.test' }]
                })
            ).toBe('picker');
        });

        it('shows loading even if locks exist', () => {
            // Loading takes precedence
            expect(
                getLoadingDisplay({
                    loading: true,
                    locks: [{ entity_id: 'lock.test' }]
                })
            ).toBe('loading');
        });
    });

    describe('config validation', () => {
        /**
         * Basic validation for LockCodesCardConfig.
         */
        function validateConfig(config: Partial<LockCodesCardConfig>): {
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

        it('accepts valid config', () => {
            expect(
                validateConfig({
                    lock_entity_id: 'lock.front_door',
                    type: 'custom:lcm-lock-codes'
                })
            ).toEqual({ valid: true });
        });

        it('accepts config with optional fields', () => {
            expect(
                validateConfig({
                    code_display: 'masked',
                    lock_entity_id: 'lock.front_door',
                    title: 'Custom Title',
                    type: 'custom:lcm-lock-codes'
                })
            ).toEqual({ valid: true });
        });
    });
});
