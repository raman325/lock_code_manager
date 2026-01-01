import { describe, expect, it } from 'vitest';

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
    });
});
