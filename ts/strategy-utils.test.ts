import { describe, expect, it } from 'vitest';

import {
    createErrorView,
    createStartingView,
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

    it('returns error when neither is provided', () => {
        expect(validateViewStrategyConfig({})).toEqual({
            error: 'missing',
            valid: false
        });
    });

    it('returns error when both are provided', () => {
        expect(
            validateViewStrategyConfig({
                config_entry_id: 'abc123',
                config_entry_title: 'My Config'
            })
        ).toEqual({
            error: 'both_specified',
            valid: false
        });
    });

    it('treats empty string as provided', () => {
        expect(validateViewStrategyConfig({ config_entry_id: '' })).toEqual({
            valid: true
        });
    });
});

describe('createErrorView', () => {
    it('creates error view with default title', () => {
        const view = createErrorView('Something went wrong');
        expect(view).toEqual({
            cards: [{ content: 'Something went wrong', type: 'markdown' }],
            title: 'Lock Code Manager'
        });
    });

    it('creates error view with custom title', () => {
        const view = createErrorView('Error message', 'Custom Title');
        expect(view).toEqual({
            cards: [{ content: 'Error message', type: 'markdown' }],
            title: 'Custom Title'
        });
    });
});

describe('createStartingView', () => {
    it('creates starting view with starting card', () => {
        const view = createStartingView();
        expect(view).toEqual({
            cards: [{ type: 'starting' }]
        });
    });
});

describe('formatConfigEntryNotFoundError', () => {
    it('formats error with config_entry_id', () => {
        const error = formatConfigEntryNotFoundError('abc123', undefined);
        expect(error).toBe('## ERROR: No Lock Code Manager configuration with ID `abc123` found!');
    });

    it('formats error with config_entry_title', () => {
        const error = formatConfigEntryNotFoundError(undefined, 'My Config');
        expect(error).toBe(
            '## ERROR: No Lock Code Manager configuration called `My Config` found!'
        );
    });

    it('prefers config_entry_id when both provided', () => {
        const error = formatConfigEntryNotFoundError('abc123', 'My Config');
        expect(error).toBe('## ERROR: No Lock Code Manager configuration with ID `abc123` found!');
    });
});
