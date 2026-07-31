/**
 * Orchestrates a Bombadil run: start the static server, explore, clean up.
 * Env knobs: BOMBADIL_TIME_LIMIT (default 60s), BOMBADIL_HEADLESS=1,
 * PBT_PORT (default 8199).
 * Exit code follows bombadil: 0 = clean, 2 = property violation.
 */
import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';

const PORT = Number(process.env.PBT_PORT ?? 8199);
const TIME_LIMIT = process.env.BOMBADIL_TIME_LIMIT ?? '60s';
const HEADLESS = process.env.BOMBADIL_HEADLESS === '1' || process.env.CI === 'true';

const server = spawn('node', ['pbt/serve.mjs'], { stdio: 'inherit' });
await sleep(500);

const args = [
    'browser',
    'test',
    `http://127.0.0.1:${PORT}/pbt/harness/`,
    'pbt/spec.ts',
    `--time-limit=${TIME_LIMIT}`,
    '--exit-on-violation',
    '--output-path=pbt-output',
    '--output-path-overwrite'
];
if (HEADLESS) {
    args.push('--headless');
}
if (process.env.CI === 'true') {
    args.push('--no-sandbox');
}

const bombadil = spawn('node_modules/.bin/bombadil', args, { stdio: 'inherit' });
const exitCode = await new Promise((resolve) => {
    bombadil.on('exit', (code) => resolve(code ?? 1));
});
server.kill();
process.exit(exitCode);
