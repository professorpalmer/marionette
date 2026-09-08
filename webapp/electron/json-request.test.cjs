const {test} = require('node:test');
const assert = require('node:assert/strict');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function client(serve) {
  const calls = [];
  return {calls, request(options, callback) {
    const req = new EventEmitter();
    req.destroyed = false;
    req.destroy = () => { req.destroyed = true; };
    req.write = data => { req.body = data; };
    req.end = () => queueMicrotask(() => serve(req, callback));
    calls.push({options, req});
    return req;
  }};
}
function response(callback, headers = {}, status = 200) {
  const res = Object.assign(new PassThrough(), {statusCode:status, headers});
  callback(res);
  return res;
}
function mainSeam(http, policy) {
  const source = fs.readFileSync(path.join(__dirname, 'main.cjs'), 'utf8');
  const handlers = new Map();
  const context = vm.createContext({http, Buffer, setTimeout, clearTimeout,
    backendPort:32123, harnessToken:'owner-secret', startInFlight:null,
    tryRefreshBackendPortFromMarker() { throw new Error('unexpected retry'); },
    require(name) {
      const mod = require(path.resolve(__dirname, name));
      return name === './json-request.cjs' && policy
        ? {...mod, requestJSONOnce:(options, body, deps) => mod.requestJSONOnce(options, body, {...deps, ...policy})}
        : mod;
    },
    ipcMain:{handle:(name, fn) => handlers.set(name, fn), on(){}},
  });
  vm.runInContext(source.slice(source.indexOf('function authToken()'), source.indexOf('ipcMain.handle("secrets:save"')), context);
  return {invoke:(method, body, options = {}) => handlers.get(`harness:${method === 'GET' ? 'getJSON' : 'postJSON'}`)({}, '/api/test', ...(method === 'GET' ? [options] : [body, options])), context};
}

test('actual main IPC bounds chunked JSON with production defaults', async () => {
  let res;
  const transport = client((req, cb) => {
    res = response(cb);
    const chunk = Buffer.alloc(1024 * 1024, 32);
    for (let i=0; i<65 && !req.destroyed; i++) res.write(chunk);
    if (!req.destroyed) res.end('{}');
  });
  const result = await mainSeam(transport).invoke('POST', {}, {responseEnvelope:true});
  assert.equal(result.kind, 'connection-error');
  assert.equal(result.code, 'JSON_RESPONSE_TOO_LARGE');
  assert.equal(transport.calls.length, 1);
  assert.equal(transport.calls[0].req.destroyed, true);
  assert.equal(res.destroyed, true);
});


