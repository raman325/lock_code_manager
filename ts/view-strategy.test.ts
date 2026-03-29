import { STATE_NOT_RUNNING } from 'home-assistant-js-websocket';
import { describe, expect, it, vi } from 'vitest';

import { createMockHass } from './test/mock-hass';
import { GenerateViewOptions, LockCodeManagerViewStrategyConfig } from './types';
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

const DEFAULT_OPTIONS: GenerateViewOptions = {
    showCodeSensors: true,
    showLockSync: true,
    showLockCards: true,
    codeDisplay: 'masked_with_reveal',
    useSlotCards: true,
    showConditions: true,
    showLockStatus: true,
    collapsedSections: undefined
};

/** Assert generateView was called with the given option overrides. */
async function expectGenerateViewOptions(overrides: Partial<GenerateViewOptions> = {}) {
    const { generateView } = await import('./generate-view');
    expect(generateView).toHaveBeenCalledWith(
        expect.anything(),
        expect.anything(),
        expect.objectContaining({ ...DEFAULT_OPTIONS, ...overrides })
    );
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

        it('calls generateView with default options', async () => {
            const hass = createMockHass({
                callWS: () => {
                    return {
                        config_entry: { entry_id: 'test', title: 'Test' },
                        entities: []
                    };
                }
            });

            await LockCodeManagerViewStrategy.generate(
                testConfig({ config_entry_id: 'test' }),
                hass
            );

            await expectGenerateViewOptions();
        });

        it.each([
            ['show_code_sensors', { show_code_sensors: false }, { showCodeSensors: false }],
            ['show_lock_sync', { show_lock_sync: false }, { showLockSync: false }],
            ['show_lock_cards', { show_lock_cards: false }, { showLockCards: false }],
            ['use_slot_cards', { use_slot_cards: false }, { useSlotCards: false }],
            ['show_conditions', { show_conditions: false }, { showConditions: false }],
            ['show_lock_status', { show_lock_status: false }, { showLockStatus: false }],
            ['code_display', { code_display: 'unmasked' }, { codeDisplay: 'unmasked' }],
            [
                'collapsed_sections',
                {
                    collapsed_sections: ['conditions', 'lock_status'] as (
                        | 'conditions'
                        | 'lock_status'
                    )[]
                },
                { collapsedSections: ['conditions', 'lock_status'] }
            ]
        ] as const)('passes %s to generateView', async (_name, strategyConfig, expectedOptions) => {
            const hass = createMockHass({
                callWS: () => {
                    return { config_entry: {}, entities: [] };
                }
            });

            await LockCodeManagerViewStrategy.generate(
                testConfig({ config_entry_id: 'test', ...strategyConfig }),
                hass
            );

            await expectGenerateViewOptions(expectedOptions);
        });

        it.each([
            [
                'include_code_slot_sensors → show_code_sensors',
                { include_code_slot_sensors: false },
                { showCodeSensors: false }
            ],
            [
                'include_in_sync_sensors → show_lock_sync',
                { include_in_sync_sensors: false },
                { showLockSync: false }
            ],
            [
                'show_all_codes_for_locks → show_lock_cards',
                { show_all_codes_for_locks: false },
                { showLockCards: false }
            ],
            [
                'code_data_view_code_display → code_display',
                { code_data_view_code_display: 'masked' },
                { codeDisplay: 'masked' }
            ]
        ] as const)('maps legacy option %s', async (_name, legacyConfig, expectedOptions) => {
            const hass = createMockHass({
                callWS: () => {
                    return { config_entry: {}, entities: [] };
                }
            });

            await LockCodeManagerViewStrategy.generate(
                testConfig({ config_entry_id: 'test', ...legacyConfig }),
                hass
            );

            await expectGenerateViewOptions(expectedOptions);
        });

        it.each([
            [
                'show_code_sensors over include_code_slot_sensors',
                { include_code_slot_sensors: true, show_code_sensors: false },
                { showCodeSensors: false }
            ],
            [
                'show_lock_cards over show_all_codes_for_locks',
                { show_all_codes_for_locks: false, show_lock_cards: true },
                { showLockCards: true }
            ],
            [
                'code_display over code_data_view_code_display',
                { code_data_view_code_display: 'masked', code_display: 'unmasked' },
                { codeDisplay: 'unmasked' }
            ]
        ] as const)(
            'new option takes precedence: %s',
            async (_name, mixedConfig, expectedOptions) => {
                const hass = createMockHass({
                    callWS: () => {
                        return { config_entry: {}, entities: [] };
                    }
                });

                await LockCodeManagerViewStrategy.generate(
                    testConfig({ config_entry_id: 'test', ...mixedConfig }),
                    hass
                );

                await expectGenerateViewOptions(expectedOptions);
            }
        );

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
