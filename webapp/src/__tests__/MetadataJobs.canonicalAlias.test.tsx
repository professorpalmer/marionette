import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import SwarmPane from '../components/SwarmPane';
import { currentExpert, metadataJobs, JobMetadataContext } from '../lib/jobMetadataContext';
import { JobMetadataClient, MetadataError, metadataSelectionKey } from '../lib/jobMetadata';
import type { MetadataContext, MetadataSelection, MetadataSummary } from '../lib/jobMetadata';
import { JobMetadataStore, metadataBannerAfterError } from '../lib/useJobMetadata';
import type { JobMetadataState } from '../lib/useJobMetadata';
import { stickyJobQuality } from '../lib/expertOutcomeFacts';
import type { LocalObservation, LocalSummary } from '../lib/localJobMetadata';
import { nativeActiveStatuses, nativeAttentionStatuses } from '../lib/localJobMetadata';
import { handshake, list, view } from './jobMetadata.fixtures';
import { clearSWRCache } from '../lib/useStaleWhileRevalidate';

vi.mock('../lib/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../lib/api')>();
  return {
    ...actual,
    api: {
      ...actual.api,
      swarmLive: vi.fn(),
      swarmCancel: vi.fn(),
      artifacts: vi.fn(),
      dashboard: vi.fn().mockResolvedValue({
        ok: true, reused: true, host: '127.0.0.1', port: 8787,
        url: 'http://127.0.0.1:8787/?job=job_canonical&embed=1',
        embed_url: 'http://127.0.0.1:8787/?job=job_canonical&embed=1',
      }),
      sessions: vi.fn().mockResolvedValue([]),
    },
  };
});

const incarnation = 'fbd538fe321b61223e2ad6903f225d52';
const canonical = {
  source: 'harness' as const,
  job_ref: {
    job_id: 'job_4ba915d01102',
    state_id: 'state_canonical',
    version: 2 as const,
    incarnation: '7e62abcd-1234-4234-8234-123456789abc',
  },
  session_id: 'sess-test',
  dispatch_id: 'dispatch-canonical',
};

function context(session_id = 'sess-test'): MetadataContext {
  return { session_id, repo: '/repo', view_generation: `generation-${session_id}`, scope: 'all' };
}

function pmSelection(c: MetadataContext = context()): MetadataSelection {
  return {
    source: 'harness',
    session_id: c.session_id,
    repo: c.repo,
    job_ref: canonical.job_ref,
  };
}

function localSummary(overrides: Partial<LocalSummary> = {}): LocalSummary {
  return {
    local_ref: { job_id: 'local-swarm-call_00_alias', incarnation },
    revision: 7,
    deleted: false,
    session_id: canonical.session_id,
    lifecycle: 'running',
    kind: 'provider',
    parent_ref: null,
    task_count: 1,
    action_count: 0,
    artifact_count: 0,
    child_count: null,
    created_at: 1,
    updated_at: 1,
    receipts: { terminal: false, launch: false, recovery: false, child: false },
    economics: { kind: 'unavailable' },
    display: { label: 'Provider worker', model: '', adapter: 'agentic', truncated: false },
    canonical,
    ...overrides,
  };
}

function pmRow(c: MetadataContext = context()): MetadataSummary {
  return {
    selection: pmSelection(c),
    revision: 11,
    deleted: false,
    lifecycle: 'running',
    ownership: { origin: 'marionette', session_id: canonical.session_id, project_id: null },
    task_count: 4,
    artifact_count: 8,
    stamp: 'known',
    display: {
      kind: 'available',
      goal_preview: 'canonical swarm goal',
      goal_preview_truncated: false,
      delivery: 'pending',
      quality: 'unverified',
    },
    economics: { kind: 'unavailable', reason: 'selected_only' },
  };
}

