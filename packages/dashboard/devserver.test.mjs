// The Vite dev server (pnpm dev, :5190) proxies /api to the Hasami server, so whatever it lets a foreign page
// read, that page can read from the API too (desk photo, real-camera frame, run records). It must send no
// Access-Control-Allow-Origin to a foreign origin, must refuse a foreign Host (DNS rebinding), must listen on
// 127.0.0.1 only, and must be a Vite with the CVE-2025-24010 (CORS / Host) and fs.deny fixes (>= 5.4.19).
// Real Vite, real sockets: the server is started from vite.config.ts on a free port and closed afterwards.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import net from 'node:net';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { createServer, resolveConfig, version } from 'vite';

const here = path.dirname(fileURLToPath(import.meta.url));

function freePort() {
  return new Promise((resolve, reject) => {
    const s = net.createServer();
    s.once('error', reject);
    s.listen(0, '127.0.0.1', () => {
      const { port } = s.address();
      s.close(() => resolve(port));
    });
  });
}

function get(port, urlPath, headers) {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: 'localhost', port, path: urlPath, method: 'GET', headers }, (res) => {
      res.resume();
      res.on('end', () => resolve({ status: res.statusCode, headers: res.headers }));
    });
    req.on('error', reject);
    req.setTimeout(20_000, () => req.destroy(new Error(`GET ${urlPath} timed out`)));
    req.end();
  });
}

function atLeast(v, want) {
  const a = v.split('.').map(Number);
  const b = want.split('.').map(Number);
  for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] > b[i];
  return true;
}

test('vite is a release with the dev-server CORS, Host and fs.deny fixes', () => {
  assert.ok(atLeast(version, '5.4.19'), `vite ${version} predates 5.4.19`);
});

test('config: dev and preview servers bind 127.0.0.1 with CORS off', async () => {
  const dev = await resolveConfig({ root: here, configFile: path.join(here, 'vite.config.ts'), logLevel: 'silent' }, 'serve');
  assert.equal(dev.server.host, '127.0.0.1');
  assert.equal(dev.server.cors, false);
  assert.equal(dev.preview.host, '127.0.0.1');
  assert.equal(dev.preview.cors, false);
});

test('a foreign origin gets no Access-Control-Allow-Origin, on source files and on proxied /api', async () => {
  const port = await freePort();
  const server = await createServer({
    root: here,
    configFile: path.join(here, 'vite.config.ts'),
    logLevel: 'silent',
    server: { port, strictPort: true },
  });
  await server.listen();
  try {
    for (const p of ['/', '/src/api.ts', '/api/status']) {
      const r = await get(port, p, { Origin: 'https://evil.example', Host: `localhost:${port}` });
      assert.equal(r.headers['access-control-allow-origin'], undefined, `${p} answered ACAO ${r.headers['access-control-allow-origin']}`);
    }
    // the page itself still loads for the dev server's own host
    const own = await get(port, '/', { Host: `localhost:${port}` });
    assert.equal(own.status, 200);
    // a rebound DNS name pointing at 127.0.0.1 is refused
    const rebound = await get(port, '/src/api.ts', { Host: `evil.example:${port}` });
    assert.equal(rebound.status, 403);
  } finally {
    await server.close();
  }
});
