const {test} = require('node:test');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const {MAX_JSON_BYTES, JSON_TIMEOUT_MS} = require('./json-request.cjs');

const routes = [
  ['GET', '/api/jobs/metadata', 64 * 1024],
  ['GET', '/api/jobs/metadata/detail', 96 * 1024],
  ['GET', '/api/jobs/metadata/local', 32 * 1024],
  ['GET', '/api/jobs/metadata/local/detail', 32 * 1024],
  ['GET', '/api/jobs/metadata/view', 16 * 1024],
  ['POST', '/api/jobs/metadata/pins', 64 * 1024],
  ['POST', '/api/jobs/metadata/view/refresh', 16 * 1024],
];
function client(serve) {
  const calls = [];
  return {calls, request(options, callback) {
    const req = new EventEmitter();
    req.destroy = () => {req.destroyed = true;};
    req.write = body => {req.body = body;};
    req.end = () => queueMicrotask(() => serve(req, callback));
    calls.push({req, options});
    return req;
  }};
}
function response(callback, headers = {}) {
  const res = Object.assign(new PassThrough(), {statusCode:200, headers});
  callback(res);
  return res;
}
function seam(http, overrides) {
  const handlers = new Map(), selected = [];
  const source = fs.readFileSync(path.join(__dirname, 'main.cjs'), 'utf8');
  const context = vm.createContext({http, Buffer, setTimeout, clearTimeout,
    backendPort:32123, harnessToken:'main-owned', startInFlight:null,
    tryRefreshBackendPortFromMarker() {throw new Error('metadata must not enter legacy retry wait');},
    require(name) {
      const mod = require(path.resolve(__dirname, name));
      if (name !== './json-request.cjs') return mod;
      return {...mod, requestJSONOnce(options, body, deps) {
        selected.push({...deps});
        return mod.requestJSONOnce(options, body, {...deps, ...overrides});
      }};
    },
    ipcMain:{handle:(name, fn) => handlers.set(name, fn), on(){}},
  });
  vm.runInContext(source.slice(source.indexOf('function authToken()'), source.indexOf('ipcMain.handle("secrets:save"')), context);
  return {context, selected, invoke(method, apiPath, body, options = {}) {
    const fn = handlers.get(`harness:${method === 'GET' ? 'getJSON' : 'postJSON'}`);
    return fn({}, apiPath, ...(method === 'GET' ? [{responseEnvelope:true, ...options}] : [body, {responseEnvelope:true, ...options}]));
  }};
}
function jsonBytes(size) {return Buffer.from('"' + 'é'.repeat((size - 2) / 2) + '"');}

for (const [method, apiPath, cap] of routes) {
  for (const declared of [true, false]) {
    test(`main rejects ${declared ? 'declared' : 'chunked'} overflow: ${method} ${apiPath}`, async () => {
      let res;
      const transport = client((req, cb) => {
        res = response(cb, declared ? {'content-length':String(cap + 1)} : {});
        if (!res.destroyed) res.end(Buffer.alloc(cap + 1, 32));
      });
      const result = await seam(transport).invoke(method, apiPath, {});
      assert.equal(result.code, 'JSON_RESPONSE_TOO_LARGE');
      assert.equal(transport.calls.length, 1);
      assert.equal(transport.calls[0].req.destroyed, true);
      assert.equal(res.destroyed, true);
      if (declared) assert.equal(res.listenerCount('data'), 0, 'declared overflow refused before body collection');
    });
  }
  test(`main accepts UTF-8 JSON exactly at cap: ${method} ${apiPath}`, async () => {
    const bytes = jsonBytes(cap);
    const transport = client((_req, cb) => {
      const res = response(cb, {'content-length':String(bytes.length)});
      res.write(bytes.subarray(0, 2)); // Split the first multibyte character.
      res.end(bytes.subarray(2));
    });
    const actual = seam(transport);
    const result = await actual.invoke(method, apiPath, {});
    assert.equal(result.kind, 'response');
    assert.equal(Buffer.byteLength(result.text), cap);
    assert.equal(JSON.parse(result.text), 'é'.repeat((cap - 2) / 2));
    assert.equal(actual.selected[0].maxResponseBytes, cap);
    assert.equal(actual.selected[0].timeoutMs, 30000);
  });
}

