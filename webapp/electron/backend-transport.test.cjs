const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs');
const vm = require('node:vm');
const { PassThrough } = require('node:stream');
const { EventEmitter } = require('node:events');

function seam(port, client = http) {
  const source = fs.readFileSync(__dirname + '/main.cjs', 'utf8');
  const context = vm.createContext({ http: client, Buffer, setTimeout, backendPort: port,
    ipcMain: { on() {}, handle() {} },
    harnessToken: '', startInFlight: null, tryRefreshBackendPortFromMarker() {},
    require: (name) => require(require('node:path').resolve(__dirname, name)),
  });
  vm.runInContext(source.slice(source.indexOf('function authToken()'), source.indexOf('ipcMain.on("harness:rendererError"')), context);
  return context;
}
async function fixture(t, handler) {
  if (process.env.TRANSPORT_STREAM_FIXTURE === '1') {
    return seam(0, { request(options, callback) {
      const req = new EventEmitter();
      req.write = () => {};
      req.destroy = () => {};
      req.end = () => queueMicrotask(() => {
        const res = new PassThrough();
        res.statusCode = 200;
        res.headers = {};
        res.writeHead = (status, headers = {}) => {
          res.statusCode = status;
          res.headers = Object.fromEntries(Object.entries(headers).map(([k,v]) => [k.toLowerCase(),v]));
        };
        const incoming = { url: options.path, socket: { destroy() { req.emit('error', Object.assign(new Error('socket hang up'), {code:'ECONNRESET'})); } } };
        callback(res);
        handler(incoming, res);
      });
      return req;
    } });
  }
  const server = http.createServer(handler);
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  t.after(() => new Promise(resolve => server.close(resolve)));
  return seam(server.address().port);
}
test('non-2xx rejects with body, status and correlation', async t => {
  const ctx = await fixture(t, (_, res) => {
    res.writeHead(409, { 'X-Correlation-Id': 'corr-test' });
    res.end('{"error":"busy","code":"lease_exhausted"}');
  });
  await assert.rejects(ctx.backendRequest('POST', '/api/write', {}), e =>
    e.status === 409 && e.body.code === 'lease_exhausted' && e.correlationId === 'corr-test');
});
test('malformed success and empty 200 reject; 204 is explicit null', async t => {
  const ctx = await fixture(t, (req, res) => {
    if (req.url === '/empty') res.writeHead(204);
    res.end(req.url === '/bad' ? 'broken' : '');
  });
  await assert.rejects(ctx.backendRequest('GET', '/bad'), /Invalid JSON/);
  await assert.rejects(ctx.backendRequest('GET', '/blank'), /Invalid JSON/);
  assert.equal(await ctx.backendRequest('GET', '/empty'), null);
});
test('possibly executed POST is not replayed', async t => {
  let writes = 0;
  const ctx = await fixture(t, (req, res) => {
    writes++;
    if (writes === 1) req.socket.destroy(); else res.end('{"ok":true}');
  });
  await assert.rejects(ctx.backendRequest('POST', '/write', {}, {delayMs: 1}));
  assert.equal(writes, 1);
});
test('GET retries a reset and recovers', async t => {
  let reads = 0;
  const ctx = await fixture(t, (req, res) => {
    reads++;
    if (reads === 1) req.socket.destroy(); else res.end('{"ok":true}');
  });
  assert.equal((await ctx.backendRequest('GET', '/read', null, {delayMs: 1})).ok, true);
  assert.equal(reads, 2);
});

for (const code of ['ECONNREFUSED', 'ECONNRESET', 'EPIPE', 'ETIMEDOUT']) {
  test(`POST does not replay ${code}; GET refreshes and retries`, async () => {
    let attempts = 0;
    let refreshes = 0;
    const ctx = seam(0, { request(_options, callback) {
      const req = new EventEmitter();
      req.write = () => {};
      req.destroy = () => {};
      req.end = () => queueMicrotask(() => {
        attempts++;
        if (attempts === 1) req.emit('error', Object.assign(new Error(code), {code}));
        else {
          const res = Object.assign(new PassThrough(), {statusCode:200, headers:{}});
          callback(res);
          res.end('{}');
        }
      });
      return req;
    } });
    ctx.tryRefreshBackendPortFromMarker = () => refreshes++;
    await assert.rejects(ctx.backendRequest('POST', '/write', {}, {delayMs:1}), {code});
    assert.equal(attempts, 1);
    assert.equal(refreshes, 0);
    attempts = 0;
    await ctx.backendRequest('GET', '/read', undefined, {delayMs:1});
    assert.equal(attempts, 2);
    assert.equal(refreshes, 1);
  });
}
test('response abort rejects instead of hanging or resolving partial data', async () => {
  const ctx = seam(0, { request(_options, callback) {
    const req = new EventEmitter();
    req.write = () => {};
    req.destroy = () => {};
    req.end = () => queueMicrotask(() => {
      const res = Object.assign(new PassThrough(), {statusCode:200, headers:{}});
      callback(res);
      res.write('{"partial":');
      res.emit('aborted');
    });
    return req;
  } });
  await assert.rejects(ctx.backendRequest('POST', '/write', {}), {code:'ECONNRESET'});
});
test('JSON unicode survives chunks splitting a UTF-8 codepoint', async t => {
  const ctx = await fixture(t, (_req, res) => {
    const bytes = Buffer.from('{"text":"caf\u00e9"}');
    const split = bytes.indexOf(0xc3) + 1;
    res.write(bytes.subarray(0, split));
    res.end(bytes.subarray(split));
  });
  assert.equal((await ctx.backendRequest('GET', '/read')).text, 'caf\u00e9');
});
