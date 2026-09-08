import wire from './metadataActive.backend.json';
import { StrictMode } from 'react';
import { act, cleanup, render } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { MetadataView } from '../lib/jobMetadata';
import { JobMetadataClient } from '../lib/jobMetadata';
import { JobMetadataStore } from '../lib/useJobMetadata';
import { JobMetadataOwner, useSharedJobMetadata } from '../lib/jobMetadataContext';
import { context, handshake, list, response, view } from './jobMetadata.fixtures';

function pending<T>() {
  let resolve: (value: T) => void = () => { throw Error('not initialized'); };
  const promise = new Promise<T>(r => { resolve = r; });
  return { promise, resolve };
}
const stores: JobMetadataStore[] = [];
afterEach(() => { cleanup(); stores.forEach(s => s.dispose()); stores.length = 0; vi.useRealTimers(); vi.unstubAllGlobals(); Reflect.deleteProperty(window, 'harnessIPC'); });
function fixture() {
  Reflect.deleteProperty(window, 'harnessIPC');
  let current: MetadataView = { ...view(), availability: 'unavailable', sources: [], missing: ['sources_not_refreshed'] };
  let post: () => Promise<Response> = async () => response(view('discovered'));
  const request = vi.fn(async (path: string, init?: RequestInit) => {
    if (path === '/api/endpoint') return response(handshake);
    if (init?.method === 'POST') {
      expect(JSON.parse(String(init.body))).toEqual({ view_generation: current.context.view_generation });
      expect(new Headers(init.headers).get('X-Harness-Endpoint')).toBeTruthy();
      return post();
    }
    if (path.endsWith('/view')) return response(current);
    if (path.startsWith('/api/jobs/metadata/local')) {
      const url = new URL(path, 'http://fixture');
      return response({ ...(url.searchParams.get('lane') === 'active' ? wire.active : { ...wire.history, page: { ...wire.history.page, outcome: 'complete', checkpoint: wire.history.page.revision, next_cursor: null } }), context: { ...current.context, scope: 'session' } });
    }
    const url = new URL(path, 'http://fixture');
    return response({ ...list([]), context: { ...current.context, scope: url.searchParams.get('scope') }, mode: url.searchParams.get('mode') });
  });
  vi.stubGlobal('fetch', request);
  const store = new JobMetadataStore(new JobMetadataClient(100)); stores.push(store);
  store.setTarget(context);
  return { store, request, posts: () => request.mock.calls.filter(([, init]) => init?.method === 'POST'),
    native: () => { current = { ...current, local: { ...wire.descriptor, version: 1 } }; },
    refreshing: (refreshing: boolean) => { current = { ...current, refreshing }; },
    post: (handler: () => Promise<Response>) => { post = handler; },
    target: (session: string, generation: string) => { current = { ...current, context: { ...current.context, session_id: session, view_generation: generation } }; },
    generation: (generation: string) => { current = { ...current, context: { ...current.context, view_generation: generation } }; },
    missing: (reason: string) => { current = { ...current, missing: [reason] }; },
  };
}
it('GET stays read-only; skipped ticks retain initial admission and held POST creates no queue', async () => {
  const f = fixture(); await f.store.readView(); expect(f.posts()).toHaveLength(0);
  const held = pending<Response>(); f.post(() => held.promise);
  const capture = f.store.tick();
  for (let i = 0; i < 100; i++) expect(await f.store.tick()).toBe('skipped');
  expect(f.posts()).toHaveLength(1);
  held.resolve(response(view('discovered'))); expect(await capture).toBe('applied');
  expect(f.posts()).toHaveLength(1);
});
it('ambiguous POST is not replayed by GET, hidden ticks, or same-ID ABA; manual recovery remains available', async () => {
  const f = fixture(); await f.store.readView(); f.post(async () => { throw Error('lost reply'); });
  expect(await f.store.tick()).toBe('failed');
  for (let i = 0; i < 3; i++) { await f.store.readView(); await f.store.tick(); }
  f.store.setTarget({ ...context, session_id: 'B' }); f.store.setTarget(context);
  await f.store.tick(); await f.store.tick();
  vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
  await f.store.tick(); vi.restoreAllMocks();
  expect(f.posts()).toHaveLength(1);
  expect(f.store.getSnapshot().error).toBe('outcome_unknown');
  f.post(async () => response(view('recovered')));
  expect(await f.store.refreshView()).toBe('applied'); expect(f.posts()).toHaveLength(2);
});
it('a stale POST reply cannot adopt after ABA; a new host incarnation gets its own capture', async () => {
  const f = fixture(); await f.store.readView(); const held = pending<Response>(); f.post(() => held.promise);
  const capture = f.store.tick(); f.store.setTarget({ ...context, session_id: 'B' }); f.store.setTarget(context);
  expect(await f.store.tick()).toBe('skipped');
  held.resolve(response(view('old-result'))); expect(await capture).toBe('discarded');
  expect(f.store.getSnapshot().view.kind).toBe('target');
  await f.store.tick(); await f.store.tick(); expect(f.posts()).toHaveLength(1);
  f.generation('new-incarnation'); f.post(async () => response(view('new-result')));
  f.store.invalidate(); await f.store.tick(); await f.store.tick(); expect(f.posts()).toHaveLength(2);
});
it.each(['no_known_sources', 'source_discovery_unavailable', 'view_unavailable'])('does not bootstrap %s', async reason => {
  const f = fixture(); f.missing(reason); await f.store.tick();
  for (let i = 0; i < 4; i++) await f.store.tick();
  expect(f.posts()).toHaveLength(0);
});
it('StrictMode and hidden visibility replay do not grant another admission', async () => {
  vi.useFakeTimers(); const f = fixture(); f.post(async () => { throw Error('ambiguous'); });
  const visibility = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
  let shared: JobMetadataStore | undefined;
  function Observer() { shared = useSharedJobMetadata().store; return null; }
  const app = render(<StrictMode><JobMetadataOwner repo={context.repo} sessionId={context.session_id}><Observer /></JobMetadataOwner></StrictMode>);
  await act(async () => { await vi.advanceTimersByTimeAsync(6000); }); expect(f.posts()).toHaveLength(0);
  visibility.mockReturnValue(false);
  await act(async () => { document.dispatchEvent(new Event('visibilitychange')); await vi.advanceTimersByTimeAsync(6000); });
  expect(f.posts()).toHaveLength(1);
  app.rerender(<StrictMode><JobMetadataOwner repo={context.repo} sessionId={context.session_id}><Observer /></JobMetadataOwner></StrictMode>);
  await act(async () => { shared?.invalidate(); document.dispatchEvent(new Event('visibilitychange')); await vi.advanceTimersByTimeAsync(6000); });
  expect(f.posts()).toHaveLength(1); visibility.mockRestore();
});