test('legacy transcript and bundle verification retain 64 MiB and 120 seconds', async () => {
  for (const apiPath of ['/api/sessions/history', '/api/bundles/verify']) {
    const bytes = jsonBytes(9 * 1024 * 1024);
    const transport = client((_req, cb) => response(cb).end(bytes));
    const actual = seam(transport);
    assert.equal((await actual.invoke('POST', apiPath, {offer:'a'.repeat(5 * 1024 * 1024)})).kind, 'response');
    assert.equal(actual.selected[0].maxResponseBytes ?? MAX_JSON_BYTES, 64 * 1024 * 1024);
    assert.equal(actual.selected[0].timeoutMs ?? JSON_TIMEOUT_MS, 120000);
  }
});

for (const [method, apiPath, cap] of routes) {
  test(`query pathname selects main policy and preserves identity: ${apiPath}`, async () => {
    const transport = client((_req, cb) => response(cb, {'x-correlation-id':'server-correlation'}).end('{}'));
    const actual = seam(transport);
    const queryPath = apiPath + '?scope=a%2Fb&next=/api/sessions/history';
    const result = await actual.invoke(method, queryPath, {}, {
      correlationId:'request-correlation',
      identityHeaders:{'X-Harness-Protocol':'1', 'X-Harness-Endpoint':'endpoint', 'X-Harness-Boot':'boot',
        'X-Harness-Token':'renderer-secret', 'Untrusted':'ignored'},
      maxBytes:Infinity, maxResponseBytes:Infinity, maxRequestBytes:Infinity, timeoutMs:120000,
      policy:{maxResponseBytes:Infinity, timeoutMs:120000},
    });
    assert.equal(result.correlationId, 'server-correlation');
    assert.equal(actual.selected[0].maxResponseBytes, cap);
    assert.equal(actual.selected[0].timeoutMs, 30000);
    assert.equal(transport.calls[0].options.path, queryPath);
    assert.deepEqual(transport.calls[0].options.headers, {
      'X-Harness-Protocol':'1', 'X-Harness-Endpoint':'endpoint', 'X-Harness-Boot':'boot',
      'Content-Type':'application/json', 'X-Harness-Token':'main-owned',
      'X-Correlation-Id':'request-correlation', ...(method === 'POST' ? {'Content-Length':2} : {}),
    });
  });
}

test('renderer options cannot enlarge the enforced response cap', async () => {
  const transport = client((_req, cb) => response(cb).end(Buffer.alloc(16 * 1024 + 1)));
  const result = await seam(transport).invoke('GET', '/api/jobs/metadata/view?job_id=x', undefined, {
    maxBytes:Infinity, maxResponseBytes:Infinity, timeoutMs:Infinity,
    policy:{maxResponseBytes:Infinity},
  });
  assert.equal(result.code, 'JSON_RESPONSE_TOO_LARGE');
});

for (const apiPath of ['/api/jobs/metadata/pins', '/api/jobs/metadata/view/refresh']) {
  test(`64 KiB UTF-8 request cap is independent of response cap: ${apiPath}`, async () => {
    const transport = client((_req, cb) => response(cb).end('{}'));
    const actual = seam(transport);
    const body = 'é'.repeat((64 * 1024 - 2) / 2);
    assert.equal((await actual.invoke('POST', apiPath, body)).kind, 'response');
    assert.equal(transport.calls[0].options.headers['Content-Length'], 64 * 1024);
    assert.equal(Buffer.byteLength(transport.calls[0].req.body), 64 * 1024);
    assert.equal(actual.selected[0].maxRequestBytes, 64 * 1024);
    const tooLarge = await actual.invoke('POST', apiPath, body + 'x', {maxRequestBytes:Infinity, maxBytes:Infinity});
    assert.equal(tooLarge.code, 'JSON_REQUEST_TOO_LARGE');
    assert.equal(transport.calls.length, 1, 'oversized request refused before socket creation');
  });
}

test('refresh accepts a 64 KiB request but rejects a response above 16 KiB', async () => {
  const transport = client((_req, cb) => response(cb).end(jsonBytes(16 * 1024 + 2)));
  const result = await seam(transport).invoke('POST', '/api/jobs/metadata/view/refresh', 'x'.repeat(64 * 1024 - 2));
  assert.equal(result.code, 'JSON_RESPONSE_TOO_LARGE');
  assert.match(result.message, /write may have completed/);
  assert.equal(transport.calls[0].options.headers['Content-Length'], 64 * 1024);
});

for (const apiPath of [
  '/api/jobs/metadata-extra', '/api/jobs/metadata/', '/api/jobs/metadata/detail/extra',
  '/api/jobs/%6detadata', '/api/other/../jobs/metadata', '/api/sessions/history?next=/api/jobs/metadata',
]) {
  test(`policy does not reinterpret the transmitted pathname: ${apiPath}`, async () => {
    const actual = seam(client((_req, cb) => response(cb).end('{}')));
    assert.equal((await actual.invoke('GET', apiPath)).kind, 'response');
    assert.equal(actual.selected[0].maxResponseBytes, undefined);
    assert.equal(actual.selected[0].timeoutMs, undefined);
  });
}

