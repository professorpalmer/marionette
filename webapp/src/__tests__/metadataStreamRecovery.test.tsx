import { afterEach, expect, it, vi } from 'vitest';
import wire from './metadataActive.backend.json';
import { JobMetadataClient, metadataStreamKey } from '../lib/jobMetadata';
import { JobMetadataStore } from '../lib/useJobMetadata';
import { context, handshake, list, response, summary, token, view } from './jobMetadata.fixtures';

const stores: JobMetadataStore[] = [];
afterEach(() => { stores.forEach(s => s.dispose()); stores.length = 0; vi.unstubAllGlobals(); vi.useRealTimers(); });
function fixture(native = false) {
  Reflect.deleteProperty(window, 'harnessIPC');
  const paths: URL[] = [];
  let pm: (url: URL) => unknown = url => ({ ...list([]), mode: url.searchParams.get('mode') });
  let local: (url: URL) => unknown = url => url.searchParams.get('lane') === 'active' ? wire.active : { ...wire.history, rows: [] };
  vi.stubGlobal('fetch', vi.fn(async (path: string) => {
    const url = new URL(path, 'http://test'); paths.push(url);
    if (url.pathname === '/api/endpoint') return response(handshake);
    if (url.pathname.endsWith('/view')) return response({ ...view(), ...(native ? { local: wire.descriptor } : {}) });
    return response(url.pathname.endsWith('/local') ? await local(url) : await pm(url));
  }));
  const store = new JobMetadataStore(new JobMetadataClient(1000)); stores.push(store);
  return { store, paths, pm: (fn: typeof pm) => { pm = fn; }, local: (fn: typeof local) => { local = fn; } };
}
async function open(f: ReturnType<typeof fixture>) { f.store.setTarget(context); await f.store.readView(); }
async function until(f: ReturnType<typeof fixture>, predicate: () => boolean) {
  for (let i = 0; i < 80; i++) { await f.store.advance(); if (predicate()) return; }
  throw Error('stream did not progress in 80 turns');
}
function nativeRow(f: ReturnType<typeof fixture>) { return f.store.getSnapshot().local.observations.find(o => o.row.local_ref.job_id === 'local-z-zero'); }

it('PM expiry recovers in its own slot with a fresh snapshot and independent history cursor', async () => {
  vi.useFakeTimers({ toFake: ['Date'] });
  const f = fixture(); let running = 0;
  f.pm(url => {
    if (url.searchParams.get('status') !== 'running') return { ...list([]), mode: url.searchParams.get('mode'), page: { outcome: 'partial', revision: 10000, checkpoint: 0, scanned: 0, next_cursor: token(f.paths.length) } };
    running++;
    return running === 1 ? { ...list([]), page: { outcome: 'cursor_expired', revision: 10000, checkpoint: 0, scanned: 0, next_cursor: null } } : { ...list(), mode: url.searchParams.get('mode') };
  });
  await open(f); await until(f, () => running === 1);
  vi.setSystemTime(Date.now() + 5001);
  await until(f, () => running === 2);
  const calls = f.paths.filter(p => p.searchParams.get('status') === 'running');
  expect(calls[1].searchParams.get('mode')).toBe('snapshot');
  expect(calls[1].searchParams.has('cursor')).toBe(false);
  expect(f.store.getSnapshot().streams.find(s => s.stream.status === null)?.traversal.cursor).not.toBeNull();
});

it.each(['PM', 'native'])('%s transport exceptions retain observations stale', async source => {
  const f = fixture(source === 'native');
  f.pm(url => ({ ...list(url.searchParams.get('status') === 'running' ? [summary()] : []), mode: url.searchParams.get('mode') }));
  await open(f); await until(f, () => source === 'native' ? !!nativeRow(f) : f.store.getSnapshot().observations.length > 0);
  if (source === 'native') f.local(() => { throw Error('offline'); }); else f.pm(() => { throw Error('offline'); });
  await until(f, () => source === 'native' ? f.store.getSnapshot().localActive.state === 'unavailable' : f.store.getSnapshot().streams.some(s => s.stream.status === 'running' && s.state === 'unavailable'));
  expect(source === 'native' ? nativeRow(f)?.freshness : f.store.getSnapshot().observations[0]?.freshness).toBe('stale');
});