function fourTaskDetail(
  selected: MetadataSelection,
  c: MetadataContext,
  overrides: { lifecycle?: string; quality?: 'unverified' | 'degraded' | 'ok' } = {},
) {
  const ids = ['task-a', 'task-b', 'task-c', 'task-d'];
  const revision = 100;
  const tasks = ids.map((id, index) => ({
    id,
    status: index === 0 ? 'running' : 'queued',
    stamp: 'known',
    revision: 2,
    binding: { task_id: id, generation: 1, lease_id: `lease-${id}`, owner: 'worker' },
  }));
  const findings = ids.map((id) => ({
    id: `artifact-${id}`,
    status: null,
    stamp: 'known',
    revision: 3,
    task_id: id,
    type: 'finding',
    sha256: 'a'.repeat(64),
    presence: 'recorded',
    check_result: 'unavailable',
  }));
  const routes = ids.map((id) => ({
    id: `route-${id}`,
    status: null,
    stamp: 'known',
    revision: 3,
    task_id: id,
    type: 'routing',
    sha256: 'b'.repeat(64),
    presence: 'recorded',
    check_result: 'unavailable',
  }));
  const artifactRows = [...findings, ...routes];
  return {
    version: 1,
    selection: selected,
    context: c,
    lifecycle: overrides.lifecycle ?? 'running',
    display: {
      kind: 'available',
      goal_preview: 'canonical swarm goal',
      goal_preview_truncated: false,
      delivery: 'pending',
      quality: 'unverified',
    },
    task_count: 4,
    artifact_count: artifactRows.length,
    tasks: {
      page: { outcome: 'complete', revision, scanned: tasks.length, checkpoint: revision, next_cursor: null },
      rows: tasks,
    },
    artifacts: {
      page: { outcome: 'complete', revision, scanned: artifactRows.length, checkpoint: revision, next_cursor: null },
      rows: artifactRows,
    },
    expert: {
      kind: 'available',
      reason: null,
      header: {
        created_at: '2026-09-11T12:00:00Z',
        completed_at: null,
        selected_workers: 4,
        completed_workers: 0,
        workers_complete: false,
        model: 'deepseek-v4-flash',
        model_provenance: 'job_routing',
        usage: {
          tokens: 1690000,
          tokens_known_workers: 4,
          cost_known_workers: 0,
          selected_workers: 4,
          complete: true,
        },
        savings: {
          routing_usd: null,
          cache_usd: null,
          compaction_usd: null,
          compact_tokens: null,
          selected_usd: null,
          basis: 'estimated',
          source: 'selected_current_records',
        },
        cost: {
          selected_usd: null,
          source: 'selected_current_records',
          basis: 'estimated',
          measured_cost_usd: null,
          estimated_cost_usd: null,
          complete: true,
          plan_workers: 0,
        },
      },
      tasks: ids.map((id, index) => ({
        id,
        role: `Worker ${index + 1}`,
        instruction: `Do work ${index + 1}`,
        instruction_truncated: false,
        adapter: 'agentic',
        model: 'deepseek-v4-flash',
        created_at: null,
        updated_at: null,
        usage: { tokens_in: 10, tokens_out: 5, est_cost_usd: null, estimated: null, cost_provenance: null },
      })),
      artifacts: [
        ...findings.map((row, index) => ({
          id: row.id,
          task_id: row.task_id,
          type: 'FINDING',
          created_by: 'worker',
          created_at: `2026-09-11T12:0${index}:00Z`,
          headline: `Evidence ${index + 1}`,
          detail: null,
          result: 'recorded',
          failure: null,
          confidence: null,
          model: null,
          adapter: null,
          policy: null,
          provider: null,
          role: null,
          est_cost_usd: null,
          rejected: [],
          check_result: 'unavailable',
        })),
        ...routes.map((row, index) => ({
          id: row.id,
          task_id: row.task_id,
          type: 'ROUTING',
          created_by: 'router',
          created_at: `2026-09-11T12:0${index}:00Z`,
          headline: 'route',
          detail: 'matched',
          result: null,
          failure: null,
          confidence: null,
          model: 'deepseek-v4-flash',
          adapter: 'agentic',
          policy: 'balanced',
          provider: null,
          role: null,
          est_cost_usd: null,
          rejected: [],
          check_result: 'unavailable',
        })),
      ],
      coverage: { tasks: 'complete', artifacts: 'complete' },
      quality: overrides.quality ?? 'unverified',
    },
    history: { kind: 'unavailable', reason: 'public_read_unbounded' },
    cost: { kind: 'unavailable', reason: 'not_in_metadata' },
    cancellation_authority: false,
    missing: ['history', 'cost'],
  };
}

