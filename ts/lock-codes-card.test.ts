import { describe, expect, it } from 'vitest';

import {
    SLOT_CODE_CHECKING,
    SLOT_CODE_DISABLED,
    SLOT_CODE_EMPTY,
    SLOT_CODE_UNANSWERED,
    SLOT_CODE_UNREADABLE,
    isSlotEmpty,
    isSlotOccupied,
    isSlotSentinel
} from './types';

// Test the logic that can be unit tested without full component instantiation

describe('SlotCode guard functions', () => {
    describe('isSlotEmpty', () => {
        it('returns true for null', () => expect(isSlotEmpty(null)).toBe(true));
        it('returns true for empty string', () => expect(isSlotEmpty('')).toBe(true));
        it('returns true for "empty" sentinel', () =>
            expect(isSlotEmpty(SLOT_CODE_EMPTY)).toBe(true));
        it('returns false for "unreadable_code" sentinel', () =>
            expect(isSlotEmpty(SLOT_CODE_UNREADABLE)).toBe(false));
        it('returns false for a PIN string', () => expect(isSlotEmpty('1234')).toBe(false));
        it('returns false for a numeric code', () => expect(isSlotEmpty(5678)).toBe(false));
    });

    describe('isSlotOccupied', () => {
        it('returns false for null', () => expect(isSlotOccupied(null)).toBe(false));
        it('returns false for empty string', () => expect(isSlotOccupied('')).toBe(false));
        it('returns true for a PIN string', () => expect(isSlotOccupied('1234')).toBe(true));
        it('returns true for "empty" with code_length', () =>
            expect(isSlotOccupied(SLOT_CODE_EMPTY, 4)).toBe(true));
    });

    describe('isSlotSentinel', () => {
        it('recognizes every backend label', () =>
            expect(
                [
                    SLOT_CODE_EMPTY,
                    SLOT_CODE_UNREADABLE,
                    SLOT_CODE_UNANSWERED,
                    SLOT_CODE_DISABLED,
                    SLOT_CODE_CHECKING
                ].every(isSlotSentinel)
            ).toBe(true));
        it('returns false for a PIN string', () => expect(isSlotSentinel('1234')).toBe(false));
        it('returns false for a numeric code', () => expect(isSlotSentinel(5678)).toBe(false));
        it('returns false for null', () => expect(isSlotSentinel(null)).toBe(false));
    });
});
