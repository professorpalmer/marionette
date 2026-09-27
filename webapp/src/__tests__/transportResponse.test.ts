import { afterEach, describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { resolve } from 'node:path';
import { runInNewContext } from 'node:vm';
import { EventEmitter } from 'node:events';
import { PassThrough } from 'node:stream';
import { getJSON, getJSONSoft, postJSON } from '../lib/transport';
import { getCorrelationId, setCorrelationId } from '../lib/correlationId';

const mainSource = readFileSync(resolve('electron/main.cjs'), 'utf8');
const preloadSource = readFileSync(resolve('electron/preload.cjs'), 'utf8');
const nodeRequire = createRequire(resolve('electron/main.cjs'));

const handshake = JSON.stringify({ok:true,protocol_version:1,endpoint_id:"test-endpoint",boot_id:"test-boot",capabilities:["endpoint_fence_v1"]});

function desktop(status: number, text: string, connectionCode = '') {
  const handlers = new Map();
  const sentHeaders: Record<string, string>[] = [];
  const context = {
    Buffer, setTimeout, harnessToken: '', backendPort: 1, startInFlight: null,
    require: nodeRequire, tryRefreshBackendPortFromMarker() {},
    ipcMain: { on() {}, handle: (name: string, handler: unknown) => handlers.set(name, handler) },
    http: { request(options: {path: string; headers: Record<string, string>}, callback: (res: PassThrough) => void) {
      if (options.path !== '/api/endpoint') sentHeaders.push(options.headers);
      const req = new EventEmitter();
      return Object.assign(req, { write() {}, destroy() {}, end() {
        queueMicrotask(() => {
          if (options.path === '/api/endpoint') {
            const res = Object.assign(new PassThrough(), {statusCode:200,headers:{}});
            callback(res); res.end(handshake); return;
          }
          if (connectionCode) {
            req.emit('error', Object.assign(new Error('connection lost'), {code: connectionCode}));
            return;
          }
          const res = Object.assign(new PassThrough(), {statusCode: status, headers: {'x-correlation-id':'server-correlation'}});
          callback(res);
          res.end(text);
        });
      } });
    } },
  };
  runInNewContext(mainSource.slice(mainSource.indexOf('function authToken()'), mainSource.indexOf('ipcMain.handle("secrets:save"')), context);
  runInNewContext(preloadSource, {
    process: {env: {}}, require: () => ({
      contextBridge: {exposeInMainWorld(name: string, value: unknown) { Object.defineProperty(window, name, {value, configurable: true}); }},
      ipcRenderer: { async invoke(name: string, ...args: unknown[]) {
        // Invoke resolves plain data through both serialization boundaries.
        return structuredClone(await handlers.get(name)(null, ...structuredClone(args)));
      } },
    }),
  });
  return sentHeaders;
}

function browser(status: number, text: string) {
  vi.stubGlobal('fetch', vi.fn(async path => path === '/api/endpoint' ? new Response(handshake, {status:200}) : new Response(status === 204 || status === 205 ? null : text, {
    status, headers: {'X-Correlation-Id':'server-correlation'},
  })));
}
afterEach(() => {
  Reflect.deleteProperty(window, 'harnessIPC');
  Reflect.deleteProperty(window, '__HARNESS_INSPECT__');
  vi.unstubAllGlobals();
});

for (const mode of ['browser', 'desktop']) {
  describe(mode, () => {
    const setup = mode === 'browser' ? browser : desktop;
    for (const method of ['GET', 'POST']) {
      it(`${method} preserves HTTP failure metadata and backend detail`, async () => {
        setup(409, JSON.stringify({error:'busy', code:'lease_exhausted', max_concurrent:2, status:999, message:'wrong'}));
        const call = method === 'GET' ? getJSON('/test') : postJSON('/test', {});
        await expect(call).rejects.toMatchObject({message:'busy', status:409, code:'lease_exhausted', max_concurrent:2,
          body: {status:999}, correlationId:'server-correlation'});
        expect(getCorrelationId()).toBe('server-correlation');
      });
    }
    it('rejects malformed and unexpectedly empty success bodies', async () => {
      for (const body of ['not-json', '']) {
        setup(200, body);
        await expect(postJSON('/test', {})).rejects.toMatchObject({status:200, code:'INVALID_JSON_RESPONSE', body});
      }
    });
    it('rejects HTML errors with status and raw body', async () => {
      setup(502, '<h1>unavailable</h1>');
      await expect(getJSON('/test')).rejects.toMatchObject({status:502, body:'<h1>unavailable</h1>', message:'/test -> 502'});
    });
    it('accepts explicit no-content statuses and JSON null', async () => {
      for (const status of [204,205]) {
        setup(status, '');
        expect(await postJSON('/test', {})).toBeNull();
      }
      setup(200, 'null');
      expect(await getJSON('/test')).toBeNull();
    });
    it('retains soft error normalization and malformed fallback', async () => {
      setup(400, '{"error":"not available"}');
      expect(await getJSONSoft('/test')).toEqual({ok:false, error:'not available'});
      setup(400, 'broken');
      expect(await getJSONSoft('/test')).toEqual({ok:false, error:'/test -> 400'});
      setup(200, 'broken');
      expect(await getJSONSoft('/test')).toEqual({});
    });
    it('does not mistake a successful application-level error body for HTTP failure', async () => {
      setup(200, '{"ok":false,"error":"domain result"}');
      expect(await getJSON('/test')).toEqual({ok:false, error:'domain result'});
    });
  });
}
it('passes renderer correlation to main and preserves connection error code without replay', async () => {
  setCorrelationId('client-correlation');
  const headers = desktop(200, '', 'ECONNRESET');
  await expect(postJSON('/write', {})).rejects.toMatchObject({message: expect.stringContaining('Backend JSON connection failed'), code:'ECONNRESET'});
  expect(headers).toHaveLength(1);
  expect(headers[0]['X-Correlation-Id']).toBe('client-correlation');
});