function localEnvelope(c: MetadataContext, rows: LocalSummary[], lane: 'active' | 'history' = 'active') {
  return {
    version: 1,
    context: c,
    incarnation,
    lane,
    rows,
    page: { outcome: 'complete', revision: 11, checkpoint: 11, scanned: rows.length, next_cursor: null },
    coverage: {
      membership: lane === 'active' ? 'retained_local_active' : 'retained_local_history',
      metadata: 'live_during_traversal',
      historical: 'unavailable',
      ordering: 'id',
    },
    missing: [],
  };
}

const stores: JobMetadataStore[] = [];

afterEach(() => {
  cleanup();
  stores.forEach((store) => store.dispose());
  stores.length = 0;
  vi.unstubAllGlobals();
  Reflect.deleteProperty(window, 'harnessIPC');
  localStorage.clear();
  sessionStorage.clear();
  clearSWRCache();
});

async function mountCanonicalAlias(options: {
  includePmList?: boolean;
  lifecycle?: string;
  quality?: 'unverified' | 'degraded' | 'ok';
} = {}) {
  let pmPresent = options.includePmList === true;
  let lifecycle = options.lifecycle ?? 'running';
  let failDetail = false;
  let tasksTerminal = false;
  const quality = options.quality ?? 'unverified';
  const c = context();
  const selected = pmSelection(c);
  const summary = localSummary({ lifecycle });
  const requestJSON = vi.fn(async (_method: string, path: string) => {
    const url = new URL(path, 'http://fixture');
    if (url.pathname === '/api/endpoint') {
      return { kind: 'response', status: 200, correlationId: '', text: JSON.stringify(handshake) };
    }
    if (url.pathname.endsWith('/view')) {
      return {
        kind: 'response',
        status: 200,
        correlationId: '',
        text: JSON.stringify({
          ...view(c.view_generation),
          context: { session_id: c.session_id, repo: c.repo, view_generation: c.view_generation },
          local: {
            available: true,
            incarnation,
            version: 1,
            lanes: ['active', 'history'],
            active_statuses: nativeActiveStatuses,
            attention_statuses: nativeAttentionStatuses,
          },
          sources: [{ source: 'harness', state_id: canonical.job_ref.state_id, cross_project: false, available: true }],
        }),
      };
    }
    if (url.pathname.endsWith('/local') || url.pathname.includes('/metadata/local')) {
      if (url.pathname.endsWith('/detail')) {
        const lane = url.searchParams.get('lane') ?? 'tasks';
        const rows = lane === 'tasks'
          ? [{
            task_id: `${summary.local_ref.job_id}-w0`,
            role: 'explore (agentic)',
            instruction: 'Synthetic alias instruction',
            status: 'running',
            adapter: 'agentic',
            model: '',
            model_kind: 'unavailable',
            truncated: false,
          }]
          : [];
        return {
          kind: 'response',
          status: 200,
          correlationId: '',
          text: JSON.stringify({
            version: 1,
            context: c,
            local_ref: summary.local_ref,
            summary,
            lane,
            rows,
            total: rows.length,
            selected_context: {
              source: 'goal',
              request: { text: 'Synthetic alias instruction', truncated: false },
              cwd: null,
              omission: 'none',
            },
            page: { outcome: 'complete', revision: summary.revision, checkpoint: summary.revision, scanned: rows.length, next_cursor: null },
            coverage: { membership: 'retained_local_active', ordering: 'id', historical: 'unavailable' },
            missing: [],
            cancellation_authority: false,
          }),
        };
      }
      const lane = (url.searchParams.get('lane') ?? 'history') as 'active' | 'history';
      return {
        kind: 'response',
        status: 200,
        correlationId: '',
        text: JSON.stringify(localEnvelope(c, [summary], lane)),
      };
    }
    if (url.pathname === '/api/jobs/metadata') {
      const status = url.searchParams.get('status');
      const rows = pmPresent && (!status || status === lifecycle) ? [{ ...pmRow(c), lifecycle }] : [];
      return {
        kind: 'response',
        status: 200,
        correlationId: '',
        text: JSON.stringify({
          ...list(rows),
          context: c,
          store: { source: 'harness', state_id: canonical.job_ref.state_id },
          mode: url.searchParams.get('mode'),
          page: {
            outcome: 'complete',
            revision: 11,
            checkpoint: 11,
            scanned: rows.length,
            next_cursor: null,
          },
        }),
      };
    }
    if (url.pathname.endsWith('/detail')) {
      if (failDetail) throw new MetadataError('unavailable');
      return {
        kind: 'response',
        status: 200,
        correlationId: '',
        text: JSON.stringify((() => {
          const detail = fourTaskDetail(selected, c, { lifecycle, quality });
          if (tasksTerminal) {
            detail.tasks.rows.forEach(task => { task.status = 'complete'; });
            detail.lifecycle = 'complete';
          }
          return detail;
        })()),
      };
    }
    throw Error(`unexpected ${path}`);
  });
  Object.defineProperty(window, 'harnessIPC', {
    configurable: true,
    value: { endpointHeaders: true, requestJSON },
  });
  const store = new JobMetadataStore(new JobMetadataClient(1000));
  stores.push(store);
  await act(async () => {
    store.setTarget({ repo: c.repo, session_id: c.session_id, scope: 'all' });
    await store.readView();
    await store.advance(true);
    await (store as unknown as { advanceLocal: (lane: 'active' | 'history') => Promise<unknown> }).advanceLocal('active');
    for (let turn = 0; turn < 24; turn++) await store.advance();
  });
  localStorage.setItem('marionette.jobScope.v1', 'repo');
  const ui = render(
    <JobMetadataContext.Provider value={store}>
      <SwarmPane />
    </JobMetadataContext.Provider>,
  );
  return {
    store,
    requestJSON,
    setPmPresent(value: boolean) { pmPresent = value; },
    setLifecycle(value: string) { lifecycle = value; },
    setFailDetail(value: boolean) { failDetail = value; },
    setTasksTerminal(value: boolean) { tasksTerminal = value; },
    ...ui,
  };
}

