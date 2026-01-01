import { describe, expect, it } from 'vitest';

import { capitalize } from './util';

describe('capitalize', () => {
    it('capitalizes first letter of lowercase word', () => {
        expect(capitalize('hello')).toBe('Hello');
    });

    it('keeps already capitalized word unchanged', () => {
        expect(capitalize('Hello')).toBe('Hello');
    });

    it('capitalizes first letter and keeps rest unchanged', () => {
        expect(capitalize('hELLO')).toBe('HELLO');
    });

    it('handles single character', () => {
        expect(capitalize('a')).toBe('A');
    });

    it('handles empty string', () => {
        expect(capitalize('')).toBe('');
    });

    it('handles string starting with number', () => {
        expect(capitalize('123abc')).toBe('123abc');
    });

    it('handles multi-word string (only capitalizes first char)', () => {
        expect(capitalize('hello world')).toBe('Hello world');
    });
});
