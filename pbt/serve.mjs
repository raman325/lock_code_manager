/** Minimal static file server for the Bombadil harness (no dependencies). */
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';

const ROOT = new URL('..', import.meta.url).pathname;
const PORT = Number(process.env.PBT_PORT ?? 8199);
const MIME = {
    '.css': 'text/css',
    '.html': 'text/html',
    '.js': 'text/javascript',
    '.json': 'application/json',
    '.mjs': 'text/javascript'
};

const server = createServer(async (req, res) => {
    const url = new URL(req.url, `http://127.0.0.1:${PORT}`);
    let path = normalize(url.pathname).replace(/^(\.\.[/\\])+/, '');
    if (path.endsWith('/')) {
        path = `${path}index.html`;
    }
    try {
        const body = await readFile(join(ROOT, path));
        res.writeHead(200, {
            'Content-Type': MIME[extname(path)] ?? 'application/octet-stream'
        });
        res.end(body);
    } catch {
        res.writeHead(404);
        res.end('not found');
    }
});

server.listen(PORT, '127.0.0.1', () => {
    console.log(`harness at http://127.0.0.1:${PORT}/pbt/harness/`);
});
