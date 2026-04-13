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
