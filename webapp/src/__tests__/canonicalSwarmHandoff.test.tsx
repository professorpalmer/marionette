import { act } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { metadataActivity, metadataJobs } from '../lib/jobMetadataContext';
import { JobMetadataClient } from '../lib/jobMetadata';
import type { MetadataContext, MetadataSummary } from '../lib/jobMetadata';
import { JobMetadataStore } from '../lib/useJobMetadata';
import type { LocalSummary } from '../lib/localJobMetadata';
import { handshake, list, view } from './jobMetadata.fixtures';

const stores: JobMetadataStore[] = [];
const incarnation = 'local-handoff-incarnation';
const canonical = {
  source: 'harness' as const,
  job_ref: { job_id: 'job_handoff', state_id: 'state-canonical', version: 2 as const, incarnation: '12345678-1234-1234-1234-123456789abc' },
  session_id: 'session-owner',
  dispatch_id: 'dispatch-handoff',
};

function context(session_id: string, scope: 'all' | 'session' = 'all'): MetadataContext {
  return { session_id, repo: '/repo', view_generation: `generation-${session_id}`, scope };
}
function viewContext(c: MetadataContext) {
  return { session_id: c.session_id, repo: c.repo, view_generation: c.view_generation };
}
function localRow(session_id: string, canonicalRef = canonical): LocalSummary {
  return {
    local_ref: { job_id: 'local-swarm-dispatch-handoff', incarnation }, revision: 7, deleted: false,
    session_id, lifecycle: 'running', kind: 'provider', parent_ref: null,
    task_count: 5, action_count: 0, artifact_count: 0, child_count: null, created_at: 1, updated_at: 1,
    receipts: { terminal: false, launch: false, recovery: false, child: false },
    economics: { kind: 'unavailable' }, display: { label: 'Provider worker', model: 'route-model', adapter: 'agentic', truncated: false },
    canonical: canonicalRef,
  };
}
function pmRow(viewContext: MetadataContext): MetadataSummary {
  return {
    selection: { source: 'harness', session_id: viewContext.session_id, repo: viewContext.repo, job_ref: canonical.job_ref },
    revision: 11, deleted: false, lifecycle: 'running', ownership: { origin: 'marionette', session_id: canonical.session_id, project_id: null },
    task_count: 5, artifact_count: 0, stamp: 'known', display: { kind: 'available', goal_preview: 'canonical swarm', goal_preview_truncated: false, delivery: 'pending', quality: 'unverified' },
    economics: { kind: 'unavailable', reason: 'selected_only' },
  };
}
function localEnvelope(c: MetadataContext, rows: LocalSummary[]) {
  return {
    version: 1, context: c, incarnation, lane: 'active', rows,
    page: { outcome: 'complete', revision: 11, checkpoint: 11, scanned: rows.length, next_cursor: null },
    coverage: { membership: 'retained_local_active', metadata: 'live_during_traversal', historical: 'unavailable', ordering: 'id' }, missing: [],
  };
}

afterEach(() => { stores.forEach(store => store.dispose()); stores.length = 0; vi.unstubAllGlobals(); Reflect.deleteProperty(window, 'harnessIPC'); });

async function observe(store: JobMetadataStore, turns = 24) {
  for (let turn = 0; turn < turns; turn++) await store.advance(turn === 0);
}