test('method mismatch does not acquire a metadata policy', async () => {
  for (const [method, apiPath] of [['POST', '/api/jobs/metadata'], ['GET', '/api/jobs/metadata/pins']]) {
    const actual = seam(client((_req, cb) => response(cb).end('{}')));
    assert.equal((await actual.invoke(method, apiPath, {})).kind, 'response');
    assert.equal(actual.selected[0].maxResponseBytes, undefined);
  }
});

test('invalid paths cannot leak URL parser or HTTP validation errors', async () => {
  const http = require('node:http');
  for (const apiPath of ['/api/jobs/metadata?secret=\u0000', 'http://[secret\u0100', {secret:'private'}]) {
    const result = await seam(http).invoke('POST', apiPath, {});
    assert.equal(result.kind, 'connection-error');
    assert.equal(result.code, 'JSON_REQUEST_INVALID');
    assert.doesNotMatch(result.message, /secret|private|http:|\[|\u0000/);
  }
});

for (const method of ['GET', 'POST']) {
  for (const stage of ['headers', 'body', 'endless chunks']) {
    test(`metadata ${method} deadline covers ${stage} and ignores late callbacks`, async () => {
      let res, callback, interval;
      const transport = client((_req, cb) => {
        callback = cb;
        if (stage === 'headers') return;
        res = response(cb);
        res.write('{');
        if (stage === 'endless chunks') interval = setInterval(() => res.write(' '), 2);
      });
      const actual = seam(transport, {timeoutMs:25});
      actual.context.startInFlight = new Promise(() => {});
      try {
        const started = Date.now();
        const result = await actual.invoke(method, method === 'GET' ? '/api/jobs/metadata' : '/api/jobs/metadata/view/refresh', {}, {retries:999, timeoutMs:Infinity});
        assert.equal(result.code, 'JSON_REQUEST_TIMEOUT');
        assert.equal(actual.selected[0].timeoutMs, 30000, 'actual main policy before test-only deadline injection');
        assert.ok(Date.now() - started < 1000);
        assert.equal(transport.calls.length, 1);
        assert.equal(transport.calls[0].req.destroyed, true);
        if (method === 'POST') assert.match(result.message, /write may have completed/);
        if (!res) res = response(callback);
        assert.equal(res.destroyed, true);
        res.emit('error', new Error('late private response'));
        res.emit('aborted');
        res.emit('data', Buffer.alloc(100));
        res.emit('end');
        transport.calls[0].req.emit('error', new Error('late private request'));
        await new Promise(resolve => setImmediate(resolve));
        assert.equal(transport.calls.length, 1);
      } finally {clearInterval(interval);}
    });
  }
}

for (const method of ['GET', 'POST']) {
  test(`metadata ${method} transient failure does not wait for restart or replay`, async () => {
    const transport = client(req => req.emit('error', Object.assign(new Error('private URL token'), {code:'ECONNRESET'})));
    const actual = seam(transport);
    actual.context.startInFlight = new Promise(() => {});
    const result = await actual.invoke(method, method === 'GET' ? '/api/jobs/metadata/local' : '/api/jobs/metadata/view/refresh', {});
    assert.equal(result.code, 'ECONNRESET');
    assert.doesNotMatch(result.message, /private|URL|token/);
    assert.equal(transport.calls.length, 1);
  });
}

test('huge declared metadata body is refused without receiving data', async () => {
  let res;
  const transport = client((_req, cb) => {res = response(cb, {'content-length':String(2 ** 40)});});
  const result = await seam(transport).invoke('GET', '/api/jobs/metadata');
  assert.equal(result.code, 'JSON_RESPONSE_TOO_LARGE');
  assert.equal(res.listenerCount('data'), 0);
  assert.equal(res.destroyed, true);
});


test('malformed URL and non-string policy lookup never throws', () => {
  const {jobMetadataPolicy} = require('./job-metadata-policy.cjs');
  for (const apiPath of ['http://[secret', undefined, null, {}, 3]) {
    assert.equal(jobMetadataPolicy('GET', apiPath), undefined);
  }
});

test('real loopback metadata bounds through actual main IPC', {timeout:10000}, async t => {
  const http = require('node:http');
  const received = [], sockets = new Set();
  const server = http.createServer((req, res) => {
    const call = {url:req.url, method:req.method, headers:req.headers, bytes:0};
    received.push(call);
    req.on('data', chunk => {call.bytes += chunk.length;});
    req.on('end', () => {
      const mode = req.headers['x-correlation-id'];
      const cap = Number(new URL(req.url, 'http://localhost').searchParams.get('cap'));
      if (mode === 'headers') return;
      if (mode === 'trickle') {
        res.writeHead(200, {'Content-Type':'application/json'});
        res.write('{');
        const interval = setInterval(() => res.write(' '), 5);
        res.on('close', () => clearInterval(interval));
        return;
      }
      if (mode === 'declared') {
        res.writeHead(200, {'Content-Length':String(cap + 1)});
        res.flushHeaders();
        return;
      }
      if (mode === 'chunked') {
        res.writeHead(200, {'Transfer-Encoding':'chunked'});
        res.write(Buffer.alloc(cap, 32));
        res.end('x');
        return;
      }
      const body = jsonBytes(cap);
      res.writeHead(200, {'Content-Length':body.length, 'X-Correlation-Id':'loopback-correlation'});
      res.write(body.subarray(0, 2));
      res.end(body.subarray(2));
    });
  });
  server.on('connection', socket => {sockets.add(socket); socket.on('close', () => sockets.delete(socket));});
  t.after(() => {for (const socket of sockets) socket.destroy(); return new Promise(resolve => server.close(resolve));});
  try {
    await new Promise((resolve, reject) => {server.once('error', reject); server.listen(0, '127.0.0.1', resolve);});
  } catch (error) {
    if (['EPERM', 'EACCES'].includes(error.code) && process.env.REQUIRE_LOCAL_HTTP !== '1') {
      t.skip(`Local HTTP bind unavailable (${error.code}); rerun REQUIRE_LOCAL_HTTP=1 node --test webapp/electron/job-metadata.test.cjs`);
      return;
    }
    throw error;
  }
  const actual = seam(http);
  actual.context.backendPort = server.address().port;
  for (const [method, apiPath, cap] of routes) {
    for (const mode of ['declared', 'chunked', 'exact']) {
      const count = received.length;
      const result = await actual.invoke(method, `${apiPath}?cap=${cap}`, method === 'POST' ? 'é'.repeat((64 * 1024 - 2) / 2) : undefined, {
        correlationId:mode,
        identityHeaders:{'X-Harness-Protocol':'1', 'X-Harness-Endpoint':'endpoint', 'X-Harness-Boot':'boot', 'X-Harness-Token':'forbidden'},
      });
      assert.equal(received.length, count + 1);
      if (mode === 'exact') {
        assert.equal(Buffer.byteLength(result.text), cap);
        assert.equal(result.correlationId, 'loopback-correlation');
        assert.equal(JSON.parse(result.text), 'é'.repeat((cap - 2) / 2));
      } else assert.equal(result.code, 'JSON_RESPONSE_TOO_LARGE');
      const call = received.at(-1);
      assert.equal(call.headers['x-harness-token'], 'main-owned');
      assert.equal(call.headers['x-harness-endpoint'], 'endpoint');
      assert.equal(call.headers['x-harness-boot'], 'boot');
      assert.equal(call.headers['x-harness-protocol'], '1');
      assert.equal(call.bytes, method === 'POST' ? 64 * 1024 : 0);
      assert.equal(actual.selected.at(-1).timeoutMs, 30000);
    }
  }
  const short = seam(http, {timeoutMs:80});
  short.context.backendPort = server.address().port;
  for (const mode of ['headers', 'trickle']) {
    const count = received.length;
    const result = await short.invoke('POST', '/api/jobs/metadata/view/refresh', {}, {correlationId:mode, retries:20});
    assert.equal(result.code, 'JSON_REQUEST_TIMEOUT');
    assert.match(result.message, /write may have completed/);
    assert.equal(short.selected.at(-1).timeoutMs, 30000);
    assert.equal(received.length, count + 1, 'refresh executed once');
  }
  const count = received.length;
  assert.equal((await actual.invoke('POST', '/api/jobs/metadata/view/refresh', 'é'.repeat(32 * 1024))).code, 'JSON_REQUEST_TOO_LARGE');
  assert.equal(received.length, count, 'oversized request never reaches loopback');
});

for (const [method, apiPath, cap] of routes) {
  for (const routedPath of [apiPath + ';ignored?scope=all', '/' + apiPath]) {
    test(`backend pathname aliases retain metadata bounds: ${method} ${routedPath}`, async () => {
      const transport = client((_req, cb) => response(cb).end(Buffer.alloc(cap + 1)));
      const result = await seam(transport).invoke(method, routedPath, {});
      assert.equal(result.code, 'JSON_RESPONSE_TOO_LARGE');
    });
  }
}
