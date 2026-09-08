import { act } from '@testing-library/react';
import type { ReactNode } from 'react';
import { vi } from 'vitest';
import { JobMetadataContext } from '../lib/jobMetadataContext';
import { JobMetadataClient } from '../lib/jobMetadata';
import type { DetailCursors, MetadataContext, MetadataDetail, MetadataSelection, MetadataSummary } from '../lib/jobMetadata';
import { JobMetadataStore } from '../lib/useJobMetadata';
import { detail, handshake, list, summary, view } from './jobMetadata.fixtures';

export function expertSummary(selected: MetadataSelection, goal: string): MetadataSummary {
  return { ...summary(), selection: selected, ownership: { origin: 'marionette', session_id: selected.session_id, project_id: null },
    display: { kind: 'available', goal_preview: goal, goal_preview_truncated: false, delivery: 'unverified', quality: 'unverified' } };
}
export function expertDetail(selected: MetadataSelection, context: MetadataContext): MetadataDetail {
  return { ...detail(), selection: selected, context };
}
/** Only wire responses are mocked: parser, transport fencing and scheduling are real. */
export async function expertMetadataFixture(rows: MetadataSummary[], options: { browser?: boolean } = {}) {
  const first = rows[0];
  if (!first) throw Error('Fixture needs an explicit selection');
  let context: MetadataContext = { repo: first.selection.repo, session_id: first.selection.session_id, scope: 'all', view_generation: 'fixture-generation' };
  const previousBridge = Object.getOwnPropertyDescriptor(window, 'harnessIPC');
  const selected = vi.fn(async (selection: MetadataSelection, _cursors: DetailCursors): Promise<unknown> => expertDetail(selection, context));
  const request = vi.fn(async (_method: string, path: string): Promise<unknown> => {
    const url = new URL(path, 'http://fixture');
    if (url.pathname === '/api/endpoint') return handshake;
    if (url.pathname.endsWith('/view')) return { ...view(), context: { repo: context.repo, session_id: context.session_id, view_generation: context.view_generation },
      sources: [...new Map(rows.map(row => [row.selection.job_ref.state_id, { source: row.selection.source, state_id: row.selection.job_ref.state_id, cross_project: false, available: true }])).values()] };
    if (url.pathname.endsWith('/detail')) {
      const row = rows.find(row => row.selection.job_ref.job_id === url.searchParams.get('job_id') && row.selection.job_ref.state_id === url.searchParams.get('state_id') && row.selection.source === url.searchParams.get('source'));
      if (!row) throw Error('Unknown selected fixture identity');
      return selected(row.selection, { task_cursor: url.searchParams.get('task_cursor'), artifact_cursor: url.searchParams.get('artifact_cursor'),
        attempt_cursor: url.searchParams.get('attempt_cursor'), run_cursor: url.searchParams.get('run_cursor'), process_outcome_cursor: url.searchParams.get('process_outcome_cursor'), observation_cursor: url.searchParams.get('observation_cursor') });
    }
    if (url.pathname === '/api/jobs/metadata') {
      const status = url.searchParams.get('status');
      const matched = rows.filter(row => row.selection.job_ref.state_id === url.searchParams.get('state_id') && row.selection.source === url.searchParams.get('source') && (!status || row.lifecycle === status));
      return { ...list(matched), context, store: { source: url.searchParams.get('source'), state_id: url.searchParams.get('state_id') }, mode: url.searchParams.get('mode') };
    }
    throw Error(`Unexpected metadata request ${url.pathname}`);
  });
  const wire = vi.fn(async (method: string, path: string) => {
    try { return { status: 200, body: await request(method, path) }; }
    catch { return { status: 503, body: { error: 'Store unavailable' } }; }
  });
  const browserFetch = vi.fn(async (path: string | URL | Request, init?: RequestInit) => {
    const response = await wire(init?.method ?? 'GET', String(path));
    return Response.json(response.body, { status: response.status });
  });
  if (options.browser) {
    Reflect.deleteProperty(window, 'harnessIPC');
    vi.stubGlobal('fetch', browserFetch);
  } else Object.defineProperty(window, 'harnessIPC', { configurable: true, value: { endpointHeaders: true,
    requestJSON: async (method: string, path: string) => {
      const result = await wire(method, path);
      return { kind: 'response', status: result.status, correlationId: '', text: JSON.stringify(result.body) };
    } } });
  const store = new JobMetadataStore(new JobMetadataClient(1000));
  const observe = async () => {
    let result: unknown;
    await act(async () => { store.setTarget(context); });
    await act(async () => { result = await store.readView(); });
    for (let i = 0; i < 24 && store.getSnapshot().observations.length < rows.length; i++) await act(async () => { await store.advance(); });
    if (store.getSnapshot().observations.length !== rows.length) throw Error(`Fixture rows rejected: ${JSON.stringify({ result, working: store.getSnapshot().working, view: store.getSnapshot().view, error: store.getSnapshot().error, streams: store.getSnapshot().streams, requests: request.mock.calls })}`);
  };
  await observe();
  return { store, selected, request, browserFetch, observe, context: () => context,
    async replace(next: MetadataSummary[]) { rows = next; context = { ...context, repo: next[0].selection.repo, session_id: next[0].selection.session_id }; await observe(); },
    Provider({ children }: { children: ReactNode }) { return <JobMetadataContext.Provider value={store}>{children}</JobMetadataContext.Provider>; },
    dispose() { store.dispose(); if (previousBridge) Object.defineProperty(window, 'harnessIPC', previousBridge); else Reflect.deleteProperty(window, 'harnessIPC'); },
  };
}
