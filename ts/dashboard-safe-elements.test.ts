import fs from 'node:fs';
import path from 'node:path';

import { describe, expect, it } from 'vitest';

/**
 * A dashboard card may only use Home Assistant elements the page has.
 *
 * HA's frontend loads its components in chunks, and a Lovelace view pulls
 * in only some of them. An `ha-*` element that has not been registered is
 * not an error -- the browser treats it as an unknown element and renders
 * nothing, so the control is simply invisible. Card *editors* are exempt:
 * they run inside HA's own config dialog, which has loaded the form
 * components by the time an editor renders.
 *
 * The allowlist is empirical. Add to it only after seeing the element
 * render on a real dashboard, not because it exists in HA's source.
 */
describe('cards only use Home Assistant elements a dashboard has loaded', () => {
    const tsDir = path.resolve(__dirname);

    /**
     * Registered on a Lovelace view without any help from us. `ha-dialog`,
     * `ha-switch` and the icons come along with the entity rows and
     * more-info dialogs every dashboard already uses.
     */
    const ALWAYS_AVAILABLE = new Set([
        'ha-card',
        'ha-dialog',
        'ha-icon',
        'ha-icon-button',
        'ha-relative-time',
        'ha-svg-icon',
        'ha-switch'
    ]);

    /**
     * Available only because a card force-loads it before rendering, and
     * only if the card also copes with the load failing -- HA does not
     * promise to hand a dashboard its picker.
     */
    const LAZY_LOADED = new Map([['ha-entity-picker', 'ensureEntityPickerLoaded']]);

    const cardFiles = fs
        .readdirSync(tsDir)
        .filter(
            (file) =>
                file.endsWith('-card.ts') && !file.includes('.test.') && !file.includes('-editor')
        );

    it('finds the card files it is meant to check', () => {
        expect(cardFiles).toContain('slot-card.ts');
        expect(cardFiles).toContain('add-user-card.ts');
        expect(cardFiles).toContain('lock-codes-card.ts');
    });

    for (const file of cardFiles) {
        it(`${file} renders no unregistered ha-* element`, () => {
            const content = fs.readFileSync(path.join(tsDir, file), 'utf-8');
            const used = new Set([...content.matchAll(/<(ha-[a-z-]+)/g)].map((match) => match[1]));

            const unavailable = [...used].filter((element) => {
                if (ALWAYS_AVAILABLE.has(element)) return false;
                const loader = LAZY_LOADED.get(element);
                return !loader || !content.includes(loader);
            });

            expect(
                unavailable,
                `${unavailable.join(', ')} may not be registered on a dashboard page, ` +
                    'so it would render as nothing. Use a plain element styled to match, ' +
                    'or force-load it the way ha-entity-picker is.'
            ).toEqual([]);
        });
    }
});
