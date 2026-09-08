import { act, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { JobMetadataClient } from '../lib/jobMetadata';
import { JobMetadataStore, useJobMetadata } from '../lib/useJobMetadata';
import { context, detail, handshake, list, response, selection, summary, token, view } from './jobMetadata.fixtures';

let store: JobMetadataStore;
let request: ReturnType<typeof vi.fn<(path: string, init?: RequestInit) => Promise<Response>>>;
function pending<T>() { let resolve: (value: T) => void = () => { throw new Error('not initialized'); }; const promise = new Promise<T>(r => { resolve = r; }); return { promise, resolve }; }
async function open() { store.setTarget(context); expect(await store.readView()).toBe('applied'); }
beforeEach(() => {
  Reflect.deleteProperty(window, 'harnessIPC');
  request = vi.fn(async (path: string) => path === '/api/endpoint' ? response(handshake) : path.endsWith('/view') ? response(view()) : response(list()));
  vi.stubGlobal('fetch', request);
  store = new JobMetadataStore(new JobMetadataClient(1000));
});
afterEach(() => { store.dispose(); vi.useRealTimers(); vi.unstubAllGlobals(); });

it('three subscribers share one view and perform no automatic network activity', async () => {
  const a = renderHook(() => useJobMetadata(store)), b = renderHook(() => useJobMetadata(store)), c = renderHook(() => useJobMetadata(store));
  expect(request).not.toHaveBeenCalled();
  await act(async () => { await open(); });
  expect(request).toHaveBeenCalledTimes(2);
  await act(async () => { await store.advance(); });
  expect(request).toHaveBeenCalledTimes(3);
  expect(a.result.current).toBe(b.result.current); expect(b.result.current).toBe(c.result.current);
  a.unmount(); b.unmount(); expect(store.getSnapshot().view.kind).toBe('view'); c.unmount();
  expect(store.getSnapshot().view.kind).toBe('target');
});
it('empty partial advances once, never drains or refills', async () => {
  await open();
  request.mockResolvedValue(response({ ...list([]), page: { outcome: 'partial', revision: 20, checkpoint: 0, scanned: 51, next_cursor: token() } }));
  const before = request.mock.calls.length;
  await store.advance();
  expect(request).toHaveBeenCalledTimes(before + 1);
  expect(store.getSnapshot().streams[0]).toMatchObject({ state: 'partial', checkpoint: 0 });
  expect(store.getSnapshot().observations).toEqual([]);
});
it('overlapping advance/ticks skip instead of building a queue', async () => {
  await open();
  const wait = pending<Response>(); request.mockReturnValue(wait.promise);
  const first = store.advance();
  const before = request.mock.calls.length;
  const outcomes = await Promise.all(Array.from({ length: 1000 }, () => store.advance()));
  expect(outcomes.every(o => o === 'skipped')).toBe(true);
  expect(request).toHaveBeenCalledTimes(before);
  wait.resolve(response(list())); await first;
  expect(request).toHaveBeenCalledTimes(before);
});
it.each(['same', 'repo', 'session', 'event'])('discards pending A-B-A result for %s context changes', async change => {
  await open();
  const wait = pending<Response>(); request.mockReturnValue(wait.promise);
  const first = store.advance();
  if (change === 'event') {
    const unsubscribe = store.subscribe(() => {});
    window.dispatchEvent(new Event('harness-session-changed'));
    window.dispatchEvent(new Event('harness-session-changed'));
    unsubscribe();
  } else {
    store.setTarget(change === 'repo' ? { ...context, repo: '/B' } : change === 'session' ? { ...context, session_id: 'B' } : context);
    store.setTarget(context);
  }
  wait.resolve(response(list()));
  expect(await first).toBe('discarded'); expect(store.getSnapshot().observations).toEqual([]);
});
it('wrong host generation cannot populate state', async () => {
  await open(); request.mockResolvedValue(response({ ...list(), context: { ...context, view_generation: 'foreign' } }));
  expect(await store.advance()).toBe('failed'); expect(store.getSnapshot().observations).toEqual([]);
  expect(store.getSnapshot().error).toBe('invalid_metadata');
});
it('unmount invalidates pending result and stops the single schedule', async () => {
  const unsubscribe = store.subscribe(() => {});
  await open(); vi.useFakeTimers();
  const wait = pending<Response>(); request.mockReturnValue(wait.promise);
  store.startTicks(1000);
  await vi.advanceTimersByTimeAsync(1000);
  const before = request.mock.calls.length;
  unsubscribe(); wait.resolve(response(list()));
  await vi.advanceTimersByTimeAsync(100000);
  expect(request).toHaveBeenCalledTimes(before); expect(store.getSnapshot().observations).toEqual([]);
});
it('scheduler skips overlap, and expired streams never hot-spin', async () => {
  await open(); vi.useFakeTimers();
  const wait = pending<Response>(); request.mockReturnValue(wait.promise);
  store.startTicks(1000);
  await vi.advanceTimersByTimeAsync(1000);
  const before = request.mock.calls.length;
  wait.resolve(response({ ...list([]), page: { outcome: 'cursor_expired', revision: 99, checkpoint: 0, scanned: 0, next_cursor: null } }));
  request.mockImplementation(async () => response({ ...list([]), page: { outcome: 'cursor_expired', revision: 99, checkpoint: 0, scanned: 0, next_cursor: null } }));
  await vi.advanceTimersByTimeAsync(100000);
  expect(request.mock.calls.length - before).toBeGreaterThanOrEqual(6);
  expect(request.mock.calls.length - before).toBeLessThanOrEqual(35);
  expect(store.getSnapshot().streams.map(s => s.state)).toEqual(Array(7).fill('cursor_expired'));
  const afterBackoff = request.mock.calls.length;
  for (let i = 0; i < 20; i++) await store.advance();
  expect(request).toHaveBeenCalledTimes(afterBackoff);
  store.restartTraversal();
  request.mockResolvedValue(response(list()));
  await vi.advanceTimersByTimeAsync(1000);
  expect(request).toHaveBeenCalledTimes(afterBackoff + 1);
});
it('serialized refresh uses returned generation and never sends old-generation follow-up', async () => {
  await open();
  const wait = pending<Response>(); request.mockReturnValue(wait.promise);
  const first = store.refreshView();
  expect(await store.refreshView()).toBe('skipped'); expect(await store.advance()).toBe('skipped');
  expect(JSON.parse(String(request.mock.calls.at(-1)?.[1]?.body))).toEqual({ view_generation: 'generation-1' });
  wait.resolve(response(view('generation-3'))); expect(await first).toBe('applied');
  request.mockResolvedValue(response({ ...list(), context: { ...context, view_generation: 'generation-3' } }));
  await store.advance();
  expect(request.mock.calls.at(-1)?.[0]).toContain('view_generation=generation-3');
});
it('ambiguous refresh blocks replay until explicit GET reconciliation, without rediscovery', async () => {
  await open(); request.mockRejectedValue(new Error('connection lost after POST'));
  expect(await store.refreshView()).toBe('failed');
  const before = request.mock.calls.length;
  expect(store.getSnapshot().view).toMatchObject({ refresh: 'ambiguous' });
  expect(await store.refreshView()).toBe('skipped'); expect(await store.advance()).toBe('skipped');
  expect(request).toHaveBeenCalledTimes(before);
  request.mockResolvedValue(response(view('generation-3')));
  expect(await store.readView()).toBe('applied');
  expect(request).toHaveBeenCalledTimes(before + 1); expect(request.mock.calls.at(-1)?.[0]).toBe('/api/jobs/metadata/view');
});
it('older refresh completion cannot restore a replaced target', async () => {
  await open(); const wait = pending<Response>(); request.mockReturnValue(wait.promise);
  const first = store.refreshView(); store.setTarget({ ...context, session_id: 'B' }); store.setTarget(context);
  wait.resolve(response(view('generation-3')));
  expect(await first).toBe('discarded'); expect(store.getSnapshot().view.kind).toBe('target');
});
it('unavailable pages keep bounded last-known observations visibly stale', async () => {
  await open(); await store.advance();
  request.mockResolvedValue(response({ ...list([]), mode: 'changes', page: { outcome: 'unavailable', revision: 0, scanned: 0, checkpoint: 10000, next_cursor: null } }));
  await store.advance(); await store.advance();
  expect(store.getSnapshot().observations).toHaveLength(1); expect(store.getSnapshot().observations[0].freshness).toBe('stale');
  expect(store.getSnapshot().streams[0].checkpoint).toBe(10000);
  expect(store.getSnapshot().streams[0].state).toBe('unavailable');
});
it('explicit eight pins are separate from list, omissions never erase known observations', async () => {
  await open(); store.setPins([selection()]);
  request.mockResolvedValue(response({ version: 1, context, results: [{ selection: selection(), result: { kind: 'present', row: summary() } }] }));
  await store.refreshPins();
  expect(store.getSnapshot().pins[0].observation?.freshness).toBe('observed');
  request.mockResolvedValue(response({ version: 1, context, results: [] }));
  expect(await store.refreshPins()).toBe('failed');
  expect(store.getSnapshot().pins[0].observation?.row.lifecycle).toBe('running');
  expect(store.getSnapshot().pins[0].observation?.freshness).toBe('stale');
  expect(() => store.setPins(Array.from({ length: 9 }, (_, i) => selection(i)))).toThrow();
});
it('selection A-B-A and wrong source discard pending detail', async () => {
  await open(); store.select(selection());
  const wait = pending<Response>(); request.mockReturnValue(wait.promise);
  const first = store.readDetail(); store.select(selection(2)); store.select(selection());
  wait.resolve(response(detail())); expect(await first).toBe('discarded');
  expect(store.getSnapshot().detail).toMatchObject({ observation: null });
  expect(() => store.select({ ...selection(), source: 'cli' })).toThrow();
});
it('task/artifact cursors advance independently and pages replace instead of accumulating', async () => {
  await open(); store.select(selection());
  const d = detail();
  d.tasks.page = { outcome: 'partial', revision: 100, checkpoint: 0, scanned: 51, next_cursor: token(1) };
  d.artifacts.page = { outcome: 'partial', revision: 100, checkpoint: 0, scanned: 51, next_cursor: token(2) };
  request.mockResolvedValue(response(d)); await store.readDetail();
  const next = structuredClone(d); next.tasks.page = { ...next.tasks.page, outcome: 'partial', next_cursor: token(3) };
  request.mockResolvedValue(response(next)); await store.readDetail('tasks');
  const taskPath = new URL(request.mock.calls.at(-1)?.[0] ?? '', 'http://local');
  expect(taskPath.searchParams.get('task_cursor')).toBe(token(1)); expect(taskPath.searchParams.has('artifact_cursor')).toBe(false);
  next.artifacts.page = { ...next.artifacts.page, outcome: 'partial', next_cursor: token(4) };
  request.mockResolvedValue(response(next)); await store.readDetail('artifacts');
  const artifactPath = new URL(request.mock.calls.at(-1)?.[0] ?? '', 'http://local');
  expect(artifactPath.searchParams.get('task_cursor')).toBe(token(1)); expect(artifactPath.searchParams.get('artifact_cursor')).toBe(token(2));
  expect(store.getSnapshot().detail).toMatchObject({ observation: { tasks: { rows: d.tasks.rows } } });
});
it('new summary marks detail stale; unavailable detail cannot erase selected observation', async () => {
  await open(); store.select(selection()); request.mockResolvedValue(response(detail())); await store.readDetail();
  request.mockResolvedValue(response(list([{ ...summary(), revision: 11, lifecycle: 'done' }]))); await store.advance();
  expect(store.getSnapshot().detail).toMatchObject({ freshness: 'stale', observation: { lifecycle: 'running' } });
  const unavailable = detail(); unavailable.lifecycle = null;
  unavailable.tasks = { page: { outcome: 'unavailable', revision: 0, checkpoint: 0, scanned: 0, next_cursor: null }, rows: [] };
  request.mockResolvedValue(response(unavailable)); await store.readDetail();
  expect(store.getSnapshot().detail).toMatchObject({ freshness: 'stale', observation: { lifecycle: 'running' } });
  expect(store.getSnapshot().observations[0].row.lifecycle).toBe('done');
});
it('state snapshots cannot be mutated by a subscriber', async () => {
  await open(); await store.advance();
  expect(Object.isFrozen(store.getSnapshot().observations[0].row.selection.job_ref)).toBe(true);
});
it.each([0, 999, Infinity, 2147483648, 1e100])('rejects timer interval %s instead of allowing runtime clamping to a hot loop', interval => {
  expect(() => store.startTicks(interval)).toThrow();
});
it('same-generation refresh success is ambiguous until explicit reconciliation', async () => {
  await open(); request.mockResolvedValue(response(view()));
  expect(await store.refreshView()).toBe('failed');
  expect(store.getSnapshot().view).toMatchObject({ refresh: 'ambiguous' });
  expect(await store.refreshView()).toBe('skipped');
});
it('an unavailable stream also marks its cached detail and pin stale', async () => {
  await open(); store.select(selection()); store.setPins([selection()]);
  request.mockResolvedValue(response(detail())); await store.readDetail();
  request.mockResolvedValue(response({ version: 1, context, results: [{ selection: selection(), result: { kind: 'present', row: summary() } }] }));
  await store.refreshPins();
  request.mockResolvedValue(response({ ...list([]), page: { outcome: 'unavailable', revision: 0, scanned: 0, checkpoint: 0, next_cursor: null } }));
  await store.advance();
  expect(store.getSnapshot().detail).toMatchObject({ freshness: 'stale' });
  expect(store.getSnapshot().pins[0].observation?.freshness).toBe('stale');
});
it('selection setters reject malformed and extra fields before publishing state', async () => {
  await open();
  expect(() => store.select({ ...selection(), job_ref: { job_id: 'malformed', state_id: 'store-A' } })).toThrow();
  const cyclic = selection(); Object.assign(cyclic.job_ref, { unexpected: cyclic });
  expect(() => store.select(cyclic)).toThrow();
  expect(() => store.setPins([cyclic])).toThrow();
  expect(store.getSnapshot().detail.kind).toBe('none'); expect(store.getSnapshot().pins).toEqual([]);
});


it('primary stores recur within four advances while all 192 sibling streams progress', async () => {
  const inventory = view();
  inventory.sources.push({ source: 'cli', state_id: 'primary-cli', cross_project: false, available: true });
  for (let i = 0; i < 32; i++) inventory.sources.push({ source: 'cli', state_id: `sibling-${i}`, cross_project: true, available: true });
  const pages: string[] = [];
  request.mockImplementation(async (path: string) => {
    if (path === '/api/endpoint') return response(handshake);
    if (path.endsWith('/view')) return response(inventory);
    const query = new URL(path, 'http://fixture').searchParams;
    pages.push(`${query.get('state_id')}/${query.get('status') ?? ''}`);
    return response({ ...list([]), store: { source: query.get('source'), state_id: query.get('state_id') }, mode: query.get('mode') });
  });
  await open();
  for (let i = 0; i < 768; i++) {
    const before = pages.length;
    expect(await store.advance()).toBe('applied');
    expect(pages.length).toBe(before + 1);
  }
  for (const id of ['store-A/', 'primary-cli/']) {
    const visits = pages.flatMap((page, index) => page === id ? [index] : []);
    expect(visits.length).toBeGreaterThanOrEqual(65);
    expect(Math.max(...visits.slice(1).map((value, index) => value - visits[index]))).toBeLessThanOrEqual(4);
  }
  expect(new Set(pages.filter(page => page.startsWith('sibling-'))).size).toBe(192);
});


it('native transport backpressure skips an untouched stream until the old IPC settles', async () => {
  const inventory = view();
  inventory.sources.push({ source: 'cli', state_id: 'primary-cli', cross_project: false, available: true });
  const ipc = vi.fn<(method: string, path: string) => Promise<unknown>>(async (_method, path) => ({
    kind: 'response', status: 200, correlationId: '', text: JSON.stringify(path === '/api/endpoint' ? handshake : inventory),
  }));
  store.dispose();
  Reflect.set(window, 'harnessIPC', { endpointHeaders: true, requestJSON: ipc });
  store = new JobMetadataStore(new JobMetadataClient(20));
  await open();
  const held = pending<unknown>(); ipc.mockReturnValue(held.promise);
  vi.useFakeTimers();
  const first = store.advance();
  await vi.advanceTimersByTimeAsync(21);
  expect(await first).toBe('failed');
  const calls = ipc.mock.calls.length;
  expect(store.getSnapshot().streams[1].state).toBe('ready');
  expect(await store.advance()).toBe('skipped');
  expect(store.getSnapshot().streams[1].state).toBe('ready');
  expect(ipc.mock.calls.length).toBe(calls);
  held.resolve({ kind: 'response', status: 200, correlationId: '', text: JSON.stringify(list()) });
  await vi.advanceTimersByTimeAsync(1);
});