it('physical IPC backpressure does not consume automatic capture admission', async () => {
  vi.useFakeTimers();
  const uncaptured = { ...view(), availability: 'unavailable', sources: [], missing: ['sources_not_refreshed'] };
  const ipc = vi.fn(async (_method: string, path: string): Promise<unknown> => ({ kind: 'response', status: 200, correlationId: '', text: JSON.stringify(path === '/api/endpoint' ? handshake : uncaptured) }));
  Reflect.set(window, 'harnessIPC', { endpointHeaders: true, requestJSON: ipc });
  const store = new JobMetadataStore(new JobMetadataClient(20)); stores.push(store); store.setTarget(context);
  await store.readView();
  const held = pending<unknown>(); ipc.mockReturnValueOnce(held.promise);
  const read = store.readView(); await vi.advanceTimersByTimeAsync(21); expect(await read).toBe('failed');
  expect(await store.tick()).toBe('skipped');
  expect(store.getSnapshot().view).toMatchObject({ refresh: 'idle' });
  expect(ipc.mock.calls.filter(([method]) => method === 'POST')).toHaveLength(0);
  held.resolve({ kind: 'response', status: 200, correlationId: '', text: JSON.stringify(uncaptured) });
  await vi.advanceTimersByTimeAsync(1);
  ipc.mockResolvedValueOnce({ kind: 'response', status: 200, correlationId: '', text: JSON.stringify(view('captured')) });
  expect(await store.tick()).toBe('applied');
  expect(ipc.mock.calls.filter(([method]) => method === 'POST')).toHaveLength(1);
});

it('pending discovery reconciles with GET and preserves native active and history progress', async () => {
  const f = fixture(); f.native(); f.post(async () => { throw Error('lost reply'); });
  await f.store.tick(); await f.store.tick(); f.refreshing(true);
  for (let i = 0; i < 12; i++) await f.store.tick();
  expect(f.posts()).toHaveLength(1);
  const paths = f.request.mock.calls.map(([path]) => path);
  expect(paths.some(path => path.includes('lane=active'))).toBe(true);
  expect(paths.some(path => path.includes('lane=history'))).toBe(true);
  expect(f.store.getSnapshot().local.observations.length).toBeGreaterThan(0);
  expect(f.store.getSnapshot().error).toBe('outcome_unknown');
  f.refreshing(false); await f.store.readView(); await f.store.tick(); expect(f.posts()).toHaveLength(1);
});

it('the real owner captures each authoritative session generation once on return', async () => {
  vi.useFakeTimers(); const f = fixture(); f.post(async () => { throw Error('ambiguous'); });
  const app = render(<JobMetadataOwner repo={context.repo} sessionId={context.session_id}><span /></JobMetadataOwner>);
  await act(async () => { await vi.advanceTimersByTimeAsync(6000); }); expect(f.posts()).toHaveLength(1);
  f.target('session-B', 'generation-B');
  app.rerender(<JobMetadataOwner repo={context.repo} sessionId="session-B"><span /></JobMetadataOwner>);
  await act(async () => { await vi.advanceTimersByTimeAsync(6000); }); expect(f.posts()).toHaveLength(2);
  f.target(context.session_id, 'generation-A-returned');
  app.rerender(<JobMetadataOwner repo={context.repo} sessionId={context.session_id}><span /></JobMetadataOwner>);
  await act(async () => { await vi.advanceTimersByTimeAsync(6000); }); expect(f.posts()).toHaveLength(3);
  app.rerender(<JobMetadataOwner repo={context.repo} sessionId={context.session_id}><span /></JobMetadataOwner>);
  await act(async () => { await vi.advanceTimersByTimeAsync(6000); }); expect(f.posts()).toHaveLength(3);
});
