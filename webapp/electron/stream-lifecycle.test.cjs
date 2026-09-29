const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');

function loadStreamHandler() {
  const source = fs.readFileSync(path.join(__dirname, 'main.cjs'), 'utf8');
  const listeners = new Map();
  const destroyed = [];
  const main = {
    Buffer, setTimeout, WeakMap, Set, backendPort: 1, logMain() {},
    authToken: () => 'tok', endpointRequestHeaders: (h) => ({...h}),
    ipcMain: {on: (name, fn) => listeners.set(name, fn), once() {}, removeListener() {}},
    wireStreamResponse: require('./stream-bridge.cjs').wireStreamResponse,
    sanitizedStreamConnError: require('./stream-bridge.cjs').sanitizedStreamConnError,
    http: {get(options, callback) {
      const req = new EventEmitter();
      req.destroy = () => destroyed.push(options.path);
      queueMicrotask(() => callback(Object.assign(new PassThrough(), {statusCode: 200, headers: {}})));
      return req;
    }},
  };
  vm.runInContext(source.slice(source.indexOf('ipcMain.on("harness:stream"'), source.indexOf('// ---- native bridges')), vm.createContext(main));
  return {start: listeners.get('harness:stream'), destroyed};
}

test('a stream ends when its page navigates (reload), not before', async () => {
  const {start, destroyed} = loadStreamHandler();
  const sender = Object.assign(new EventEmitter(), {isDestroyed: () => false, send() {}});
  start({sender}, 'stream-a-1', '/api/chat/events?watch=1', {});
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(destroyed, []);
  sender.emit('did-navigate');
  assert.deepEqual(destroyed, ['/api/chat/events?watch=1']);
});

test('stream ids never repeat across page loads', () => {
  const ids = [];
  for (let load = 0; load < 2; load++) {
    const exposed = {};
    vm.runInNewContext(fs.readFileSync(path.join(__dirname, 'preload.cjs'), 'utf8'), {process: {env: {}}, Date, Math, require: () => ({
      contextBridge: {exposeInMainWorld: (name, value) => { exposed[name] = value; }},
      ipcRenderer: {invoke() {}, on() {}, removeListener() {}, send: (name, id) => { if (name === 'harness:stream') ids.push(id); }},
    })});
    exposed.harnessIPC.stream('/x', () => {}, () => {}, () => {}, {});
  }
  assert.equal(ids.length, 2);
  assert.notEqual(ids[0], ids[1]);
});
