import { STATE_NOT_RUNNING } from 'home-assistant-js-websocket';
import { describe, expect, it, vi } from 'vitest';

import { createMockHass } from './test/mock-hass';
import { LockCodeManagerViewStrategyConfig } from './types';
import { LockCodeManagerViewStrategy } from './view-strategy';

// Mock generateView since it has its own complex logic
vi.mock('./generate-view', () => {
    return {
        generateView: vi.fn().mockResolvedValue({
            badges: [],
            cards: [{ type: 'entities' }],
            title: 'Generated View'
        })
    };
});

// Helper to create partial config for tests
function testConfig(
    config: Partial<LockCodeManagerViewStrategyConfig>
): LockCodeManagerViewStrategyConfig {
    return config as LockCodeManagerViewStrategyConfig;
}

describe('LockCodeManagerViewStrategy', () => {
    describe('generate', () => {
        it('returns starting view when HA is not running', async () => {
            const hass = createMockHass({
                configState: STATE_NOT_RUNNING
            });

            const result = await LockCodeManagerViewStrategy.generate(
                testConfig({ config_entry_id: 'test' }),
                hass
            );

            expect(result).toEqual({
                cards: [{ type: 'starting' }]
            });
        });

        it('returns error when neither config_entry_id nor config_entry_title provided', async () => {
            const hass = createMockHass();

            const result = await LockCodeManagerViewStrategy.generate(testConfig({}), hass);

            expect(result.cards?.[0]).toHaveProperty('content');
            const { content } = result.cards?.[0] as { content: string };
            expect(content).toContain('ERROR');
            expect(content).toContain('config_entry_title');
            expect(content).toContain('config_entry_id');
        });

        it('returns error when both config_entry_id and config_entry_title provided', async () => {
            const hass = createMockHass();

            const result = await LockCodeManagerViewStrategy.generate(
                testConfig({ config_entry_id: 'id123', config_entry_title: 'My Title' }),
                hass
            );

            const { content } = result.cards?.[0] as { content: string };
            expect(content).toContain('ERROR');
        });

        it('calls generateView with correct parameters on success', async () => {
            const mockEntities = [{ entity_id: 'switch.test' }];
            const mockConfigEntry = { entry_id: 'test', title: 'Test' };
            const hass = createMockHass({
                callWS: () => {
                    return {
                        config_entry: mockConfigEntry,
                        entities: mockEntities
                    };
                }
            });

            const { generateView } = await import('./generate-view');

            await LockCodeManagerViewStrategy.generate(
                testConfig({ config_entry_id: 'test' }),
                hass
            );

            // Expected: all show_* defaults are true
            expect(generateView).toHaveBeenCalledWith(
                hass,
                mockConfigEntry,
                true,
                true,
                true,
                'masked_with_reveal',
                true,
                true,
                true,
                undefined
            );
        });

        it('passes show_code_sensors to generateView (new option)', async () => {
            const hass = createMockHass({
                callWS: () => {
                    return { config_entry: {}, entities: [] };
                }
            });

            const { generateView } = await import('./generate-view');

            await LockCodeManagerViewStrategy.generate(
                testConfig({ config_entry_id: 'test', show_code_sensors: false }),
                hass
            );

            // show_code_sensors=false (set explicitly), rest are defaults (all true)
            expect(generateView).toHaveBeenCalledWith(
                hass,
                expect.anything(),
                false,
                true,
                true,
                'masked_with_reveal',
                true,
                true,
                true,
                undefined
            );
        });

        it('maps legacy include_code_slot_sensors to show_code_sensors', async () => {
            const hass = createMockHass({
                callWS: () => {
                    return { config_entry: {}, entities: [] };
                }
            });

            const { generateView } = await import('./generate-view');

            await LockCodeManagerViewStrategy.generate(
                testConfig({ config_entry_id: 'test', include_code_slot_sensors: false }),
                hass
            );

            // show_code_sensors=false (from legacy include_code_slot_sensors), rest are defaults
            expect(generateView).toHaveBeenCalledWith(
                hass,
                expect.anything(),
                false,
                true,
                true,
                'masked_with_reveal',
                true,
                true,
                true,
                undefined
            );
        });

        it('passes show_lock_sync to generateView (new option)', async () => {
            const hass = createMockHass({
                callWS: () => {
                    return { config_entry: {}, entities: [] };
                }
            });

            const { generateView } = await import('./generate-view');

            await LockCodeManagerViewStrategy.generate(
                testConfig({ config_entry_id: 'test', show_lock_sync: false }),
                hass
            );

            // show_lock_sync=false (set explicitly), rest are defaults (all true)
            expect(generateView).toHaveBeenCalledWith(
                hass,
                expect.anything(),
                true,
                false,
                true,
                'masked_with_reveal',
                true,
                true,
                true,
                undefined
            );
        });

        it('maps legacy include_in_sync_sensors to show_lock_sync', async () => {
            const hass = createMockHass({
                callWS: () => {
                    return { config_entry: {}, entities: [] };
                }
            });

            const { generateView } = await import('./generate-view');

            await LockCodeManagerViewStrategy.generate(
                testConfig({ config_entry_id: 'test', include_in_sync_sensors: false }),
                hass
            );

            // show_lock_sync=false (from legacy include_in_sync_sensors), rest are defaults (all true)
            expect(generateView).toHaveBeenCalledWith(
                hass,
                expect.anything(),
                true,
                false,
                true,
                'masked_with_reveal',
                true,
                true,
                true,
                undefined
            );
        });

        it('new option takes precedence over legacy option', async () => {
            const hass = createMockHass({
                callWS: () => {
                    return { config_entry: {}, entities: [] };
                }
            });

            const { generateView } = await import('./generate-view');

            // New option (show_code_sensors: false) should take precedence over legacy (include_code_slot_sensors: true)
            await LockCodeManagerViewStrategy.generate(
                testConfig({
                    config_entry_id: 'test',
                    include_code_slot_sensors: true,
                    show_code_sensors: false
                }),
                hass
            );

            // show_code_sensors=false (new option takes precedence over legacy), rest are defaults (all true)
            expect(generateView).toHaveBeenCalledWith(
                hass,
                expect.anything(),
                false,
                true,
                true,
                'masked_with_reveal',
                true,
                true,
                true,
                undefined
            );
        });

        it('passes show_lock_cards to generateView', async () => {
            const hass = createMockHass({
                callWS: () => {
                    return { config_entry: {}, entities: [] };
                }
            });

            const { generateView } = await import('./generate-view');

            await LockCodeManagerViewStrategy.generate(
                testConfig({ config_entry_id: 'test', show_lock_cards: false }),
                hass
            );

            // show_lock_cards=false (3rd boolean param), rest are defaults
            expect(generateView).toHaveBeenCalledWith(
                hass,
                expect.anything(),
                true,
                true,
                false,
                'masked_with_reveal',
                true,
                true,
                true,
                undefined
            );
        });

        it('maps legacy show_all_codes_for_locks to show_lock_cards', async () => {
            const hass = createMockHass({
                callWS: () => {
                    return { config_entry: {}, entities: [] };
                }
            });

            const { generateView } = await import('./generate-view');

            await LockCodeManagerViewStrategy.generate(
                testConfig({ config_entry_id: 'test', show_all_codes_for_locks: false }),
                hass
            );

            // show_lock_cards=false (from legacy show_all_codes_for_locks), rest are defaults
            expect(generateView).toHaveBeenCalledWith(
                hass,
                expect.anything(),
                true,
                true,
                false,
                'masked_with_reveal',
                true,
                true,
                true,
                undefined
            );
        });

        it('show_lock_cards takes precedence over show_all_codes_for_locks', async () => {
            const hass = createMockHass({
                callWS: () => {
                    return { config_entry: {}, entities: [] };
                }
            });

            const { generateView } = await import('./generate-view');

            await LockCodeManagerViewStrategy.generate(
                testConfig({
                    config_entry_id: 'test',
                    show_all_codes_for_locks: false,
                    show_lock_cards: true
                }),
                hass
            );

            // New option takes precedence, show_lock_cards=true
            expect(generateView).toHaveBeenCalledWith(
                hass,
                expect.anything(),
                true,
                true,
                true,
                'masked_with_reveal',
                true,
                true,
                true,
                undefined
            );
        });

        it('passes code_display to generateView', async () => {
            const hass = createMockHass({
                callWS: () => {
                    return { config_entry: {}, entities: [] };
                }
            });

            const { generateView } = await import('./generate-view');

            await LockCodeManagerViewStrategy.generate(
                testConfig({ config_entry_id: 'test', code_display: 'unmasked' }),
                hass
            );

            expect(generateView).toHaveBeenCalledWith(
                hass,
                expect.anything(),
                true,
                true,
                true,
                'unmasked',
                true,
                true,
                true,
                undefined
            );
        });

        it('maps legacy code_data_view_code_display to code_display', async () => {
            const hass = createMockHass({
                callWS: () => {
                    return { config_entry: {}, entities: [] };
                }
            });

            const { generateView } = await import('./generate-view');

            await LockCodeManagerViewStrategy.generate(
                testConfig({ config_entry_id: 'test', code_data_view_code_display: 'masked' }),
                hass
            );

            expect(generateView).toHaveBeenCalledWith(
                hass,
                expect.anything(),
                true,
                true,
                true,
                'masked',
                true,
                true,
                true,
                undefined
            );
        });

        it('code_display takes precedence over code_data_view_code_display', async () => {
            const hass = createMockHass({
                callWS: () => {
                    return { config_entry: {}, entities: [] };
                }
            });

            const { generateView } = await import('./generate-view');

            await LockCodeManagerViewStrategy.generate(
                testConfig({
                    config_entry_id: 'test',
                    code_data_view_code_display: 'masked',
                    code_display: 'unmasked'
                }),
                hass
            );

            expect(generateView).toHaveBeenCalledWith(
                hass,
                expect.anything(),
                true,
                true,
                true,
                'unmasked',
                true,
                true,
                true,
                undefined
            );
        });

        it('passes use_slot_cards to generateView', async () => {
            const hass = createMockHass({
                callWS: () => {
                    return { config_entry: {}, entities: [] };
                }
            });

            const { generateView } = await import('./generate-view');

            await LockCodeManagerViewStrategy.generate(
                testConfig({ config_entry_id: 'test', use_slot_cards: false }),
                hass
            );

            expect(generateView).toHaveBeenCalledWith(
                hass,
                expect.anything(),
                true,
                true,
                true,
                'masked_with_reveal',
                false,
                true,
                true,
                undefined
            );
        });

        it('passes show_conditions to generateView', async () => {
            const hass = createMockHass({
                callWS: () => {
                    return { config_entry: {}, entities: [] };
                }
            });

            const { generateView } = await import('./generate-view');

            await LockCodeManagerViewStrategy.generate(
                testConfig({ config_entry_id: 'test', show_conditions: false }),
                hass
            );

            expect(generateView).toHaveBeenCalledWith(
                hass,
                expect.anything(),
                true,
                true,
                true,
                'masked_with_reveal',
                true,
                false,
                true,
                undefined
            );
        });

        it('passes show_lock_status to generateView', async () => {
            const hass = createMockHass({
                callWS: () => {
                    return { config_entry: {}, entities: [] };
                }
            });

            const { generateView } = await import('./generate-view');

            await LockCodeManagerViewStrategy.generate(
                testConfig({ config_entry_id: 'test', show_lock_status: false }),
                hass
            );

            expect(generateView).toHaveBeenCalledWith(
                hass,
                expect.anything(),
                true,
                true,
                true,
                'masked_with_reveal',
                true,
                true,
                false,
                undefined
            );
        });

        it('passes collapsed_sections to generateView', async () => {
            const hass = createMockHass({
                callWS: () => {
                    return { config_entry: {}, entities: [] };
                }
            });

            const { generateView } = await import('./generate-view');

            await LockCodeManagerViewStrategy.generate(
                testConfig({
                    config_entry_id: 'test',
                    collapsed_sections: ['conditions', 'lock_status']
                }),
                hass
            );

            expect(generateView).toHaveBeenCalledWith(
                hass,
                expect.anything(),
                true,
                true,
                true,
                'masked_with_reveal',
                true,
                true,
                true,
                ['conditions', 'lock_status']
            );
        });

        it('returns error view when callWS throws', async () => {
            const hass = createMockHass({
                callWS: () => {
                    throw new Error('Not found');
                }
            });

            const result = await LockCodeManagerViewStrategy.generate(
                testConfig({ config_entry_id: 'missing123' }),
                hass
            );

            const { content } = result.cards?.[0] as { content: string };
            expect(content).toContain('ERROR');
            expect(content).toContain('missing123');
        });

        it('returns error with title when config_entry_title is used', async () => {
            const hass = createMockHass({
                callWS: () => {
                    throw new Error('Not found');
                }
            });

            const result = await LockCodeManagerViewStrategy.generate(
                testConfig({ config_entry_title: 'Missing Config' }),
                hass
            );

            const { content } = result.cards?.[0] as { content: string };
            expect(content).toContain('Missing Config');
        });
    });
});
