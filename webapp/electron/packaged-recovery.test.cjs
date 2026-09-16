"use strict";
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const vm = require('node:vm');
const { EventEmitter } = require('node:events');
const bootstrap = require('./bootstrap.cjs');

test('startup awaits selected checkout before parity, backend spawn and renderer resolution', async t => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'packaged-startup-'));
  t.after(() => fs.rmSync(home, { recursive: true, force: true }));
  const source = fs.readFileSync(path.join(__dirname, 'main.cjs'), 'utf8');
  const region = (a, b) => source.slice(source.indexOf(a), source.indexOf(b, source.indexOf(a)));
  const events = [];
  const selected = bootstrap.selectPackagedCheckout({ home, env: {} });
  const env = {};
  const noop = () => {};
  let ready;
  const context = {
    require, fs, path, os: { homedir: () => home }, __dirname, console,
    process: { env, platform: process.platform, argv: [], stdout: { write: noop }, stderr: { write: noop } },
    setImmediate, setTimeout, isPackaged: true, isDev: false, gotSingleInstanceLock: true,
    selfDevEnabled: () => false, selfDevCheckout: () => null,
    selectPackagedCheckout: () => bootstrap.selectPackagedCheckout({ home, env }),
    isInstallComplete: () => false,
    runBootstrap: async root => {
      assert.equal(root, selected); events.push('build-start');
      await new Promise(resolve => setImmediate(resolve));
      // An event changing the environment cannot redirect later consumers.
      env.MARIONETTE_CHECKOUT = path.join(home, 'other');
      const dist = path.join(root, 'webapp/dist'); fs.mkdirSync(dist, { recursive: true });
      fs.writeFileSync(path.join(dist, 'index.html'), 'selected renderer');
      events.push('built');
    },
    ensurePuppetmasterParity: async root => { assert.equal(root, selected); events.push('parity'); },
    createBootstrapWindow: () => ({ close: noop }), waitForBootstrapWindow: async () => {},
    bootstrapWin: null, sendBootstrapProgress: noop,
    loginShellEnv: () => ({}), registerMarionetteProtocol: noop, configureBrowserSession: noop,
    setupRequestInterception: noop, flushWikiConnectQueue: noop,
    app: { getVersion: () => '0.9.422', whenReady: () => ({ then: cb => { ready = cb(); } }) },
    dialog: { showErrorBox: message => assert.fail(message) },
    startInFlight: null, backend: null, backendOwned: false, backendPort: 0,
    connectDesktopBrowser: async () => { events.push('browser'); },
    currentBackendIdentity: ({ repoRoot }) => { assert.equal(repoRoot, selected); return {}; },
    readPmHarnessStateFile: () => null, decideBackendReuse: () => ({ action: 'spawn' }),
    readLiveUpdateMarker: () => null, freePort: async () => 12345,
    resolveHarnessStateDir: () => path.join(home, 'state'), buildPuppetmasterBackendEnv: x => x,
    isInspectMode: () => false, harnessToken: 'fixture', harnessAppRunId: 'fixture',
    secretVault: { injectEnv: noop }, waitForBackend: async () => {}, refreshAllowedLoopbackAliases: noop,
    markerPath: () => path.join(home, 'marker'), buildBackendMarkerPayload: x => x,
    _dbg2: noop,
    spawn: (python, args, options) => {
      assert.equal(options.cwd, selected);
      assert.equal(python, bootstrap.venvPython(selected));
      assert.equal(options.env.MARIONETTE_APP_ROOT, selected);
      assert.deepEqual(events, ['build-start', 'built', 'parity']); events.push('spawn');
      const child = new EventEmitter(); child.stdout = new EventEmitter(); child.stderr = new EventEmitter(); return child;
    },
  };
  vm.createContext(context);
  vm.runInContext(region('let selectedRepoRoot = null;', '// Live-UI mode:') +
    region('function resolveDistIndex()', 'function freePort()') +
    region('async function ensurePackagedCheckout()', '// --- window bounds') +
    region('function startBackend()', '// ---- transport seam'), context);
  context.createWindow = () => {
    assert.equal(vm.runInContext('resolveDistIndex()', context), path.join(selected, 'webapp/dist/index.html'));
    events.push('renderer');
  };
  vm.runInContext(region('app.whenReady().then(async () => {', '  // Re-open: ensure a healthy backend') + '\n});', context);
  await ready;
  assert.deepEqual(events, ['build-start', 'built', 'parity', 'spawn', 'browser', 'renderer']);
});

test('managed production updates install the shell without source fetch, stash, pull or relaunch', async () => {
  const handlers = {}, roots = [];
  let installed = 0;
  require('./update-bridge.cjs').registerUpdateBridge(
    { handle: (name, handler) => { handlers[name] = handler; } },
    { isPackaged: true, getVersion: () => '0.9.422' }, {}, {
      allowSourceUpdates: false, getRepoRoot: () => '/selected/release',
      readCheckoutVersion: root => { roots.push(root); return '0.9.422'; },
      checkSourceUpdate: () => assert.fail('must not fetch a source branch'),
      applySourceUpdate: () => assert.fail('must not mutate the pinned checkout'),
      relaunch: () => assert.fail('must wait for installer'),
      packagedUpdater: { enabled: true, isDownloaded: () => false,
        check: async () => ({ available: true, latest: '0.9.423' }),
        downloadAndInstall: async () => { installed++; return { ok: true }; } },
    });
  assert.equal((await handlers['updates:check']()).available, true);
  const result = await handlers['updates:apply']({ sender: { isDestroyed: () => true } });
  assert.equal(result.ok, true); assert.equal(result.packagedInstallPending, true);
  assert.equal(installed, 1); assert.ok(roots.every(root => root === '/selected/release'));
});

test('self-dev toggle persists the actual root across a fresh process', t => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'self-dev-root-'));
  t.after(() => fs.rmSync(home, { recursive: true, force: true }));
  const source = fs.readFileSync(path.join(__dirname, 'main.cjs'), 'utf8');
  const resolver = source.slice(source.indexOf('let selectedRepoRoot = null;'), source.indexOf('// Live React needs'));
  const launch = () => {
    const context = vm.createContext({ fs, path, os: { homedir: () => home }, __dirname,
      process: { env: {}, platform: process.platform }, isPackaged: true,
      selectPackagedCheckout: options => bootstrap.selectPackagedCheckout({ ...options, home, env: {} }),
    });
    vm.runInContext(resolver, context); return code => vm.runInContext(code, context);
  };
  const first = launch(), root = first('resolveRepoRoot()');
  assert.equal(first('setSelfDevEnabled(true)'), true);
  assert.equal(launch()('resolveRepoRoot()'), root);
  const settings = path.join(home, '.pmharness/self-dev.json');
  fs.writeFileSync(settings, JSON.stringify({ enabled: true }));
  assert.equal(launch()('resolveRepoRoot()'), path.join(home, '.marionette/marionette'));
});
