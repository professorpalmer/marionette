const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

test('a failed browser registration does not fail backend start', async () => {
  const source = fs.readFileSync(path.join(__dirname, 'main.cjs'), 'utf8');
  const logs = [];
  const ctx = vm.createContext({
    startInFlight: null,
    _startBackendOnce: async () => {},
    connectDesktopBrowser: async () => { throw new Error('controller 500'); },
    logMain: (m) => logs.push(m),
  });
  vm.runInContext(source.slice(source.indexOf('function startBackend()'), source.indexOf('async function _startBackendOnce()')), ctx);
  await ctx.startBackend();
  assert.match(logs.join('\n'), /desktop browser registration failed: controller 500/);
});

test('a failed backend start still rejects', async () => {
  const source = fs.readFileSync(path.join(__dirname, 'main.cjs'), 'utf8');
  const ctx = vm.createContext({
    startInFlight: null,
    _startBackendOnce: async () => { throw new Error('spawn failed'); },
    connectDesktopBrowser: async () => {},
    logMain() {},
  });
  vm.runInContext(source.slice(source.indexOf('function startBackend()'), source.indexOf('async function _startBackendOnce()')), ctx);
  await assert.rejects(ctx.startBackend(), /spawn failed/);
});

// macOS keeps the app (and the backend it spawned) alive after the last
// window closes; a Dock reopen re-ensures the backend. Probing the marker then
// treated our own child as foreign: a successful probe set backend=null /
// backendOwned=false (Cmd+Q orphaned the tree, a crash was never respawned) and
// a probe that timed out on a busy backend threw BACKEND_NOT_OWNED (error box,
// no window).
function startBackendOnceContext(overrides) {
  const source = fs.readFileSync(path.join(__dirname, 'main.cjs'), 'utf8');
  const probes = [];
  const child = { pid: 4242, exitCode: null, signalCode: null, killed: false };
  const ctx = vm.createContext({
    process: { env: {}, platform: 'darwin' },
    console,
    backend: child,
    backendOwned: true,
    backendPort: 51234,
    quitting: false,
    logMain() {},
    ...require('./backend-lifecycle.cjs'),
    ...require('./backend-identity.cjs'),
    requestAuthenticatedBackendStop: async () => { probes.push('stop'); },
    consumeIntentionalRestartSignal() {},
    readPmHarnessStateFile() { probes.push('marker'); return JSON.stringify({ port: 51234, pid: 4242 }); },
    waitForAuthenticatedBackend: async () => { probes.push('probe'); throw Object.assign(new Error('timeout'), { code: 'ETIMEDOUT' }); },
    currentBackendIdentity: () => ({}),
    resolveRepoRoot: () => '/repo',
    app: { getVersion: () => '0.0.0' },
    ...overrides,
  });
  vm.runInContext(source.slice(source.indexOf('async function _startBackendOnce()'), source.indexOf('function authToken()')), ctx);
  return { ctx, probes, child };
}

test('re-ensuring keeps a live backend this app spawned (no probe, same port, still owned)', async () => {
  const { ctx, probes, child } = startBackendOnceContext({});
  await ctx._startBackendOnce().catch((err) => probes.push(`threw ${err.code}`));
  assert.deepEqual(probes, []);
  assert.equal(ctx.backend, child);
  assert.equal(ctx.backendOwned, true);
  assert.equal(ctx.backendPort, 51234);
});

test('a dead or foreign backend is not kept', () => {
  const { shouldKeepOwnedLiveBackend } = require('./backend-lifecycle.cjs');
  const live = { exitCode: null, signalCode: null, killed: false };
  assert.equal(shouldKeepOwnedLiveBackend({ child: live, backendOwned: true, quitting: false }), true);
  assert.equal(shouldKeepOwnedLiveBackend({ child: live, backendOwned: false, quitting: false }), false);
  assert.equal(shouldKeepOwnedLiveBackend({ child: live, backendOwned: true, quitting: true }), false);
  assert.equal(shouldKeepOwnedLiveBackend({ child: { ...live, exitCode: 1 }, backendOwned: true, quitting: false }), false);
  assert.equal(shouldKeepOwnedLiveBackend({ child: null, backendOwned: true, quitting: false }), false);
});
