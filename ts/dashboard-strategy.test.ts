import { describe, expect, it } from 'vitest';

import { DEFAULT_SHOW_ALL_CODES_FOR_LOCKS } from './const';
import {
    LockCodeManagerDashboardStrategy,
    NO_CONFIG_MESSAGE,
    ZERO_WIDTH_SPACE
} from './dashboard-strategy';
import { createMockHass } from './test/mock-hass';
import { LockCodeManagerDashboardStrategyConfig } from './types';

// Helper to create partial config for tests
function testConfig(
    config: Partial<LockCodeManagerDashboardStrategyConfig>
): LockCodeManagerDashboardStrategyConfig {
    return config as LockCodeManagerDashboardStrategyConfig;
}

// Helper to create mock that handles different WS message types
function createDashboardMockHass(options: {
    configEntries?: Array<{ entry_id: string; title: string }>;
    locksPerEntry?: Record<string, string[]>;
}) {
    const { configEntries = [], locksPerEntry = {} } = options;
    return createMockHass({
        callWS: (msg) => {
            if (msg.type === 'config_entries/get') {
                return configEntries;
            }
            if (msg.type === 'lock_code_manager/get_slot_calendar_data') {
                const entryId = msg.config_entry_id as string;
                return {
                    locks: locksPerEntry[entryId] ?? [],
                    slots: {}
                };
            }
            return undefined;
        }
    });
}

