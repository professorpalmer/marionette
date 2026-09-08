// @vitest-environment node
import { createServer } from 'node:http';
import type { IncomingMessage, ServerResponse } from 'node:http';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { JobMetadataClient } from '../lib/jobMetadata';
import { JobMetadataStore } from '../lib/useJobMetadata';
import { context, detail, handshake, initial, list, selection, stream, token, view } from './jobMetadata.fixtures';

const realFetch = globalThis.fetch;
const calls: { path: string; method: string; endpoint: string | string[] | undefined; boot: string | string[] | undefined; body: string }[] = [];
let handler: (req: IncomingMessage, res: ServerResponse) => void;
let base = '';
function json(res: ServerResponse, value: unknown, status = 200) { res.writeHead(status, { 'Content-Type': 'application/json' }); res.end(JSON.stringify(value)); }
const server = createServer((req, res) => {
  res.setHeader('Connection', 'close');
  const call = { path: req.url ?? '', method: req.method ?? '', endpoint: req.headers['x-harness-endpoint'], boot: req.headers['x-harness-boot'], body: '' };
  calls.push(call);
  req.on('data', (chunk: Buffer) => { call.body += chunk.toString(); });
  req.on('end', () => req.url === '/api/endpoint' ? json(res, handshake) : handler(req, res));
});
beforeEach(() => {
  calls.length = 0;
  vi.stubGlobal('window', Object.assign(new EventTarget(), { location: { origin: base } }));
  vi.stubGlobal('fetch', (path: string, init?: RequestInit) => realFetch(new URL(path, base), init));
  handler = (_req, res) => json(res, view());
});
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); });
async function connected(timeout = 1000) { const client = new JobMetadataClient(timeout); await client.connect(); return client; }