const {requestJSONOnce, MAX_JSON_BYTES} = require('./json-request.cjs');
const options = {host:'127.0.0.1', port:1, path:'/api/private?secret=value', method:'POST', headers:{'X-Harness-Token':'secret'}};
function run(transport, body, policy = {}) {
  return requestJSONOnce(options, body, {request:transport.request, maxBytes:16, timeoutMs:1000, ...policy});
}
for (const delta of [0, 1]) {
  test(`UTF-8 request boundary ${delta ? 'plus one' : 'exact'} is measured before socket creation`, async () => {
    const transport = client((_req, cb) => response(cb).end('{}'));
    const body = 'é'.repeat(7) + (delta ? 'x' : ''); // JSON quotes count too.
    if (delta) {
      await assert.rejects(run(transport, body), {code:'JSON_REQUEST_TOO_LARGE'});
      assert.equal(transport.calls.length, 0);
    } else {
      assert.equal((await run(transport, body)).text, '{}');
      assert.equal(transport.calls[0].options.headers['Content-Length'], 16);
      assert.equal(Buffer.byteLength(transport.calls[0].req.body), 16);
    }
  });
  test(`UTF-8 response boundary ${delta ? 'plus one' : 'exact'} across codepoint splits`, async () => {
    const transport = client((_req, cb) => {
      const res = response(cb);
      const bytes = Buffer.from('"' + 'é'.repeat(7) + (delta ? 'x' : '') + '"');
      for (const byte of bytes) res.write(Buffer.from([byte]));
      res.end();
    });
    if (delta) await assert.rejects(run(transport), {code:'JSON_RESPONSE_TOO_LARGE'});
    else assert.equal(JSON.parse((await run(transport)).text), 'é'.repeat(7));
  });
}
for (const [length, text, code] of [
  ['17', '', 'JSON_RESPONSE_TOO_LARGE'],
  ['1', ' '.repeat(17), 'JSON_RESPONSE_TOO_LARGE'],
  ['4', '{}', 'JSON_RESPONSE_LENGTH'],
  ['1', '{}', 'JSON_RESPONSE_LENGTH'],
  ['garbage', '', 'JSON_RESPONSE_LENGTH'],
]) {
  test(`declared length ${length} with ${text.length} actual bytes is refused`, async () => {
    let res;
    const transport = client((_req, cb) => {
      res = response(cb, {'content-length':length});
      if (!res.destroyed) res.end(text);
    });
    await assert.rejects(run(transport), {code});
    assert.equal(transport.calls[0].req.destroyed, true);
    assert.equal(res.destroyed, true);
  });
}
test('5 MiB Offer and 9 MiB transcript remain usable through actual main', async () => {
  const body = {offer:'a'.repeat(5 * 1024 * 1024)};
  const text = JSON.stringify({history:['a'.repeat(9 * 1024 * 1024)], display:[]});
  const transport = client((_req, cb) => response(cb).end(text));
  const result = await mainSeam(transport).invoke('POST', body, {responseEnvelope:true});
  assert.equal(result.kind, 'response');
  assert.equal(result.text.length, text.length);
  assert.ok(MAX_JSON_BYTES > text.length);
});
for (const stage of ['headers', 'body', 'trickle']) {
  test(`absolute deadline stops stalled ${stage}, destroys ownership and never replays POST`, async () => {
    let res, interval;
    const transport = client((_req, cb) => {
      if (stage === 'headers') return;
      res = response(cb);
      res.write('{');
      if (stage === 'trickle') interval = setInterval(() => res.write(' '), 2);
    });
    try {
      const start = Date.now();
      const result = await mainSeam(transport, {timeoutMs:35}).invoke('POST', {}, {responseEnvelope:true, retries:5});
      assert.equal(result.kind, 'connection-error');
      assert.equal(result.code, 'JSON_REQUEST_TIMEOUT');
      assert.match(result.message, /write may have completed/);
      assert.ok(Date.now() - start < 1000);
      assert.equal(transport.calls.length, 1);
      assert.equal(transport.calls[0].req.destroyed, true);
      if (res) assert.equal(res.destroyed, true);
    } finally { clearInterval(interval); }
  });
}
test('GET deadline is not retried by legacy recovery', async () => {
  const transport = client(() => {});
  const result = await mainSeam(transport, {timeoutMs:10}).invoke('GET', undefined, {responseEnvelope:true});
  assert.equal(result.code, 'JSON_REQUEST_TIMEOUT');
  assert.equal(transport.calls.length, 1);
});
test('serialization failure and synchronous request failure never leak input', async () => {
  const circular = {secret:'credential'}; circular.self = circular;
  const transport = client(() => { throw new Error('not reached'); });
  await assert.rejects(run(transport, circular), e => e.code === 'JSON_REQUEST_INVALID' && !/credential|secret|private/.test(e.message));
  assert.equal(transport.calls.length, 0);
  await assert.rejects(requestJSONOnce(options, {}, {request() { throw new Error('secret private credential'); }}),
    e => e.code === 'JSON_REQUEST_INVALID' && !/credential|secret|private/.test(e.message));
});
test('deadline includes synchronous serialization time and opens no socket afterward', async () => {
  const transport = client(() => {});
  const body = {toJSON() { const until = Date.now() + 20; while (Date.now() < until) {} return {}; }};
  await assert.rejects(run(transport, body, {timeoutMs:5}), {code:'JSON_REQUEST_TIMEOUT'});
  assert.equal(transport.calls.length, 0);
});
test('transport error text is sanitized and stays distinct from HTTP errors', async () => {
  const transport = client(req => req.emit('error', Object.assign(new Error('secret URL response body'), {code:'ECONNREFUSED'})));
  const result = await mainSeam(transport).invoke('POST', {}, {responseEnvelope:true});
  assert.equal(result.kind, 'connection-error');
  assert.equal(result.code, 'ECONNREFUSED');
  assert.equal(result.status, undefined);
  assert.doesNotMatch(result.message, /secret|URL|response body/);
});
test('HTTP 4xx shared contract and malformed JSON stay with the existing parser', async () => {
  const transport = client((_req, cb) => response(cb, {'x-correlation-id':'corr'}, 409).end('{"ok":false,"code":"outcome_unknown"}'));
  const seam = mainSeam(transport);
  const result = await seam.invoke('POST', {}, {responseEnvelope:true});
  assert.equal(result.kind, 'response');
  assert.equal(result.status, 409);
  assert.equal(result.correlationId, 'corr');
  await assert.rejects(seam.invoke('POST', {}), e => e.status === 409 && e.code === 'outcome_unknown');
  const malformed = mainSeam(client((_req, cb) => response(cb).end('broken')));
  await assert.rejects(malformed.invoke('GET'), {code:'INVALID_JSON_RESPONSE', status:200});
});
test('normal completion clears the deadline and ignores late errors', async () => {
  let res;
  const transport = client((_req, cb) => {res = response(cb, {'content-length':'2'}); res.end('{}');});
  assert.equal((await run(transport, undefined, {timeoutMs:15})).text, '{}');
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.equal(transport.calls[0].req.destroyed, false);
  res.emit('error', new Error('late secret'));
  transport.calls[0].req.emit('error', new Error('late secret'));
});

