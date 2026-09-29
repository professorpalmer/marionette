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