describe.skipIf(process.env.METADATA_TEST_NO_LISTEN === '1')('real HTTP', () => {
beforeEach(async () => {
  await new Promise<void>((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('No HTTP fixture address');
  base = `http://127.0.0.1:${address.port}`;
  vi.stubGlobal('window', Object.assign(new EventTarget(), { location: { origin: base } }));
});
afterEach(async () => { server.closeAllConnections(); await new Promise<void>(resolve => server.close(() => resolve())); });
it('real HTTP pins endpoint/boot on every metadata route and no v1 fallback', async () => {
  const client = await connected();
  await client.view(context);
  handler = (_req, res) => json(res, list()); await client.list(context, stream, initial);
  handler = (_req, res) => json(res, detail()); await client.detail(context, selection(), { task_cursor: null, artifact_cursor: null });
  handler = (_req, res) => json(res, { version: 1, context, results: [{ selection: selection(), result: { kind: 'unavailable', reason: 'missing' } }] }); await client.pins(context, [selection()]);
  expect(calls).toHaveLength(5);
  for (const call of calls.slice(1)) { expect(call.endpoint).toBe('endpoint-1'); expect(call.boot).toBe('boot-1'); expect(call.path).toMatch(/^\/api\/jobs\/metadata/); }
  expect(JSON.parse(calls[4].body).selections).toHaveLength(1);
});
it.each(['endpoint_mismatch', 'boot_mismatch'])('real HTTP %s invalidates without rediscovery or POST replay', async code => {
  const client = await connected();
  handler = (_req, res) => json(res, { code }, 409);
  await expect(client.refresh(context)).rejects.toMatchObject({ code: 'endpoint_changed' });
  await expect(client.view(context)).rejects.toMatchObject({ code: 'endpoint_changed' });
  expect(calls).toHaveLength(2); expect(calls[1].method).toBe('POST');
});
it('real HTTP malformed or tampered cursor response stays an explicit request failure', async () => {
  const client = await connected();
  handler = (_req, res) => json(res, { code: 'invalid_read_request' }, 400);
  await expect(client.list(context, stream, { ...initial, cursor: token(999) })).rejects.toMatchObject({ code: 'invalid_request' });
  expect(calls).toHaveLength(2);
});
it('real HTTP declared size rejects before consuming an unbounded body', async () => {
  const client = await connected();
  handler = (_req, res) => { res.writeHead(200, { 'Content-Length': '99999999' }); res.flushHeaders(); };
  await expect(client.view(context)).rejects.toMatchObject({ code: 'bounds_exceeded' });
  expect(calls).toHaveLength(2);
});
it('real HTTP counts UTF-8 bytes, including a split multibyte stream', async () => {
  const client = await connected();
  handler = (_req, res) => {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    const bytes = Buffer.from(JSON.stringify({ text: '界'.repeat(30000) }));
    res.write(bytes.subarray(0, 1001)); res.end(bytes.subarray(1001));
  };
  await expect(client.list(context, stream, initial)).rejects.toMatchObject({ code: 'bounds_exceeded' });
});
it('real HTTP body deadline aborts a nonterminating response and never retries', async () => {
  const client = await connected(100);
  handler = (_req, res) => { res.writeHead(200); res.write('{'); };
  await expect(client.view(context)).rejects.toMatchObject({ code: 'outcome_unknown' });
  expect(calls).toHaveLength(2);
});
it('real HTTP ambiguous POST blocks store refresh until explicit view GET', async () => {
  const client = await connected(100), store = new JobMetadataStore(client);
  store.setTarget(context); expect(await store.readView()).toBe('applied');
  handler = (_req, res) => { res.writeHead(200); res.write('{'); };
  expect(await store.refreshView()).toBe('failed');
  const count = calls.length;
  expect(await store.refreshView()).toBe('skipped'); expect(await store.advance()).toBe('skipped'); expect(calls).toHaveLength(count);
  handler = (_req, res) => json(res, view('generation-3'));
  await store.readView(); expect(calls).toHaveLength(count + 1); expect(calls.at(-1)?.method).toBe('GET'); store.dispose();
});
it('request dispatch captures mutable caller context before the response', async () => {
  const client = await connected();
  let deliver: () => void = () => { throw new Error('not dispatched'); };
  let started: () => void = () => {};
  const ready = new Promise<void>(resolve => { started = resolve; });
  handler = (_req, res) => { deliver = () => json(res, list()); started(); };
  const mutable = { ...context };
  const work = client.list(mutable, stream, initial); await ready;
  mutable.session_id = 'other'; deliver();
  expect((await work).context.session_id).toBe('session-A');
});
});
it('native IPC validates unknown envelopes and byte caps immediately after return', async () => {
  const requestJSON = vi.fn(async () => ({ kind: 'response', status: 200, text: JSON.stringify(handshake), correlationId: '' }));
  vi.stubGlobal('window', { location: { origin: 'file://' }, harnessIPC: { endpointHeaders: true, requestJSON } });
  const client = await connected();
  requestJSON.mockResolvedValue({ kind: 'response', status: 200, text: '界'.repeat(6000), correlationId: '' });
  await expect(client.view(context)).rejects.toMatchObject({ code: 'bounds_exceeded' });
  expect(requestJSON.mock.calls).toHaveLength(2);
  expect(calls).toHaveLength(0);
});
it('native IPC timeout retains one unsettled slot across 1000 explicit attempts', async () => {
  let finish: (value: unknown) => void = () => {};
  const requestJSON = vi.fn<(...args: unknown[]) => Promise<unknown>>().mockResolvedValue({ kind: 'response', status: 200, text: JSON.stringify(handshake), correlationId: '' });
  vi.stubGlobal('window', { location: { origin: 'file://' }, harnessIPC: { endpointHeaders: true, requestJSON } });
  const client = await connected();
  requestJSON.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  vi.useFakeTimers();
  const rejected = expect(client.view(context)).rejects.toMatchObject({ code: 'outcome_unknown' });
  await vi.advanceTimersByTimeAsync(1001); await rejected;
  for (let i = 0; i < 1000; i++) await expect(client.view(context)).rejects.toMatchObject({ code: 'busy' });
  expect(requestJSON).toHaveBeenCalledTimes(2);
  finish({ kind: 'response', status: 200, text: JSON.stringify(view()), correlationId: '' });
  await vi.advanceTimersByTimeAsync(1);
  expect(vi.getTimerCount()).toBe(0);
});
it('native IPC bridge replacement invalidates a pending response', async () => {
  let finish: (value: unknown) => void = () => {};
  const requestJSON = vi.fn<(...args: unknown[]) => Promise<unknown>>().mockResolvedValue({ kind: 'response', status: 200, text: JSON.stringify(handshake), correlationId: '' });
  const desktop = { location: { origin: 'file://' }, harnessIPC: { endpointHeaders: true, requestJSON } };
  vi.stubGlobal('window', desktop);
  const client = await connected();
  requestJSON.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  const work = client.view(context);
  await Promise.resolve();
  desktop.harnessIPC = { endpointHeaders: true, requestJSON: vi.fn().mockResolvedValue(null) };
  finish({ kind: 'response', status: 200, text: JSON.stringify(view()), correlationId: '' });
  await expect(work).rejects.toMatchObject({ code: 'endpoint_changed' });
});

it('browser Response reader enforces declared size before reading', async () => {
  const cancel = vi.fn();
  const request = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(handshake))).mockResolvedValueOnce(
    new Response(new ReadableStream({ start(controller) { controller.enqueue(new TextEncoder().encode('{')); }, cancel }), { headers: { 'Content-Length': '9999999' } }),
  );
  vi.stubGlobal('fetch', request);
  const client = await connected();
  await expect(client.view(context)).rejects.toMatchObject({ code: 'bounds_exceeded' });
  expect(request).toHaveBeenCalledTimes(2);
});
it('browser Response stream counts bytes rather than JS string length', async () => {
  const bytes = new TextEncoder().encode(JSON.stringify({ text: '界'.repeat(30000) }));
  const cancel = vi.fn();
  const body = new ReadableStream({ start(c) { c.enqueue(bytes.subarray(0, 1001)); c.enqueue(bytes.subarray(1001)); }, cancel });
  const request = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(handshake))).mockResolvedValueOnce(new Response(body));
  vi.stubGlobal('fetch', request);
  const client = await connected();
  await expect(client.list(context, stream, initial)).rejects.toMatchObject({ code: 'bounds_exceeded' });
  expect(cancel).toHaveBeenCalled();
});
it('browser Response body timeout cancels and clears its deadline', async () => {
  const cancel = vi.fn();
  const request = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(handshake))).mockResolvedValueOnce(
    new Response(new ReadableStream({ start(c) { c.enqueue(new TextEncoder().encode('{')); }, cancel })),
  );
  vi.stubGlobal('fetch', request);
  const client = await connected(100);
  vi.useFakeTimers();
  const rejected = expect(client.view(context)).rejects.toMatchObject({ code: 'outcome_unknown' });
  await vi.advanceTimersByTimeAsync(101); await rejected;
  expect(cancel).toHaveBeenCalled(); expect(vi.getTimerCount()).toBe(0); expect(request).toHaveBeenCalledTimes(2);
});
it('browser Response rejects invalid UTF-8 and malformed JSON without empty fallbacks', async () => {
  const request = vi.fn().mockResolvedValueOnce(new Response(JSON.stringify(handshake))).mockResolvedValueOnce(new Response(new Uint8Array([0xff, 0xff]))).mockResolvedValueOnce(new Response('{'));
  vi.stubGlobal('fetch', request);
  const client = await connected();
  await expect(client.view(context)).rejects.toMatchObject({ code: 'outcome_unknown' });
  await expect(client.view(context)).rejects.toMatchObject({ code: 'invalid_metadata' });
});
it('native JSON envelope malformed shape cannot leak into domain state', async () => {
  const requestJSON = vi.fn<(...args: unknown[]) => Promise<unknown>>().mockResolvedValueOnce({ kind: 'response', status: 200, text: JSON.stringify(handshake), correlationId: '' }).mockResolvedValue({ kind: 'response', status: 200, text: 12 });
  vi.stubGlobal('window', { location: { origin: 'file://' }, harnessIPC: { endpointHeaders: true, requestJSON } });
  const client = await connected();
  await expect(client.view(context)).rejects.toMatchObject({ code: 'invalid_metadata' });
});
it('uncancellable native requests cannot accumulate across disposed client lifetimes', async () => {
  let finish: (value: unknown) => void = () => {};
  const requestJSON = vi.fn<(...args: unknown[]) => Promise<unknown>>().mockResolvedValueOnce({ kind: 'response', status: 200, text: JSON.stringify(handshake), correlationId: '' });
  vi.stubGlobal('window', { location: { origin: 'file://' }, harnessIPC: { endpointHeaders: true, requestJSON } });
  const first = await connected(100);
  requestJSON.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  vi.useFakeTimers();
  const work = expect(first.view(context)).rejects.toMatchObject({ code: 'outcome_unknown' });
  await vi.advanceTimersByTimeAsync(101); await work; first.close();
  for (let i = 0; i < 1000; i++) {
    const replacement = new JobMetadataClient();
    await expect(replacement.connect()).rejects.toMatchObject({ code: 'busy' });
    replacement.close();
  }
  expect(requestJSON).toHaveBeenCalledTimes(2);
  finish({ kind: 'response', status: 200, text: JSON.stringify(view()), correlationId: '' });
  await vi.advanceTimersByTimeAsync(1);
  expect(vi.getTimerCount()).toBe(0);
});
