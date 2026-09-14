import { act, cleanup, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import MetadataJobs from '../components/MetadataJobs';
import { JobMetadataOwner } from '../lib/jobMetadataContext';
import { metadataSelectionKey, parseMetadataPins } from '../lib/jobMetadata';
import type { MetadataSelection } from '../lib/jobMetadata';
import type { ExpertHeader } from '../lib/expertMetadata';
import { parseExpertHeader } from '../lib/expertMetadata';
import { sessionWorkerUsage } from '../lib/sessionWorkerUsage';
import { expertHeaderModel } from '../lib/expertRoutingFacts';
import backend from './expertCurrent.backend.json';
import { expertMetadataFixture, expertSummary } from './metadataExpert.fixtures';
import { selection as legacy } from './jobMetadata.fixtures';

let fixture: Awaited<ReturnType<typeof expertMetadataFixture>> | undefined;
afterEach(() => { cleanup(); fixture?.dispose(); fixture = undefined; vi.restoreAllMocks(); vi.useRealTimers(); localStorage.clear(); });
function selection(n = 1): MetadataSelection {
  const s = legacy(n); return { ...s, job_ref: { ...s.job_ref, version: 2, incarnation: '12345678-1234-4234-8234-123456789abc' } };
}
function header(patch: Partial<ExpertHeader> = {}): ExpertHeader {
  const parsed = parseExpertHeader({ ...backend.economics.header, model: 'gpt-6-astra', model_provenance: 'task_assignment', quality: 'ok' });
  if (!parsed) throw Error('Missing producer header');
  return { ...parsed, ...patch };
}
async function setup(rows = [expertSummary(selection(), 'Automatic worker')]) {
  const f = fixture = await expertMetadataFixture(rows);
  const original = f.request.getMockImplementation()!;
  let currentHeader = header(); let failed = false;
  const batches: number[] = [];
  f.request.mockImplementation(async (method, path, body) => {
    if (!path.endsWith('/pins')) return original(method, path, body);
    if (failed) throw Error('Temporary header failure');
    if (!body || typeof body !== 'object' || !('selections' in body) || !Array.isArray(body.selections)) throw Error('Missing selections');
    batches.push(body.selections.length);
    return { version: 1, context: f.context(), results: body.selections.map(selected => {
      const row = rows.find(row => metadataSelectionKey(row.selection) === metadataSelectionKey(selected));
      if (!row) throw Error('Unknown header selection');
      return { selection: row.selection, result: { kind: 'present', row: { ...row, header: currentHeader } } };
    }) };
  });
  return { ...f, batches, setHeader(value: ExpertHeader) { currentHeader = value; }, fail(value: boolean) { failed = value; } };
}
async function ticks(f: Awaited<ReturnType<typeof setup>>, count = 64) {
  for (let i = 0; i < count; i++) await act(async () => { await f.store.ownerTick(); });
}

describe('automatic current headers and session worker usage', () => {
  it('keeps successful session totals available between scheduled two-second owner polls', async () => {
    const f = await setup();
    vi.useFakeTimers();
    render(<JobMetadataOwner repo={selection().repo} sessionId={selection().session_id}><MetadataJobs /></JobMetadataOwner>);
    let hydrated = false;
    for (let i = 0; i < 60; i++) {
      await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
      const meter = screen.getByLabelText('Session worker usage');
      if (meter.textContent?.includes('120 tokens')) hydrated = true;
      if (hydrated) expect(meter).toHaveTextContent('120 tokens');
    }
    expect(hydrated).toBe(true);
  });
  it('hydrates collapsed counts and model in normal owner UI without any manual action', async () => {
    const f = await setup();
    vi.useFakeTimers();
    render(<JobMetadataOwner repo={selection().repo} sessionId={selection().session_id}><MetadataJobs /></JobMetadataOwner>);
    for (let i = 0; i < 20; i++) await act(async () => { await vi.advanceTimersByTimeAsync(2000); });
    expect(screen.getByTitle('Model: gpt-6-astra')).toBeVisible();
    expect(screen.getByRole('button', { name: 'Automatic worker · running' })).toHaveTextContent('0/1');
    expect(f.selected).toHaveBeenCalled();
    expect(f.selected.mock.calls.length).toBeLessThanOrEqual(2);
    expect(f.batches.length).toBeGreaterThan(0);
    expect(Math.max(...f.batches)).toBeLessThanOrEqual(8);
  });
  it('recovers positive current session totals after unavailable headers with identical list rows', async () => {
    const f = await setup(); let now = Date.now(); vi.spyOn(Date, 'now').mockImplementation(() => now);
    render(<f.Provider><MetadataJobs /></f.Provider>);
    f.fail(true); await ticks(f);
    const rows = f.store.getSnapshot().observations;
    expect(screen.getByLabelText('Session worker usage')).toHaveTextContent('partial coverage');
    f.fail(false); now += 120001; await ticks(f);
    expect(f.store.getSnapshot().observations).toEqual(rows);
    expect(sessionWorkerUsage(f.store.getSnapshot())).toMatchObject({ kind: 'complete', jobs: 1, workers: 1, tokens: 120, cost: 0 });
    expect(screen.getByLabelText('Session worker usage')).toHaveTextContent('120 tokens');
    expect(screen.getByLabelText('Session worker usage')).toHaveTextContent('Native, pilot and legacy unowned usage excluded');
    expect(f.selected).toHaveBeenCalled();
    expect(f.selected.mock.calls.length).toBeLessThanOrEqual(16);
  });
  it('retries partial economics and replaces positive costs and counts on TTL even with unchanged rows', async () => {
    const f = await setup(); let now = Date.now(); vi.spyOn(Date, 'now').mockImplementation(() => now);
    f.setHeader(header({ workers_complete: false })); await ticks(f);
    expect(sessionWorkerUsage(f.store.getSnapshot()).kind).toBe('partial');
    const live = header();
    f.setHeader(header({ completed_workers: 1, cost: { ...live.cost, selected_usd: 1.5, measured_cost_usd: 1.25, estimated_cost_usd: .25, basis: 'mixed' },
      savings: { ...live.savings!, routing_usd: .1, cache_usd: .2, compaction_usd: null } }));
    now += 120001; await ticks(f);
    expect(sessionWorkerUsage(f.store.getSnapshot())).toMatchObject({ kind: 'complete', cost: 1.5, measured: 1.25, estimated: .25, routing: .1, cache: .2, compaction: null });
    render(<f.Provider><MetadataJobs /></f.Provider>);
    expect(screen.getByRole('button', { name: 'Automatic worker · running' })).toHaveTextContent('1/1');
    const meter = within(screen.getByLabelText('Session worker usage'));
    expect(meter.getByText('Measured $1.25')).toBeVisible();
    expect(meter.queryByText(/Estimated compaction savings/)).toBeNull();
  });
  it('requires every source stream and matching row header revision, filters other sessions and preserves source collisions', async () => {
    const a = selection(), b: MetadataSelection = { ...a, source: 'cli', job_ref: { ...a.job_ref, state_id: 'store-B' } };
    const other = { ...expertSummary(selection(2), 'Other session'), ownership: { origin: 'marionette', session_id: 'other', project_id: null } };
    const f = await setup([expertSummary(a, 'Harness'), expertSummary(b, 'CLI'), other]); await ticks(f, 128);
    const state = f.store.getSnapshot();
    expect(sessionWorkerUsage(state)).toMatchObject({ kind: 'complete', jobs: 2, tokens: 240 });
    expect(sessionWorkerUsage({ ...state, streams: state.streams.slice(1) }).kind).toBe('partial');
    const key = metadataSelectionKey(a), cached = state.headers[key];
    expect(sessionWorkerUsage({ ...state, headers: { ...state.headers, [key]: { ...cached, observation: { ...cached.observation, row: { ...cached.observation.row, revision: cached.observation.row.revision + 1 } } } } }).kind).toBe('partial');
    expect(sessionWorkerUsage({ ...state, displayLimited: true }).kind).toBe('partial');
    expect(sessionWorkerUsage(state, Date.now() + 60000).kind).toBe('partial');
    await act(async () => f.store.invalidate());
    expect(sessionWorkerUsage(f.store.getSnapshot()).kind).toBe('partial');
  });
  it('preserves unknown fields rather than manufacturing zero and permits zero only for a complete empty known scope', async () => {
    const row = { ...expertSummary(selection(), 'Other session'), ownership: { origin: 'marionette', session_id: 'other', project_id: null } };
    const f = await setup([row]); await ticks(f);
    expect(sessionWorkerUsage(f.store.getSnapshot())).toMatchObject({ kind: 'complete', jobs: 0, workers: 0, tokens: 0, cost: 0 });
    const h = header();
    const sameSession = { ...row, ownership: { ...row.ownership, session_id: selection().session_id } };
    const state = f.store.getSnapshot(), key = metadataSelectionKey(row.selection);
    const unknown = header({ usage: { ...h.usage!, tokens: null, tokens_known_workers: 0, cost_known_workers: 0 }, cost: { ...h.cost, selected_usd: null } });
    expect(sessionWorkerUsage({ ...state, observations: [{ row: sameSession, freshness: 'observed' }], headers: { [key]: { refreshedAt: Date.now(), observation: { row: { ...sameSession, header: unknown }, freshness: 'observed' } } } })).toMatchObject({ kind: 'complete', tokens: null, cost: null });
  });
  it('keeps headers bounded and allows source discovery during repeated failures', async () => {
    const rows = Array.from({ length: 30 }, (_, i) => expertSummary(selection(i + 1), `Worker ${i}`));
    const f = await setup(rows); let now = Date.now(); vi.spyOn(Date, 'now').mockImplementation(() => now);
    f.fail(true); await ticks(f, 128);
    const count = f.request.mock.calls.filter(([, path]) => path.endsWith('/pins')).length;
    await ticks(f, 128);
    expect(f.request.mock.calls.filter(([, path]) => path.endsWith('/pins')).length).toBe(count);
    expect(f.store.getSnapshot().streams.every(s => s.state === 'complete')).toBe(true);
    expect(Object.keys(f.store.getSnapshot().headerReads).length).toBeLessThanOrEqual(200);
    f.fail(false); now += 120001; await ticks(f, 128);
    expect(Object.keys(f.store.getSnapshot().headers)).toHaveLength(30);
    expect(Math.max(...f.batches)).toBeLessThanOrEqual(8);
  });
  it.each(['job_routing', 'task_assignment', 'uniform_task_assignments'] as const)('accepts and displays authoritative %s model headers through the pin parser', provenance => {
    const s = selection(), row = expertSummary(s, 'Routed job');
    const h = header({ model_provenance: provenance });
    const context = { session_id: s.session_id, repo: s.repo, scope: 'session' as const, view_generation: 'generation-1' };
    const parsed = parseMetadataPins({ version: 1, context, results: [{ selection: s, result: { kind: 'present', row: { ...row, header: h } } }] }, context, [s]);
    const result = parsed.results[0].result;
    expect(result.kind === 'present' && expertHeaderModel(result.row.header)).toBe('gpt-6-astra');
  });
  it('rejects unproven or malformed model metadata', () => {
    expect(parseExpertHeader(header({ model_provenance: 'unknown' }))).toMatchObject({ model: null, model_provenance: 'unknown' });
    expect(parseExpertHeader(header({ model: '' }))).toMatchObject({ model: null, model_provenance: 'unknown' });
    expect(() => parseExpertHeader({ ...header(), model_provenance: 'history' })).toThrow();
    expect(expertHeaderModel(header({ model: null, model_provenance: 'unknown' }))).toBeNull();
    expect(expertHeaderModel(header({ model: 'codex' }))).toBeNull();
  });
});
