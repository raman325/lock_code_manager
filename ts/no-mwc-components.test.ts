import fs from 'node:fs';
import path from 'node:path';

import { describe, expect, it } from 'vitest';

/**
 * Guard against using deprecated Material Web Components (mwc-*) elements.
 *
 * Home Assistant's frontend has migrated from mwc-* to ha-* components.
 * Using mwc-* elements causes them to render as invisible unknown elements
 * because HA no longer registers those custom elements.
 */
describe('no deprecated mwc-* components in source', () => {
    const tsDir = path.resolve(__dirname);
    const sourceFiles = fs
        .readdirSync(tsDir)
        .filter((f) => f.endsWith('.ts') && !f.includes('.test.'));

    for (const file of sourceFiles) {
        it(`${file} does not use mwc-* elements`, () => {
            const content = fs.readFileSync(path.join(tsDir, file), 'utf-8');
            const mwcMatches = content.match(/<mwc-\w+|mwc-\w+[.>{\s]/g);
            expect(
                mwcMatches,
                `Found deprecated mwc-* usage: ${mwcMatches?.join(', ')}`
            ).toBeNull();
        });
    }
});

/**
 * Guard against Home Assistant elements the frontend has replaced (#1018).
 *
 * Editors are covered too: `ha-textfield` vanished from the card editors on
 * HA 2026.5 and left them with no inputs. The list is HA's own, from
 * "Frontend component updates 2026.4" (developers.home-assistant.io blog,
 * 2026-03-25): each element maps to its successor.
 */
describe('no replaced Home Assistant elements in source', () => {
    const REPLACED = new Map([
        ['ha-textfield', 'ha-input'],
        ['ha-outlined-text-field', 'ha-input'],
        ['search-input', 'ha-input-search'],
        ['search-input-outlined', 'ha-input-search'],
        ['ha-multi-textfield', 'ha-input-multi'],
        ['copy-textfield', 'ha-input-copy']
    ]);
    const tsDir = path.resolve(__dirname);
    const sourceFiles = fs
        .readdirSync(tsDir)
        .filter((f) => f.endsWith('.ts') && !f.includes('.test.'));

    it('checks the card editors', () => {
        expect(sourceFiles).toContain('slot-card-editor.ts');
        expect(sourceFiles).toContain('lock-codes-card-editor.ts');
    });

    for (const file of sourceFiles) {
        it(`${file} uses no replaced element`, () => {
            const content = fs.readFileSync(path.join(tsDir, file), 'utf-8');
            // As a tag, and by name in a string (whenDefined, get, createElement).
            const used = [
                ...content.matchAll(/<([a-z]+(?:-[a-z]+)+)[\s>/]/g),
                ...content.matchAll(/['"`]([a-z]+(?:-[a-z]+)+)['"`]/g)
            ]
                .map((match) => match[1])
                .filter((element) => REPLACED.has(element))
                .map((element) => `${element} (use ${REPLACED.get(element)})`)
                .filter((entry, index, all) => all.indexOf(entry) === index);
            expect(used).toEqual([]);
        });
    }
});
