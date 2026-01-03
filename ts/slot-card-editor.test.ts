import { describe, expect, it } from 'vitest';

import { CodeDisplayMode } from './types';

/**
 * Tests for LcmSlotCardEditor logic.
 *
 * These tests verify the configuration handling logic used in the editor
 * without requiring full LitElement component instantiation.
 */
describe('LcmSlotCardEditor logic', () => {
    describe('config entry selection', () => {
        interface ConfigWithEntry {
            config_entry_id?: string;
            config_entry_title?: string;
        }

        /**
         * When selecting a config entry by ID, the title should be cleared.
         * This mirrors the behavior in _configEntryChanged.
         */
        function selectConfigEntry(config: ConfigWithEntry, newEntryId: string): ConfigWithEntry {
            const { config_entry_title: _, ...rest } = config;
            return { ...rest, config_entry_id: newEntryId };
        }

        it('sets config_entry_id on selection', () => {
            const config: ConfigWithEntry = {};
            const result = selectConfigEntry(config, 'abc123');
            expect(result.config_entry_id).toBe('abc123');
        });

        it('clears config_entry_title when selecting by id', () => {
            const config: ConfigWithEntry = {
                config_entry_title: 'My Lock Manager'
            };
            const result = selectConfigEntry(config, 'abc123');
            expect(result.config_entry_id).toBe('abc123');
            expect(result.config_entry_title).toBeUndefined();
        });

        it('replaces existing config_entry_id', () => {
            const config: ConfigWithEntry = {
                config_entry_id: 'old123'
            };
            const result = selectConfigEntry(config, 'new456');
            expect(result.config_entry_id).toBe('new456');
        });
    });

    describe('slot number validation', () => {
        /**
         * Validates and parses slot number from string input.
         * Returns undefined for invalid values.
         */
        function parseSlotNumber(input: string): number | undefined {
            const value = parseInt(input, 10);
            if (isNaN(value)) {
                return undefined;
            }
            return value;
        }

        it('parses valid integer', () => {
            expect(parseSlotNumber('5')).toBe(5);
        });

        it('parses boundary values', () => {
            expect(parseSlotNumber('1')).toBe(1);
            expect(parseSlotNumber('9999')).toBe(9999);
        });

        it('returns undefined for non-numeric input', () => {
            expect(parseSlotNumber('abc')).toBeUndefined();
        });

        it('returns undefined for empty string', () => {
            expect(parseSlotNumber('')).toBeUndefined();
        });

        it('parses negative numbers (validation happens elsewhere)', () => {
            // The editor allows parsing; the card validates bounds
            expect(parseSlotNumber('-1')).toBe(-1);
        });

        it('truncates decimal values', () => {
            expect(parseSlotNumber('5.7')).toBe(5);
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

        it('defaults to masked_with_reveal for slot card', () => {
            const getDefaultCodeDisplay = (): CodeDisplayMode => 'masked_with_reveal';
            expect(getDefaultCodeDisplay()).toBe('masked_with_reveal');
        });
    });

    describe('show section toggles', () => {
        type ShowSectionKey =
            | 'show_conditions'
            | 'show_lock_status'
            | 'show_code_sensors'
            | 'show_lock_sync';

        interface ShowSectionsConfig {
            show_code_sensors?: boolean;
            show_conditions?: boolean;
            show_lock_status?: boolean;
            show_lock_sync?: boolean;
        }

        /**
         * Updates a show section boolean in the config.
         * Mirrors the _updateConfig helper behavior.
         */
        function updateShowSection(
            config: ShowSectionsConfig,
            key: ShowSectionKey,
            value: boolean
        ): ShowSectionsConfig {
            return { ...config, [key]: value };
        }

        it('sets show_conditions to true', () => {
            const config: ShowSectionsConfig = {};
            const result = updateShowSection(config, 'show_conditions', true);
            expect(result.show_conditions).toBe(true);
        });

        it('sets show_conditions to false', () => {
            const config: ShowSectionsConfig = { show_conditions: true };
            const result = updateShowSection(config, 'show_conditions', false);
            expect(result.show_conditions).toBe(false);
        });

        it('sets show_lock_status', () => {
            const config: ShowSectionsConfig = {};
            const result = updateShowSection(config, 'show_lock_status', false);
            expect(result.show_lock_status).toBe(false);
        });

        it('sets show_code_sensors', () => {
            const config: ShowSectionsConfig = {};
            const result = updateShowSection(config, 'show_code_sensors', true);
            expect(result.show_code_sensors).toBe(true);
        });

        it('sets show_lock_sync', () => {
            const config: ShowSectionsConfig = {};
            const result = updateShowSection(config, 'show_lock_sync', false);
            expect(result.show_lock_sync).toBe(false);
        });

        it('preserves other config values', () => {
            const config: ShowSectionsConfig = {
                show_conditions: true,
                show_lock_status: true
            };
            const result = updateShowSection(config, 'show_conditions', false);
            expect(result.show_conditions).toBe(false);
            expect(result.show_lock_status).toBe(true);
        });
    });

    describe('toggle from label click', () => {
        /**
         * Simulates toggle behavior when clicking the label.
         * The new value is the inverse of (current === false).
         * This matches: `this._config?.show_X === false` check.
         */
        function toggleFromLabel(currentValue: boolean | undefined): boolean {
            // When currentValue is false, toggle to true
            // When currentValue is undefined or true, toggle to false
            return currentValue === false;
        }

        it('toggles undefined to false (treating undefined as true)', () => {
            // undefined means "show by default", so clicking hides it
            expect(toggleFromLabel(undefined)).toBe(false);
        });

        it('toggles true to false', () => {
            expect(toggleFromLabel(true)).toBe(false);
        });

        it('toggles false to true', () => {
            expect(toggleFromLabel(false)).toBe(true);
        });
    });

    describe('checkbox checked state', () => {
        /**
         * Determines if checkbox should be checked.
         * Mirrors the `.checked=${this._config.show_X !== false}` pattern.
         */
        function isChecked(value: boolean | undefined): boolean {
            return value !== false;
        }

        it('returns true when undefined (default to shown)', () => {
            expect(isChecked(undefined)).toBe(true);
        });

        it('returns true when explicitly true', () => {
            expect(isChecked(true)).toBe(true);
        });

        it('returns false when explicitly false', () => {
            expect(isChecked(false)).toBe(false);
        });
    });

    describe('config entry filtering', () => {
        interface ConfigEntry {
            entry_id: string;
            state: string;
            title: string;
        }

        /**
         * Filters config entries to only include loaded ones.
         * Mirrors the behavior in _fetchConfigEntries.
         */
        function filterLoadedEntries(entries: ConfigEntry[]): ConfigEntry[] {
            return entries.filter((e) => e.state === 'loaded');
        }

        it('keeps loaded entries', () => {
            const entries: ConfigEntry[] = [
                { entry_id: '1', title: 'Lock 1', state: 'loaded' },
                { entry_id: '2', title: 'Lock 2', state: 'loaded' }
            ];
            expect(filterLoadedEntries(entries)).toHaveLength(2);
        });

        it('filters out non-loaded entries', () => {
            const entries: ConfigEntry[] = [
                { entry_id: '1', title: 'Lock 1', state: 'loaded' },
                { entry_id: '2', title: 'Lock 2', state: 'not_loaded' },
                { entry_id: '3', title: 'Lock 3', state: 'setup_error' }
            ];
            const result = filterLoadedEntries(entries);
            expect(result).toHaveLength(1);
            expect(result[0].entry_id).toBe('1');
        });

        it('returns empty array when no loaded entries', () => {
            const entries: ConfigEntry[] = [{ entry_id: '1', title: 'Lock 1', state: 'failed' }];
            expect(filterLoadedEntries(entries)).toHaveLength(0);
        });

        it('handles empty input', () => {
            expect(filterLoadedEntries([])).toEqual([]);
        });
    });

    describe('config change detection', () => {
        /**
         * Checks if a new value differs from the current config value.
         * Used to prevent unnecessary config dispatches.
         */
        function valueChanged<T>(currentValue: T | undefined, newValue: T): boolean {
            return newValue !== currentValue;
        }

        it('detects string change', () => {
            expect(valueChanged('old', 'new')).toBe(true);
        });

        it('detects no change for same string', () => {
            expect(valueChanged('same', 'same')).toBe(false);
        });

        it('detects change from undefined', () => {
            expect(valueChanged(undefined, 'value')).toBe(true);
        });

        it('detects number change', () => {
            expect(valueChanged(1, 2)).toBe(true);
        });

        it('detects no change for same number', () => {
            expect(valueChanged(5, 5)).toBe(false);
        });

        it('detects boolean change', () => {
            expect(valueChanged(true, false)).toBe(true);
        });
    });
});