it.each(['explicit', 'expired'])('native %s snapshot absence fences old history without inventing deletion', async restart => {
  const f = fixture(true); await open(f); await f.store.advance();
  const old = nativeRow(f); expect(old).toBeDefined();
  if (restart === 'explicit') f.store.restartTraversal();
  else {
    f.local(url => url.searchParams.get('lane') === 'active' ? { ...wire.active, rows: [], page: { outcome: 'expired', revision: 0, checkpoint: wire.active.page.checkpoint, scanned: 0, next_cursor: null } } : { ...wire.history, rows: [] });
    await until(f, () => f.store.getSnapshot().localActive.state === 'expired');
  }
  f.local(url => url.searchParams.get('lane') === 'active'
    ? { ...wire.active, rows: [] }
    : { ...wire.history, rows: [old?.row], page: { outcome: 'partial', revision: 10000, checkpoint: 0, scanned: 1, next_cursor: token(f.paths.length) } });
  await until(f, () => f.store.getSnapshot().localActive.state === 'complete');
  for (let i = 0; i < 16; i++) await f.store.advance();
  expect(nativeRow(f)?.freshness).toBe('stale');
  expect(nativeRow(f)?.row.lifecycle).toBe(old?.row.lifecycle);
  expect(f.store.getSnapshot().localActive.keys).toHaveLength(0);
});

it.each([1, 2, 9999, 10000])('PM complete snapshot absence fences history revision %s through its observed boundary', async replayRevision => {
  const f = fixture(); let empty = false;
  f.pm(url => ({ ...list(url.searchParams.get('status') === 'running' && !empty ? [summary()] : []), mode: url.searchParams.get('mode') }));
  await open(f); await until(f, () => f.store.getSnapshot().observations.length > 0);
  const running = f.store.getSnapshot().streams.find(s => s.stream.status === 'running'); if (!running) throw Error('missing stream');
  empty = true; f.store.restartTraversal(metadataStreamKey(running.stream));
  await until(f, () => f.store.getSnapshot().streams.find(s => s.stream.status === 'running')?.state === 'complete');
  f.pm(url => ({ ...list(url.searchParams.has('status') ? [] : [{ ...summary(), revision: replayRevision }]), mode: url.searchParams.get('mode') }));
  for (let i = 0; i < 16; i++) await f.store.advance();
  expect(f.store.getSnapshot().observations[0]?.freshness).toBe('stale');
  expect(f.store.getSnapshot().observations[0]?.row.lifecycle).toBe('running');
});

it.each(['PM', 'native'])('%s history transport failure preserves independently observed active progress', async source => {
  const f = fixture(source === 'native');
  f.pm(url => ({ ...list(url.searchParams.get('status') === 'running' ? [summary()] : []), mode: url.searchParams.get('mode') }));
  await open(f); await until(f, () => source === 'native' ? !!nativeRow(f) : f.store.getSnapshot().observations.length > 0);
  if (source === 'native') f.local(url => { if (url.searchParams.get('lane') === 'history') throw Error('history offline'); return wire.active; });
  else f.pm(url => { if (!url.searchParams.has('status')) throw Error('history offline'); return { ...list([]), mode: url.searchParams.get('mode') }; });
  await until(f, () => f.store.getSnapshot().error !== null);
  expect(source === 'native' ? nativeRow(f)?.freshness : f.store.getSnapshot().observations[0]?.freshness).toBe('observed');
});

