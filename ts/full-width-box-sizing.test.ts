import fs from 'node:fs';
import path from 'node:path';

import { describe, expect, it } from 'vitest';

/**
 * A rule that fills its container must say so in border-box terms.
 *
 * `width: 100%` defaults to content-box, so any padding or border is added
 * on top and the element overruns whatever holds it. The overrun is easy to
 * miss because most containers do not clip: the element simply slides under
 * its neighbour. That is how the name field ended up behind the state pill.
 */
describe('full-width rules account for their own padding', () => {
    const tsDir = path.resolve(__dirname);
    const styleFiles = fs
        .readdirSync(tsDir)
        .filter((file) => file.endsWith('.ts') && !file.includes('.test.'));

    /** Selector plus declaration block of every rule in a stylesheet. */
    const rules = (content: string): Array<{ body: string; selector: string }> =>
        [...content.matchAll(/([.#][\w-][^{}]*)\{([^{}]*)\}/g)].map((match) => {
            return {
                body: match[2],
                selector: match[1].trim()
            };
        });

    for (const file of styleFiles) {
        it(`${file} sets box-sizing on every padded full-width rule`, () => {
            const offenders = rules(fs.readFileSync(path.join(tsDir, file), 'utf-8'))
                .filter(
                    ({ body }) =>
                        /width:\s*100%/.test(body) &&
                        /(padding|border):/.test(body) &&
                        !/box-sizing/.test(body)
                )
                .map(({ selector }) => selector);

            expect(offenders, `Missing box-sizing: border-box on ${offenders.join(', ')}`).toEqual(
                []
            );
        });
    }
});
