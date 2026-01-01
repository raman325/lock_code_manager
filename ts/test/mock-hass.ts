import { vi } from 'vitest';

import { HomeAssistant } from '../ha_type_stubs';

type CallWSHandler = (msg: { [key: string]: unknown; type: string }) => unknown;

interface MockHassOptions {
    /** Handler for callWS calls. Return value is used as the resolved value. */
    callWS?: CallWSHandler;
    /** HA config state (e.g., 'RUNNING', 'NOT_RUNNING') */
    configState?: string;
    /** Map of entity_id to state objects */
    states?: Record<string, { attributes?: Record<string, unknown>; state: string }>;
}

/**
 * Creates a mock HomeAssistant object for testing.
 *
 * @example
 * ```ts
 * const hass = createMockHass({
 *     callWS: (msg) => {
 *         if (msg.type === 'config_entries/get') {
 *             return [{ entry_id: '123', title: 'Test' }];
 *         }
 *         return [];
 *     }
 * });
 * ```
 */
export function createMockHass(options: MockHassOptions = {}): HomeAssistant {
    const callWSMock = vi.fn().mockImplementation((msg: { type: string }) => {
        if (options.callWS) {
            return Promise.resolve(options.callWS(msg));
        }
        return Promise.resolve(undefined);
    });

    return {
        callWS: callWSMock,
        config: {
            state: options.configState ?? 'RUNNING'
        },
        states: options.states ?? {}
    } as unknown as HomeAssistant;
}

/**
 * Creates a mock callWS that returns different responses based on message type.
 */
export function createCallWSRouter(
    routes: Record<string, (msg: { [key: string]: unknown; type: string }) => unknown>
): CallWSHandler {
    return (msg) => {
        const handler = routes[msg.type];
        if (handler) {
            return handler(msg);
        }
        throw new Error(`Unexpected callWS type: ${msg.type}`);
    };
}