describe('metadataJobs alias dedupe', () => {
  it('dedupes the same local job_id from observations and localDetail by freshest revision', () => {
    const older = localSummary({ revision: 3, local_ref: { job_id: 'local-swarm-call_00_alias', incarnation: 'old-incarnation' } });
    const newer = localSummary({ revision: 9 });
    const c = context();
    const state = {
      view: {
        kind: 'view',
        target: { repo: c.repo, session_id: c.session_id, scope: 'all' },
        context: c,
        view: {
          ...view(c.view_generation),
          context: { session_id: c.session_id, repo: c.repo, view_generation: c.view_generation },
          local: { available: true, incarnation, version: 1 },
        },
        refresh: 'idle',
      },
      observations: [],
      pins: [],
      local: {
        observations: [
          { row: older, freshness: 'observed', observedAt: 1 },
          { row: newer, freshness: 'observed', observedAt: 2 },
        ] satisfies LocalObservation[],
        traversal: { mode: 'snapshot', after_revision: 0, cursor: null },
        state: 'complete',
        missing: [],
        observedAt: 2,
      },
      localDetail: {
        selection: newer.local_ref,
        lane: 'tasks',
        observation: {
          lane: 'tasks',
          local_ref: newer.local_ref,
          summary: { ...newer, revision: 12 },
          rows: [],
          page: { outcome: 'complete', revision: 12, checkpoint: 12, scanned: 0, next_cursor: null },
          missing: [],
          total: 0,
        },
        summaryFreshness: 'observed',
        laneFreshness: 'observed',
        error: null,
      },
      detail: { kind: 'none' },
      detailCache: {},
      headers: {},
      working: false,
      error: null,
    } as unknown as JobMetadataState;
    const jobs = metadataJobs(state);
    expect(jobs).toHaveLength(1);
    expect(jobs[0].id).toBe('local-swarm-call_00_alias');
    expect(jobs[0].local_ref?.incarnation).toBe(incarnation);
  });

  it('moves a live alias to finished as soon as its canonical detail or a stale PM row reports terminal', () => {
    const c = context();
    const selected = pmSelection(c);
    const key = metadataSelectionKey(selected);
    const alias = localSummary({ lifecycle: 'running' });
    const base = {
      view: { kind: 'view', target: { repo: c.repo, session_id: c.session_id, scope: 'all' }, context: c, view: view(c.view_generation), refresh: 'idle' },
      pins: [],
      local: { observations: [{ row: alias, freshness: 'observed', observedAt: 1 }] satisfies LocalObservation[], traversal: { mode: 'snapshot', after_revision: 0, cursor: null }, state: 'complete', missing: [], observedAt: 1 },
      localDetail: null,
      detail: { kind: 'none' },
      headers: {},
      working: false,
      error: null,
    };
    // List lanes have not caught up (no PM row yet), but the 4s detail hydrate already saw complete.
    const detailSettled = {
      ...base,
      observations: [],
      detailCache: { [key]: { kind: 'selected', selection: selected, cursors: { task_cursor: null, artifact_cursor: null }, observation: { ...fourTaskDetail(selected, c), lifecycle: 'complete' }, freshness: 'observed', error: null } },
    } as unknown as JobMetadataState;
    expect(metadataJobs(detailSettled).map(job => [job.id, job.status])).toEqual([['local-swarm-call_00_alias', 'complete']]);
    // A still-running detail leaves the alias running.
    const detailRunning = {
      ...base,
      observations: [],
      detailCache: { [key]: { kind: 'selected', selection: selected, cursors: { task_cursor: null, artifact_cursor: null }, observation: { ...fourTaskDetail(selected, c), lifecycle: 'running' }, freshness: 'observed', error: null } },
    } as unknown as JobMetadataState;
    expect(metadataJobs(detailRunning).map(job => job.status)).toEqual(['running']);
    // A PM row that reported complete and then went stale still replaces the alias: no snap back to Active.
    const stalePM = {
      ...base,
      observations: [{ row: { ...pmRow(c), lifecycle: 'complete' }, freshness: 'stale' }],
      detailCache: {},
    } as unknown as JobMetadataState;
    expect(metadataJobs(stalePM).map(job => [job.id, job.status])).toEqual([[canonical.job_ref.job_id, 'complete']]);
  });

  it('keeps the canonical link from selected detail when the equal-revision list row omits it', () => {
    const c = context();
    const selected = pmSelection(c);
    const key = metadataSelectionKey(selected);
    const listed = localSummary({ canonical: undefined });
    const detailSummary = localSummary();
    const state = {
      view: { kind: 'view', target: { repo: c.repo, session_id: c.session_id, scope: 'all' }, context: c, view: view(c.view_generation), refresh: 'idle' },
      observations: [],
      pins: [],
      local: { observations: [{ row: listed, freshness: 'observed', observedAt: 1 }] satisfies LocalObservation[], traversal: { mode: 'snapshot', after_revision: 0, cursor: null }, state: 'complete', missing: [], observedAt: 1 },
      localDetail: {
        selection: detailSummary.local_ref,
        lane: 'tasks',
        observation: { lane: 'tasks', local_ref: detailSummary.local_ref, summary: detailSummary, rows: [], page: { outcome: 'complete', revision: detailSummary.revision, checkpoint: detailSummary.revision, scanned: 0, next_cursor: null }, missing: [], total: 0 },
        summaryFreshness: 'observed', laneFreshness: 'observed', error: null,
      },
      detail: { kind: 'none' },
      detailCache: { [key]: { kind: 'selected', selection: selected, cursors: { task_cursor: null, artifact_cursor: null }, observation: { ...fourTaskDetail(selected, c), lifecycle: 'complete' }, freshness: 'observed', error: null } },
      headers: {}, working: false, error: null,
    } as unknown as JobMetadataState;
    expect(metadataJobs(state).map(job => [job.id, job.status])).toEqual([[listed.local_ref.job_id, 'complete']]);
  });

  it.each([null, 'unavailable'] as const)('keeps a hydrated expert through stale list and detail errors (%s)', (error) => {
    const c = context();
    const selected = pmSelection(c);
    const key = metadataSelectionKey(selected);
    const detail = fourTaskDetail(selected, c);
    const state = {
      view: { kind: 'view', target: { repo: c.repo, session_id: c.session_id, scope: 'all' }, context: c, view: view(c.view_generation), refresh: 'idle' },
      observations: [{ row: pmRow(c), freshness: 'stale' }],
      pins: [],
      detail: { kind: 'none' },
      detailCache: {
        [key]: {
          kind: 'selected',
          selection: selected,
          cursors: { task_cursor: null, artifact_cursor: null },
          observation: detail,
          freshness: 'stale',
          error,
          presentationRetained: error !== null,
        },
      },
      headers: {},
      error: 'invalid_metadata',
      working: false,
    } as unknown as JobMetadataState;
    const expert = currentExpert(state, key);
    expect(expert?.kind).toBe('available');
    expect(expert?.tasks).toHaveLength(4);
  });

  it('keeps the hydrated roster when a live list row runs ahead of the last detail read', () => {
    const c = context();
    const selected = pmSelection(c);
    const key = metadataSelectionKey(selected);
    const detail = fourTaskDetail(selected, c);
    const state = {
      view: { kind: 'view', target: { repo: c.repo, session_id: c.session_id, scope: 'all' }, context: c, view: view(c.view_generation), refresh: 'idle' },
      observations: [{ row: { ...pmRow(c), revision: detail.tasks.page.revision + 250 }, freshness: 'observed' }],
      pins: [],
      detail: { kind: 'none' },
      detailCache: {
        [key]: { kind: 'selected', selection: selected, cursors: { task_cursor: null, artifact_cursor: null }, observation: detail, freshness: 'observed', error: null },
      },
      headers: {},
      error: null,
      working: false,
    } as unknown as JobMetadataState;
    expect(currentExpert(state, key)?.tasks).toHaveLength(4);
  });
});