describe('LockCodeManagerDashboardStrategy', () => {
    describe('generate', () => {
        it('returns error view when no config entries exist', async () => {
            const hass = createMockHass({
                callWS: () => []
            });

            const result = await LockCodeManagerDashboardStrategy.generate(testConfig({}), hass);

            expect(result).toEqual({
                title: 'Lock Code Manager',
                views: [
                    {
                        cards: [{ content: NO_CONFIG_MESSAGE, type: 'markdown' }],
                        title: 'Lock Code Manager'
                    }
                ]
            });
        });

        it('generates views for each config entry', async () => {
            const hass = createDashboardMockHass({
                configEntries: [
                    { entry_id: 'entry1', title: 'Front Door' },
                    { entry_id: 'entry2', title: 'Back Door' }
                ],
                locksPerEntry: { entry1: [], entry2: [] }
            });

            const result = await LockCodeManagerDashboardStrategy.generate(
                testConfig({ show_all_codes_for_locks: false }),
                hass
            );

            expect(result.title).toBe('Lock Code Manager');
            expect(result.views).toHaveLength(2);
            expect(result.views[0]).toEqual({
                path: 'front-door',
                strategy: {
                    code_display: undefined,
                    collapsed_sections: undefined,
                    config_entry_id: 'entry1',
                    show_code_sensors: undefined,
                    show_conditions: undefined,
                    show_lock_status: undefined,
                    show_lock_sync: undefined,
                    type: 'custom:lock-code-manager',
                    use_slot_cards: undefined
                },
                title: 'Front Door'
            });
            expect(result.views[1]).toEqual({
                path: 'back-door',
                strategy: {
                    code_display: undefined,
                    collapsed_sections: undefined,
                    config_entry_id: 'entry2',
                    show_code_sensors: undefined,
                    show_conditions: undefined,
                    show_lock_status: undefined,
                    show_lock_sync: undefined,
                    type: 'custom:lock-code-manager',
                    use_slot_cards: undefined
                },
                title: 'Back Door'
            });
        });

        it('adds placeholder view when only one config entry exists', async () => {
            const hass = createDashboardMockHass({
                configEntries: [{ entry_id: 'entry1', title: 'Only Lock' }],
                locksPerEntry: { entry1: [] }
            });

            const result = await LockCodeManagerDashboardStrategy.generate(
                testConfig({ show_all_codes_for_locks: false }),
                hass
            );

            expect(result.views).toHaveLength(2);
            expect(result.views[1]).toEqual({ title: ZERO_WIDTH_SPACE });
        });

        it('does not add placeholder when multiple config entries exist', async () => {
            const hass = createDashboardMockHass({
                configEntries: [
                    { entry_id: 'entry1', title: 'Lock 1' },
                    { entry_id: 'entry2', title: 'Lock 2' }
                ],
                locksPerEntry: { entry1: [], entry2: [] }
            });

            const result = await LockCodeManagerDashboardStrategy.generate(
                testConfig({ show_all_codes_for_locks: false }),
                hass
            );

            expect(result.views).toHaveLength(2);
            expect(result.views.every((v) => 'strategy' in v)).toBe(true);
        });

        it('passes show_code_sensors config to view strategies', async () => {
            const hass = createDashboardMockHass({
                configEntries: [{ entry_id: 'entry1', title: 'Lock' }],
                locksPerEntry: { entry1: [] }
            });

            const result = await LockCodeManagerDashboardStrategy.generate(
                testConfig({ show_code_sensors: true, show_all_codes_for_locks: false }),
                hass
            );

            const view = result.views[0] as { strategy: { show_code_sensors: boolean } };
            expect(view.strategy.show_code_sensors).toBe(true);
        });

        it('passes show_lock_sync config to view strategies', async () => {
            const hass = createDashboardMockHass({
                configEntries: [{ entry_id: 'entry1', title: 'Lock' }],
                locksPerEntry: { entry1: [] }
            });

            const result = await LockCodeManagerDashboardStrategy.generate(
                testConfig({ show_lock_sync: false, show_all_codes_for_locks: false }),
                hass
            );

            const view = result.views[0] as { strategy: { show_lock_sync: boolean } };
            expect(view.strategy.show_lock_sync).toBe(false);
        });

        it('slugifies config entry titles for view paths', async () => {
            const hass = createDashboardMockHass({
                configEntries: [{ entry_id: 'entry1', title: 'My Special Lock!' }],
                locksPerEntry: { entry1: [] }
            });

            const result = await LockCodeManagerDashboardStrategy.generate(
                testConfig({ show_all_codes_for_locks: false }),
                hass
            );

            const view = result.views[0] as { path: string };
            expect(view.path).toBe('my-special-lock');
        });

        describe('show_all_codes_for_locks', () => {
            it('adds User Codes view by default when DEFAULT_SHOW_ALL_CODES_FOR_LOCKS is true', async () => {
                // Skip if default is false
                if (!DEFAULT_SHOW_ALL_CODES_FOR_LOCKS) return;

                const hass = createDashboardMockHass({
                    configEntries: [{ entry_id: 'entry1', title: 'Front Door' }],
                    locksPerEntry: { entry1: ['lock.front_door'] }
                });

                const result = await LockCodeManagerDashboardStrategy.generate(
                    testConfig({}),
                    hass
                );

                const lockCodesView = result.views.find(
                    (v) => 'path' in v && v.path === 'user-codes'
                ) as {
                    sections: Array<{
                        cards: Array<{ lock_entity_id?: string }>;
                        title: string;
                        type: string;
                    }>;
                    title: string;
                    type: string;
                };
                expect(lockCodesView).toBeDefined();
                expect(lockCodesView.title).toBe('User Codes');
                expect(lockCodesView.type).toBe('sections');
                expect(lockCodesView.sections).toHaveLength(1);
                expect(lockCodesView.sections[0].type).toBe('grid');
                expect(lockCodesView.sections[0].cards).toHaveLength(1);
                expect(lockCodesView.sections[0].cards[0].lock_entity_id).toBe('lock.front_door');
            });

            it('does not add Lock Codes view when show_all_codes_for_locks is false', async () => {
                const hass = createDashboardMockHass({
                    configEntries: [{ entry_id: 'entry1', title: 'Front Door' }],
                    locksPerEntry: { entry1: ['lock.front_door'] }
                });

                const result = await LockCodeManagerDashboardStrategy.generate(
                    testConfig({ show_all_codes_for_locks: false }),
                    hass
                );

                const lockCodesView = result.views.find(
                    (v) => 'path' in v && v.path === 'user-codes'
                );
                expect(lockCodesView).toBeUndefined();
            });

            it('adds User Codes view when show_all_codes_for_locks is true', async () => {
                const hass = createDashboardMockHass({
                    configEntries: [{ entry_id: 'entry1', title: 'Front Door' }],
                    locksPerEntry: { entry1: ['lock.front_door', 'lock.back_door'] }
                });

                const config = DEFAULT_SHOW_ALL_CODES_FOR_LOCKS
                    ? testConfig({})
                    : testConfig({ show_all_codes_for_locks: true });
                const result = await LockCodeManagerDashboardStrategy.generate(config, hass);

                const lockCodesView = result.views.find(
                    (v) => 'path' in v && v.path === 'user-codes'
                ) as {
                    sections: Array<{ cards: Array<{ lock_entity_id: string }>; type: string }>;
                    type: string;
                };
                expect(lockCodesView).toBeDefined();
                expect(lockCodesView.type).toBe('sections');
                expect(lockCodesView.sections).toHaveLength(2);
                expect(lockCodesView.sections[0].type).toBe('grid');
                expect(lockCodesView.sections[0].cards).toHaveLength(1);
            });

            it('deduplicates locks across multiple config entries', async () => {
                const hass = createDashboardMockHass({
                    configEntries: [
                        { entry_id: 'entry1', title: 'Config 1' },
                        { entry_id: 'entry2', title: 'Config 2' }
                    ],
                    locksPerEntry: {
                        entry1: ['lock.front_door', 'lock.shared'],
                        entry2: ['lock.back_door', 'lock.shared']
                    }
                });

                const result = await LockCodeManagerDashboardStrategy.generate(
                    testConfig({ show_all_codes_for_locks: true }),
                    hass
                );

                const lockCodesView = result.views.find(
                    (v) => 'path' in v && v.path === 'user-codes'
                ) as {
                    sections: Array<{ cards: Array<{ lock_entity_id: string }>; type: string }>;
                    type: string;
                };
                expect(lockCodesView.type).toBe('sections');
                // 3 unique locks: front_door, back_door, shared (deduplicated)
                expect(lockCodesView.sections).toHaveLength(3);
                expect(lockCodesView.sections[0].type).toBe('grid');
            });

            it('shows "No locks found" message when no locks exist', async () => {
                const hass = createDashboardMockHass({
                    configEntries: [{ entry_id: 'entry1', title: 'Front Door' }],
                    locksPerEntry: { entry1: [] }
                });

                const result = await LockCodeManagerDashboardStrategy.generate(
                    testConfig({ show_all_codes_for_locks: true }),
                    hass
                );

                const lockCodesView = result.views.find(
                    (v) => 'path' in v && v.path === 'user-codes'
                ) as { cards: Array<{ content?: string; type: string }> };
                expect(lockCodesView).toBeDefined();
                expect(lockCodesView.cards).toHaveLength(1);
                expect(lockCodesView.cards[0].type).toBe('markdown');
                expect(lockCodesView.cards[0].content).toContain('No locks found');
            });
        });
    });
});