test('real loopback HTTP through main IPC: boundaries, stalls, no write replay', {timeout:10000}, async t => {
  const http = require('node:http');
  const writes = new Map();
  const sockets = new Set();
  const server = http.createServer((req, res) => {
    const mode = req.headers['x-correlation-id'];
    writes.set(mode, (writes.get(mode) || 0) + 1);
    req.resume();
    if (mode === 'headers') return;
    if (mode === 'body' || mode === 'trickle') {
      res.writeHead(200, {'Content-Type':'application/json'});
      res.write('{');
      if (mode === 'trickle') {
        const interval = setInterval(() => res.write(' '), 20);
        res.once('close', () => clearInterval(interval));
      }
      return;
    }
    if (mode === 'declared-large') { res.writeHead(200, {'Content-Length':'17'}); res.flushHeaders(); return; }
    if (mode === 'declared-short-body') { res.writeHead(200, {'Content-Length':'10'}); res.end('{}'); return; }
    if (mode === 'over') { res.write(' '.repeat(16)); res.end('x'); return; }
    if (mode === 'exact') { res.end('"' + 'x'.repeat(14) + '"'); return; }
    if (mode === 'bad') { res.end('not JSON'); return; }
    if (mode === 'http-error') { res.writeHead(409, {'X-Correlation-Id':'server-corr'}); res.end('{"ok":false,"code":"outcome_unknown"}'); return; }
    res.end('{"ok":true}');
  });
  server.on('connection', socket => {sockets.add(socket); socket.once('close', () => sockets.delete(socket));});
  t.after(() => {for (const socket of sockets) socket.destroy(); return new Promise(resolve => server.close(resolve));});
  try {
    await new Promise((resolve, reject) => {server.once('error', reject); server.listen(0, '127.0.0.1', resolve);});
  } catch (error) {
    if (['EPERM', 'EACCES'].includes(error.code) && process.env.REQUIRE_LOCAL_HTTP !== '1') {
      t.skip(`Local HTTP bind unavailable (${error.code}); rerun REQUIRE_LOCAL_HTTP=1 node --test electron/json-request.test.cjs`);
      return;
    }
    throw error;
  }
  const seam = mainSeam(http, {maxBytes:16, timeoutMs:120});
  seam.context.backendPort = server.address().port;
  const invoke = mode => seam.invoke('POST', {}, {responseEnvelope:true, correlationId:mode, retries:5});
  for (const mode of ['headers', 'body', 'trickle', 'declared-short-body']) {
    const result = await invoke(mode);
    assert.equal(result.kind, 'connection-error', mode);
    assert.ok(['JSON_REQUEST_TIMEOUT', 'ECONNRESET'].includes(result.code), mode);
    assert.equal(writes.get(mode), 1);
  }
  for (const mode of ['declared-large', 'over']) assert.equal((await invoke(mode)).code, 'JSON_RESPONSE_TOO_LARGE');
  assert.equal((await invoke('exact')).text.length, 16);
  assert.equal(JSON.parse((await invoke('ok')).text).ok, true);
  await assert.rejects(seam.invoke('POST', {}, {correlationId:'bad'}), {code:'INVALID_JSON_RESPONSE'});
  const larger = mainSeam(http, {maxBytes:64, timeoutMs:120});
  larger.context.backendPort = server.address().port;
  const result = await larger.invoke('POST', {}, {responseEnvelope:true, correlationId:'http-error'});
  assert.equal(result.status, 409);
  assert.equal(result.correlationId, 'server-corr');
  assert.deepEqual(JSON.parse(result.text), {ok:false, code:'outcome_unknown'});
});

test('late headers after timeout are destroyed without a second settlement', async () => {
  let callback;
  const transport = client((_req, cb) => {callback = cb;});
  await assert.rejects(run(transport, {}, {timeoutMs:5}), {code:'JSON_REQUEST_TIMEOUT'});
  const res = response(callback);
  assert.equal(res.destroyed, true);
  assert.equal(transport.calls[0].req.destroyed, true);
});
test('synchronous write failure destroys the request and warns about the outcome', async () => {
  const req = new EventEmitter();
  req.destroy = () => {req.destroyed = true;};
  req.write = () => {throw new Error('private body');};
  await assert.rejects(run({request:() => req}, {}), e =>
    e.code === 'JSON_CONNECTION_ERROR' && /write may have completed/.test(e.message) && !/private/.test(e.message));
  assert.equal(req.destroyed, true);
});
