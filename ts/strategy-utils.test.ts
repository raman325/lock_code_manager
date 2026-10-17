import { describe, expect, it } from 'vitest';

import {
    createErrorView,
    formatConfigEntryNotFoundError,
    validateViewStrategyConfig
} from './strategy-utils';

describe('validateViewStrategyConfig', () => {
    it('returns valid when only config_entry_id is provided', () => {
        expect(validateViewStrategyConfig({ config_entry_id: 'abc123' })).toEqual({
            valid: true
        });
    });

    it('returns valid when only config_entry_title is provided', () => {
        expect(validateViewStrategyConfig({ config_entry_title: 'My Config' })).toEqual({
            valid: true
        });
    });

    it('treats empty string as provided', () => {
        expect(validateViewStrategyConfig({ config_entry_id: '' })).toEqual({
            valid: true
        });
    });
});

describe('createErrorView', () => {
    it('creates error view with custom title', () => {
        const view = createErrorView('Error message', 'Custom Title');
        expect(view).toEqual({
            cards: [{ content: 'Error message', type: 'markdown' }],
            title: 'Custom Title'
        });
    });
});

describe('formatConfigEntryNotFoundError', () => {
    it('prefers config_entry_id when both provided', () => {
        const error = formatConfigEntryNotFoundError('abc123', 'My Config');
        expect(error).toBe('## ERROR: No Lock Code Manager configuration with ID `abc123` found!');
    });
});