it('partial native snapshot preserves newer history observations and stales only absent entry revisions', async () => {
  const f = fixture(true); await open(f); await f.store.advance();
  const old = nativeRow(f); if (!old) throw Error('missing observation');
  f.store.restartTraversal();
  let page = 0;
  f.local(url => url.searchParams.get('lane') === 'active'
    ? { ...wire.active, rows: [], page: (++page === 1 || nativeRow(f)?.row.revision === old.row.revision) ? { outcome: 'partial', revision: 1, checkpoint: 0, scanned: 0, next_cursor: token(f.paths.length) } : { outcome: 'complete', revision: 1, checkpoint: 1, scanned: 0, next_cursor: null } }
    : { ...wire.history, rows: [{ ...old.row, revision: old.row.revision + (page > 0 ? 1 : 0) }], page: { outcome: 'partial', revision: 10000, checkpoint: 0, scanned: 1, next_cursor: token(f.paths.length) } });
  await until(f, () => page === 1);
  await until(f, () => nativeRow(f)?.row.revision === old.row.revision + 1);
  expect(f.store.getSnapshot().localActive.state).toBe('partial');
  await until(f, () => f.store.getSnapshot().localActive.state === 'complete');
  expect(nativeRow(f)?.freshness).toBe('observed');
});

it('targeted PM restart preserves native and other PM observations and cursors', async () => {
  const f = fixture(true);
  f.pm(url => ({ ...list(url.searchParams.get('status') === 'running' ? [summary()] : []), mode: url.searchParams.get('mode') }));
  await open(f); await until(f, () => f.store.getSnapshot().observations.length > 0);
  const before = f.store.getSnapshot();
  const queued = before.streams.find(s => s.stream.status === 'queued'); if (!queued) throw Error('missing queued');
  f.store.restartTraversal(metadataStreamKey(queued.stream));
  expect(f.store.getSnapshot().local).toEqual(before.local);
  expect(f.store.getSnapshot().localActive).toEqual(before.localActive);
  expect(f.store.getSnapshot().observations).toEqual(before.observations);
});

it('context invalidation discards a held recovery response and overlap does not send', async () => {
  const f = fixture(true); await open(f); await f.store.advance(); f.store.restartTraversal();
  let release: (value: unknown) => void = () => {};
  // Advance to the next native active request.
  let held: Promise<unknown> | undefined;
  while (f.store.getSnapshot().advanceNumber % 8 !== 4) await f.store.advance();
  f.local(() => new Promise(resolve => { release = resolve; }));
  held = f.store.advance(); await Promise.resolve();
  const calls = f.paths.length;
  expect(await f.store.advance()).toBe('skipped'); expect(f.paths).toHaveLength(calls);
  f.store.invalidate(); release(wire.active);
  expect(await held).toBe('discarded'); expect(f.store.getSnapshot().local.observations).toHaveLength(0);
});

it('PM status removal cannot stale an equal-revision successor from another status', async () => {
  const f = fixture(); let phase = 'running';
  f.pm(url => {
    const status = url.searchParams.get('status');
    const rows = status === 'running' && phase === 'running' ? [summary()]
      : status === 'stitching' && phase !== 'running' ? [{ ...summary(), revision: 2, lifecycle: 'stitching' }] : [];
    const body = { ...list(rows), mode: url.searchParams.get('mode') };
    return status === 'running' && phase === 'removed' ? { ...body, rows: [{ selection: summary().selection, revision: 2, deleted: true }], page: { ...body.page, scanned: 1 } } : body;
  });
  await open(f); await until(f, () => f.store.getSnapshot().observations.length > 0);
  phase = 'stitching'; await until(f, () => f.store.getSnapshot().observations[0]?.row.lifecycle === 'stitching');
  phase = 'removed';
  for (let i = 0; i < 16; i++) await f.store.advance();
  expect(f.store.getSnapshot().observations[0]).toMatchObject({ freshness: 'observed', row: { lifecycle: 'stitching', revision: 2 } });
});