it('replaces only the exact canonical placeholder and keeps one row when PM becomes unavailable', async () => {
  let pmMode: 'empty' | 'present' | 'unavailable' = 'empty';
  let target = context('session-view');
  const requestJSON = vi.fn(async (_method: string, path: string) => {
    const url = new URL(path, 'http://fixture');
    if (url.pathname === '/api/endpoint') return { kind: 'response', status: 200, correlationId: '', text: JSON.stringify(handshake) };
    if (url.pathname.endsWith('/view')) {
      const current = context(target.session_id, target.scope);
      return { kind: 'response', status: 200, correlationId: '', text: JSON.stringify({ ...view(current.view_generation), context: viewContext(current), local: { available: true, incarnation, version: 1, lanes: ['active', 'history'], active_statuses: ['running'], attention_statuses: [] }, sources: [{ source: 'harness', state_id: canonical.job_ref.state_id, cross_project: false, available: true }] }) };
    }
    if (url.pathname.endsWith('/local')) return { kind: 'response', status: 200, correlationId: '', text: JSON.stringify(localEnvelope(context(target.session_id, target.scope), [localRow(canonical.session_id)])) };
    if (url.pathname === '/api/jobs/metadata') {
      const status = url.searchParams.get('status');
      const rows = pmMode === 'present' && (!status || status === 'running') ? [pmRow(context(target.session_id, target.scope))] : [];
      const page = pmMode === 'unavailable'
        ? { outcome: 'unavailable', revision: 0, checkpoint: Number(url.searchParams.get('after_revision') || 0), scanned: 0, next_cursor: null }
        : pmMode === 'present'
        ? list(rows).page
        : { outcome: 'complete', revision: 11, checkpoint: 11, scanned: 0, next_cursor: null };
      return { kind: 'response', status: 200, correlationId: '', text: JSON.stringify({ ...list(rows), context: context(target.session_id, target.scope), store: { source: 'harness', state_id: canonical.job_ref.state_id }, mode: url.searchParams.get('mode'), page }) };
    }
    throw Error(`unexpected ${path}`);
  });
  Object.defineProperty(window, 'harnessIPC', { configurable: true, value: { endpointHeaders: true, requestJSON } });
  const store = new JobMetadataStore(new JobMetadataClient(1000)); stores.push(store);
  await act(async () => { store.setTarget(target); await store.readView(); await observe(store); });
  expect(metadataJobs(store.getSnapshot()).map(job => job.id)).toEqual(['local-swarm-dispatch-handoff']);
  expect(metadataActivity(store.getSnapshot()).count).toBe(1);

  pmMode = 'present';
  act(() => store.restartTraversal());
  await act(async () => { await observe(store); });
  const canonicalJobs = metadataJobs(store.getSnapshot());
  expect(canonicalJobs).toHaveLength(1);
  expect(canonicalJobs[0]).toMatchObject({ id: canonical.job_ref.job_id, source: 'harness', session_id: canonical.session_id, job_ref: canonical.job_ref });
  expect(metadataActivity(store.getSnapshot()).count).toBe(1);

  pmMode = 'unavailable';
  act(() => store.restartTraversal());
  await act(async () => { await observe(store); });
  // The swarm stays one canonical row; its live alias carries the lifecycle while PM is stale.
  expect(metadataJobs(store.getSnapshot()).map(job => job.id)).toEqual(['job_handoff']);
  expect(metadataJobs(store.getSnapshot())[0]).toMatchObject({ status: 'running', canonical_aliases: ['local-swarm-dispatch-handoff'] });
  expect(metadataJobs(store.getSnapshot())[0].read_status).toBeUndefined();
  expect(metadataActivity(store.getSnapshot()).count).toBe(1);
});

it('does not carry a canonical placeholder across a view-session switch', async () => {
  let target = context('session-owner');
  Object.defineProperty(window, 'harnessIPC', { configurable: true, value: { endpointHeaders: true,
    requestJSON: async (_method: string, path: string) => {
      const url = new URL(path, 'http://fixture');
      if (url.pathname === '/api/endpoint') return { kind: 'response', status: 200, correlationId: '', text: JSON.stringify(handshake) };
      if (url.pathname.endsWith('/view')) return { kind: 'response', status: 200, correlationId: '', text: JSON.stringify({ ...view(target.view_generation), context: viewContext(target), local: { available: true, incarnation, version: 1, lanes: ['active', 'history'], active_statuses: ['running'], attention_statuses: [] }, sources: [{ source: 'harness', state_id: canonical.job_ref.state_id, cross_project: false, available: true }] }) };
      if (url.pathname.endsWith('/local')) return { kind: 'response', status: 200, correlationId: '', text: JSON.stringify(localEnvelope(target, target.session_id === 'session-owner' ? [localRow('session-owner')] : [])) };
      if (url.pathname === '/api/jobs/metadata') {
        const status = url.searchParams.get('status');
        const rows = target.session_id === 'session-owner' && (!status || status === 'running') ? [pmRow(target)] : [];
        return { kind: 'response', status: 200, correlationId: '', text: JSON.stringify({ ...list(rows), context: target, store: { source: 'harness', state_id: canonical.job_ref.state_id }, mode: url.searchParams.get('mode') }) };
      }
      throw Error(`unexpected ${path}`);
    },
  } });
  const store = new JobMetadataStore(new JobMetadataClient(1000)); stores.push(store);
  await act(async () => { store.setTarget(target); await store.readView(); await observe(store); });
  expect(metadataJobs(store.getSnapshot())).toHaveLength(1);
  target = context('session-other');
  await act(async () => { store.setTarget(target); await store.readView(); await observe(store); });
  expect(metadataJobs(store.getSnapshot())).toEqual([]);
});