describe('SwarmPane canonical alias presentation', () => {
  it('retains running workers through a failed refresh and keeps terminal aliases finished after weaker refreshes', async () => {
    const fixture = await mountCanonicalAlias();
    const selected = pmSelection(context());
    try {
      const row = await screen.findByTestId(`inspect-local-${localSummary().local_ref.job_id}`);
      fireEvent.click(within(row).getByRole('button', { name: /Provider worker|canonical swarm goal|Synthetic alias/ }));
      await screen.findByRole('group', { name: 'Workers' });
      expect(metadataJobs(fixture.store.getSnapshot())[0].status).toBe('running');
      fixture.setFailDetail(true);
      await act(async () => { await fixture.store.hydrateDetail(selected, { prefetch: true }); });
      expect(fixture.store.getSnapshot().detailCache[metadataSelectionKey(selected)].freshness).toBe('stale');
      expect(within(screen.getByRole('group', { name: 'Workers' })).getByText('Worker 4')).toBeVisible();
      fixture.setFailDetail(false);
      fixture.setLifecycle('complete');
      await act(async () => { await fixture.store.hydrateDetail(selected, { prefetch: true }); });
      expect(metadataJobs(fixture.store.getSnapshot())[0].status).toBe('complete');
      expect(screen.getByRole('button', { name: /Finished/ })).toBeVisible();
      fixture.setLifecycle('running');
      await act(async () => {
        await fixture.store.hydrateDetail(selected, { prefetch: true });
        fixture.store.restartTraversal();
      });
      for (let turn = 0; turn < 24; turn++) {
        await act(async () => { await fixture.store.advance(); });
      }
      expect(metadataJobs(fixture.store.getSnapshot())[0].status).toBe('complete');
      expect(screen.getByRole('button', { name: /Finished/ })).toBeVisible();
    } finally { fixture.unmount(); }
  });

  it('titles a closed alias from PM hydrate without a click', async () => {
    const fixture = await mountCanonicalAlias({ includePmList: false });
    try {
      const row = await screen.findByTestId(`inspect-local-${localSummary().local_ref.job_id}`);
      expect(within(row).getByRole('button', { name: /Provider worker|canonical swarm goal · / })).toHaveAttribute('aria-expanded', 'false');
      await waitFor(() => {
        expect(within(row).getByRole('button', { name: /canonical swarm goal · / })).toBeVisible();
      });
      expect(within(row).getByRole('button', { name: /canonical swarm goal · / })).toHaveAttribute('aria-expanded', 'false');
      expect(row).not.toHaveTextContent('PRIVATE FULL PROMPT');
      expect(fixture.requestJSON.mock.calls.some(([, path]) => String(path).includes('/detail'))).toBe(true);
    } finally {
      fixture.unmount();
    }
  });

  it('shows finished alias title and degraded quality without a click or live job', async () => {
    const fixture = await mountCanonicalAlias({
      includePmList: false,
      lifecycle: 'complete',
      quality: 'degraded',
    });
    try {
      const row = await screen.findByTestId(`inspect-local-${localSummary().local_ref.job_id}`);
      await waitFor(() => {
        expect(within(row).getByRole('button', { name: /canonical swarm goal · complete/ })).toBeVisible();
      });
      expect(within(row).getByRole('button', { name: /canonical swarm goal · complete/ })).toHaveAttribute('aria-expanded', 'false');
      expect(row).toHaveAttribute('data-quality', 'degraded');
      expect(screen.getByText(/1 untrustworthy/i)).toBeVisible();
    } finally {
      fixture.unmount();
    }
  });

  it('renders canonical goal, job id, and a 4-worker PM roster for an alias with canonical', async () => {
    const fixture = await mountCanonicalAlias({ includePmList: false });
    try {
      const row = await screen.findByTestId(`inspect-local-${localSummary().local_ref.job_id}`);
      expect(within(row).queryAllByRole('button', { name: /Provider worker|canonical swarm goal/ })).toHaveLength(1);
      fireEvent.click(within(row).getByRole('button', { name: /Provider worker|canonical swarm goal|Synthetic alias/ }));
      await waitFor(() => {
        expect(screen.getByRole('button', { name: `Job ${canonical.job_ref.job_id}` })).toBeVisible();
      });
      await waitFor(() => {
        expect(screen.getAllByText('canonical swarm goal').length).toBeGreaterThan(0);
      });
      const roster = await screen.findByRole('group', { name: 'Workers' });
      expect(within(roster).getByText('Worker 1')).toBeVisible();
      expect(within(roster).getByText('Worker 2')).toBeVisible();
      expect(within(roster).getByText('Worker 3')).toBeVisible();
      expect(within(roster).getByText('Worker 4')).toBeVisible();
      expect(screen.queryByText(/workers loaded · partial coverage/i)).toBeNull();
      expect(screen.getAllByTestId(`inspect-local-${localSummary().local_ref.job_id}`)).toHaveLength(1);
    } finally {
      fixture.unmount();
    }
  });

  it('re-hydrates a live canonical alias so the roster follows the job instead of freezing', async () => {
    const fixture = await mountCanonicalAlias({ includePmList: false });
    const detailCalls = () => fixture.requestJSON.mock.calls.filter(([, path]) => String(path).includes('/detail')).length;
    try {
      const row = await screen.findByTestId(`inspect-local-${localSummary().local_ref.job_id}`);
      fireEvent.click(within(row).getByRole('button', { name: /Provider worker|canonical swarm goal|Synthetic alias/ }));
      await screen.findByRole('group', { name: 'Workers' });
      const first = detailCalls();
      expect(first).toBeGreaterThan(0);
      const later = Date.now() + 5000;
      vi.spyOn(Date, 'now').mockReturnValue(later);
      try {
        await act(async () => {
          for (let turn = 0; turn < 16; turn++) await fixture.store.advance();
        });
        await waitFor(() => expect(detailCalls()).toBeGreaterThan(first));
      } finally {
        vi.mocked(Date.now).mockRestore();
      }
    } finally {
      fixture.unmount();
    }
  });

  it('hides the alias once the canonical PM row is observed', async () => {
    const fixture = await mountCanonicalAlias({ includePmList: false });
    try {
      expect(await screen.findByTestId(`inspect-local-${localSummary().local_ref.job_id}`)).toBeVisible();
      fixture.setPmPresent(true);
      await act(async () => {
        fixture.store.restartTraversal();
        for (let turn = 0; turn < 24; turn++) await fixture.store.advance();
      });
      await waitFor(() => {
        expect(screen.queryByTestId(`inspect-local-${localSummary().local_ref.job_id}`)).toBeNull();
      });
      expect(screen.getByTestId(`inspect-harness-${canonical.job_ref.job_id}`)).toBeVisible();
      expect(metadataJobs(fixture.store.getSnapshot()).map((job) => job.id)).toEqual([canonical.job_ref.job_id]);
    } finally {
      fixture.unmount();
    }
  });

  it('titles a local alias from cached PM preview when list display has no goal_preview', () => {
    const c = context();
    const selected = pmSelection(c);
    const key = metadataSelectionKey(selected);
    const alias = localSummary({
      display: { label: 'Provider worker', model: '', adapter: 'agentic', truncated: false },
    });
    const state = {
      view: { kind: 'view', target: { repo: c.repo, session_id: c.session_id, scope: 'all' }, context: c, view: view(c.view_generation), refresh: 'idle' },
      observations: [],
      pins: [],
      local: {
        observations: [{ row: alias, freshness: 'observed', observedAt: 1 }],
        traversal: { mode: 'snapshot', after_revision: 0, cursor: null },
        state: 'complete',
        missing: [],
        observedAt: 1,
      },
      localDetail: null,
      detail: { kind: 'none' },
      detailCache: {
        [key]: {
          kind: 'selected',
          selection: selected,
          cursors: { task_cursor: null, artifact_cursor: null },
          observation: fourTaskDetail(selected, c),
          freshness: 'observed',
          error: null,
        },
      },
    } as unknown as JobMetadataState;
    const jobs = metadataJobs(state);
    expect(jobs).toHaveLength(1);
    expect(jobs[0].goal).toBe('canonical swarm goal');
    expect(jobs[0].goal).not.toMatch(/Provider worker|PRIVATE FULL PROMPT/);
  });

  it('titles a local alias from display.goal_preview before Provider worker', () => {
    const c = context();
    const alias = localSummary({
      display: {
        label: 'Provider worker',
        model: '',
        adapter: 'agentic',
        truncated: false,
        goal_preview: 'RE-AUDIT of the leftover tracker nits',
      },
    });
    const state = {
      view: { kind: 'view', target: { repo: c.repo, session_id: c.session_id, scope: 'all' }, context: c, view: view(c.view_generation), refresh: 'idle' },
      observations: [],
      pins: [],
      local: {
        observations: [{ row: alias, freshness: 'observed', observedAt: 1 }],
        traversal: { mode: 'snapshot', after_revision: 0, cursor: null },
        state: 'complete',
        missing: [],
        observedAt: 1,
      },
      localDetail: null,
      detail: { kind: 'none' },
      detailCache: {},
    } as unknown as JobMetadataState;
    const jobs = metadataJobs(state);
    expect(jobs).toHaveLength(1);
    expect(jobs[0].goal).toBe('RE-AUDIT of the leftover tracker nits');
    expect(jobs[0].goal).not.toMatch(/Provider worker/);
  });

  it('keeps degraded quality after a later unverified live value', () => {
    const first = stickyJobQuality('job-key', 'degraded', {});
    expect(first.quality).toBe('degraded');
    const later = stickyJobQuality('job-key', 'unverified', first.next);
    expect(later.quality).toBe('degraded');
  });

  it('does not raise the global banner for a detail-scope invalid_metadata', () => {
    expect(metadataBannerAfterError({ kind: 'detail' }, 'invalid_metadata', null)).toBeNull();
    expect(metadataBannerAfterError({ kind: 'pm' }, 'invalid_metadata', null)).toBe('invalid_metadata');
    const err = new MetadataError('invalid_metadata', 'expert:routing');
    expect(err.detail).toBe('expert:routing');
  });
});


it('settles the backend terminal projection and never reopens the alias on a weaker read', async () => {
  const fixture = await mountCanonicalAlias();
  fixture.setTasksTerminal(true);
  await act(async () => { await fixture.store.hydrateDetail(pmSelection(), { prefetch: true }); });
  expect(metadataJobs(fixture.store.getSnapshot())[0].status).toBe('complete');
  fixture.setTasksTerminal(false);
  await act(async () => { await fixture.store.hydrateDetail(pmSelection(), { prefetch: true }); });
  expect(metadataJobs(fixture.store.getSnapshot())[0].status).toBe('complete');
  fixture.unmount();
});
