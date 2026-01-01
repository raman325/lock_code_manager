import { describe, expect, it } from 'vitest';

import { slugify } from './slugify';

describe('slugify', () => {
    describe('basic transformations', () => {
        it('converts to lowercase', () => {
            expect(slugify('Hello World')).toBe('hello-world');
        });

        it('replaces spaces with delimiter', () => {
            expect(slugify('hello world')).toBe('hello-world');
        });

        it('handles empty string', () => {
            expect(slugify('')).toBe('');
        });

        it('returns unknown for non-alphanumeric input', () => {
            expect(slugify('!!!')).toBe('unknown');
        });
    });

    describe('special characters', () => {
        it('transliterates accented characters', () => {
            expect(slugify('café')).toBe('cafe');
            expect(slugify('naïve')).toBe('naive');
            expect(slugify('über')).toBe('uber');
        });

        it('handles various diacritics', () => {
            expect(slugify('àáâäæãåā')).toBe('aaaaaaaa');
            expect(slugify('èéêëēėęě')).toBe('eeeeeeee');
        });

        it('replaces middle dots with delimiter', () => {
            expect(slugify('hello·world')).toBe('hello-world');
        });
    });

    describe('number handling', () => {
        it('preserves numbers', () => {
            expect(slugify('test123')).toBe('test123');
        });

        it('removes commas between numbers', () => {
            expect(slugify('1,000,000')).toBe('1000000');
        });
    });

    describe('delimiter handling', () => {
        it('removes leading delimiters', () => {
            expect(slugify('---hello')).toBe('hello');
        });

        it('removes trailing delimiters', () => {
            expect(slugify('hello---')).toBe('hello');
        });

        it('collapses multiple delimiters', () => {
            expect(slugify('hello   world')).toBe('hello-world');
        });
    });

    describe('custom delimiter', () => {
        it('uses underscore as delimiter', () => {
            expect(slugify('hello world', '_')).toBe('hello_world');
        });

        it('collapses multiple custom delimiters', () => {
            expect(slugify('hello   world', '_')).toBe('hello_world');
        });
    });
});
