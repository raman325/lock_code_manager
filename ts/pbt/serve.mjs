/** Minimal static file server for the Bombadil harness (no dependencies). */
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { extname, join, normalize } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = fileURLToPath(new URL('../..', import.meta.url));
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
    if (url.pathname === '/favicon.ico') {
        res.writeHead(204);
        res.end();
        return;
    }
    let path = normalize(url.pathname).replace(/^(\.\.[/\\])+/, '');
    if (path.endsWith('/')) {
        path = `${path}index.html`;
    }
    // The cards navigate via history.pushState to virtual Home Assistant
    // routes (e.g. /config/integrations/...), and a browser reload then
    // requests that route from this server. Serve the harness page for any
    // extensionless path — mirroring the real frontend's single-page-app
    // routing — while keeping honest 404s for missing assets.
    if (!extname(path)) {
        path = '/ts/pbt/harness/index.html';
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
    console.log(`harness at http://127.0.0.1:${PORT}/ts/pbt/harness/`);
});
