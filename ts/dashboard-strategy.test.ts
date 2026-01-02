import { describe, expect, it } from 'vitest';

import { DEFAULT_INCLUDE_CODE_DATA_VIEW } from './const';
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
            const hass = createMockHass({
                callWS: () => [
                    { entry_id: 'entry1', title: 'Front Door' },
                    { entry_id: 'entry2', title: 'Back Door' }
                ]
            });

            const result = await LockCodeManagerDashboardStrategy.generate(testConfig({}), hass);

            expect(result.title).toBe('Lock Code Manager');
            expect(result.views).toHaveLength(2);
            expect(result.views[0]).toEqual({
                path: 'front-door',
                strategy: {
                    config_entry_id: 'entry1',
                    include_code_slot_sensors: undefined,
                    include_in_sync_sensors: undefined,
                    type: 'custom:lock-code-manager'
                },
                title: 'Front Door'
            });
            expect(result.views[1]).toEqual({
                path: 'back-door',
                strategy: {
                    config_entry_id: 'entry2',
                    include_code_slot_sensors: undefined,
                    include_in_sync_sensors: undefined,
                    type: 'custom:lock-code-manager'
                },
                title: 'Back Door'
            });
        });

        it('adds placeholder view when only one config entry exists', async () => {
            const hass = createMockHass({
                callWS: () => [{ entry_id: 'entry1', title: 'Only Lock' }]
            });

            const result = await LockCodeManagerDashboardStrategy.generate(testConfig({}), hass);

            expect(result.views).toHaveLength(2);
            expect(result.views[1]).toEqual({ title: ZERO_WIDTH_SPACE });
        });

        it('does not add placeholder when multiple config entries exist', async () => {
            const hass = createMockHass({
                callWS: () => [
                    { entry_id: 'entry1', title: 'Lock 1' },
                    { entry_id: 'entry2', title: 'Lock 2' }
                ]
            });

            const result = await LockCodeManagerDashboardStrategy.generate(testConfig({}), hass);

            expect(result.views).toHaveLength(2);
            expect(result.views.every((v) => 'strategy' in v)).toBe(true);
        });

        it('passes include_code_slot_sensors config to view strategies', async () => {
            const hass = createMockHass({
                callWS: () => [{ entry_id: 'entry1', title: 'Lock' }]
            });

            const result = await LockCodeManagerDashboardStrategy.generate(
                testConfig({ include_code_slot_sensors: true }),
                hass
            );

            const view = result.views[0] as { strategy: { include_code_slot_sensors: boolean } };
            expect(view.strategy.include_code_slot_sensors).toBe(true);
        });

        it('passes include_in_sync_sensors config to view strategies', async () => {
            const hass = createMockHass({
                callWS: () => [{ entry_id: 'entry1', title: 'Lock' }]
            });

            const result = await LockCodeManagerDashboardStrategy.generate(
                testConfig({ include_in_sync_sensors: false }),
                hass
            );

            const view = result.views[0] as { strategy: { include_in_sync_sensors: boolean } };
            expect(view.strategy.include_in_sync_sensors).toBe(false);
        });

        it('slugifies config entry titles for view paths', async () => {
            const hass = createMockHass({
                callWS: () => [{ entry_id: 'entry1', title: 'My Special Lock!' }]
            });

            const result = await LockCodeManagerDashboardStrategy.generate(testConfig({}), hass);

            const view = result.views[0] as { path: string };
            expect(view.path).toBe('my-special-lock');
        });

        describe('include_code_data_view', () => {
            it('adds User Codes view by default when DEFAULT_INCLUDE_CODE_DATA_VIEW is true', async () => {
                // Skip if default is false
                if (!DEFAULT_INCLUDE_CODE_DATA_VIEW) return;

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
                    cards: Array<{ cards?: Array<{ lock_entity_id?: string }>; type: string }>;
                    title: string;
                };
                expect(lockCodesView).toBeDefined();
                expect(lockCodesView.title).toBe('User Codes');
                expect(lockCodesView.cards).toHaveLength(2);
                expect(lockCodesView.cards[1].type).toBe('grid');
                expect(lockCodesView.cards[1].cards).toHaveLength(1);
                expect(lockCodesView.cards[1].cards?.[0].lock_entity_id).toBe('lock.front_door');
            });

            it('does not add Lock Codes view when include_code_data_view is false', async () => {
                const hass = createDashboardMockHass({
                    configEntries: [{ entry_id: 'entry1', title: 'Front Door' }],
                    locksPerEntry: { entry1: ['lock.front_door'] }
                });

                const result = await LockCodeManagerDashboardStrategy.generate(
                    testConfig({ include_code_data_view: false }),
                    hass
                );

                const lockCodesView = result.views.find(
                    (v) => 'path' in v && v.path === 'user-codes'
                );
                expect(lockCodesView).toBeUndefined();
            });

            it('adds User Codes view when include_code_data_view is true', async () => {
                const hass = createDashboardMockHass({
                    configEntries: [{ entry_id: 'entry1', title: 'Front Door' }],
                    locksPerEntry: { entry1: ['lock.front_door', 'lock.back_door'] }
                });

                const result = await LockCodeManagerDashboardStrategy.generate(
                    testConfig({ include_code_data_view: true }),
                    hass
                );

                const lockCodesView = result.views.find(
                    (v) => 'path' in v && v.path === 'user-codes'
                ) as { cards: Array<{ cards?: Array<{ lock_entity_id: string }>; type: string }> };
                expect(lockCodesView).toBeDefined();
                // Cards are wrapped in markdown + grid
                expect(lockCodesView.cards).toHaveLength(2);
                expect(lockCodesView.cards[1].type).toBe('grid');
                expect(lockCodesView.cards[1].cards).toHaveLength(2);
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
                    testConfig({ include_code_data_view: true }),
                    hass
                );

                const lockCodesView = result.views.find(
                    (v) => 'path' in v && v.path === 'user-codes'
                ) as { cards: Array<{ cards?: Array<{ lock_entity_id: string }>; type: string }> };
                // Cards are wrapped in markdown + grid
                expect(lockCodesView.cards).toHaveLength(2);
                expect(lockCodesView.cards[1].type).toBe('grid');
                expect(lockCodesView.cards[1].cards).toHaveLength(3);
            });

            it('shows "No locks found" message when no locks exist', async () => {
                const hass = createDashboardMockHass({
                    configEntries: [{ entry_id: 'entry1', title: 'Front Door' }],
                    locksPerEntry: { entry1: [] }
                });

                const result = await LockCodeManagerDashboardStrategy.generate(
                    testConfig({ include_code_data_view: true }),
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
