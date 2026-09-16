'use strict';

const assert = require('node:assert/strict');
const { execFile } = require('node:child_process');
const { mkdtemp, readFile, rm } = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { promisify } = require('node:util');
const test = require('node:test');

test('production Conversation follows the active pane and preserves read-away', { timeout: 90000 }, async () => {
  const { createServer } = await import('vite');
  const root = path.resolve(__dirname, '..');
  const scratch = await mkdtemp(path.join(os.tmpdir(), 'mar-feed-test-'));
  const server = await createServer({
    root,
    configFile: path.join(root, 'vite.config.ts'),
    server: { host: '127.0.0.1', port: 0, strictPort: false, open: false },
  });
  try {
    await server.listen();
    const address = server.httpServer.address();
    assert.ok(address && typeof address !== 'string');
    const report = path.join(scratch, 'report.json');
    await promisify(execFile)(require('electron'), [
      '--no-sandbox', '--disable-setuid-sandbox', '--disable-gpu',
      '--disable-dev-shm-usage', path.join(__dirname, 'feed-attachment-repro.cjs'),
    ], {
      cwd: root,
      env: {
        ...process.env,
        ELECTRON_NO_ATTACH_CONSOLE: '1',
        FEED_FIXTURE_URL: `http://127.0.0.1:${address.port}/fixtures/feed-attachment.html`,
        FEED_REPORT: report,
        FEED_PROFILE: path.join(scratch, 'profile'),
      },
      timeout: 75000,
      maxBuffer: 1024 * 1024,
    });
    const payload = JSON.parse(await readFile(report, 'utf8'));
    assert.equal(payload.ok, true);
    assert.equal(payload.results.length, 22, 'all session switches and restoration must run');
  } finally {
    await server.close();
    await rm(scratch, { recursive: true, force: true });
  }
});
