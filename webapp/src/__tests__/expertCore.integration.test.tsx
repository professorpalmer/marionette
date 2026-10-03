import sqlite from './expertSqlite.backend.json';
import backend from './expertCurrent.backend.json';
import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import MetadataJobs from '../components/MetadataJobs';
import { inspectHarnessJob, JobsInspectHarness } from './jobsInspectHarness';
import { currentExpert, currentHeader } from '../lib/jobMetadataContext';
import { metadataSelectionKey, parseMetadataDetail, parseMetadataList } from '../lib/jobMetadata';
import type { MetadataSelection } from '../lib/jobMetadata';
import type { ExpertArtifact } from '../lib/expertMetadata';
import { parseExpertHeader, parseExpertMetadata } from '../lib/expertMetadata';
import { currentFacts as facts, currentDetail as selected } from './expertCurrent.fixtures';
import { context, selection as legacySelection } from './jobMetadata.fixtures';
import { expertMetadataFixture, expertSummary } from './metadataExpert.fixtures';

function selection(n = 1): MetadataSelection { const s = legacySelection(n); return { ...s, job_ref: { ...s.job_ref, version: 2, incarnation: '12345678-1234-4234-8234-123456789abc' } }; }
async function expandAndInspect(name?: string | RegExp) {
  const jobs = name
    ? screen.queryAllByRole('button', { name }).filter((btn) => btn.getAttribute('aria-expanded') != null)
    : screen.queryAllByRole('button').filter((btn) => (
      btn.getAttribute('aria-expanded') === 'false' && (btn.getAttribute('aria-label') || '').includes(' · ')
    ));
  const job = jobs[0];
  if (job?.getAttribute('aria-expanded') === 'false') fireEvent.click(job);
  fireEvent.click((await screen.findAllByRole('button', { name: /Inspect (tasks and artifacts|actions)/ }))[0]);
  await screen.findByRole('region', { name: 'Selected job inspector' });
}
function inspected() {
  return within(screen.getByRole('region', { name: 'Selected job inspector' }));
}
let fixture: Awaited<ReturnType<typeof expertMetadataFixture>> | undefined;
afterEach(() => { cleanup(); fixture?.dispose(); fixture = undefined; localStorage.clear(); vi.restoreAllMocks(); });
async function setup(rows = [expertSummary(selection(), 'Audit auth flow')]) {
  const f = fixture = await expertMetadataFixture(rows);
  f.selected.mockImplementation(async s => ({ ...selected(facts(), s), context: f.context() }));
  return f;
}
const cursors = { task_cursor: null, artifact_cursor: null };
describe('current expert contract boundary', () => {
  it('parses the actual backend economics envelope and preserves provider-attested zero', () => {
    const expert = parseExpertMetadata(backend);
    const payload = selected(expert);
    const parsed = parseMetadataDetail(payload, context, selection(), cursors);
    expect(parsed.expert?.live_economics?.cost.selected_usd).toBe(0);
    expect(parsed.expert?.tasks[0].usage).toMatchObject({ tokens: 120, est_cost_usd: 0, cost_provenance: 'provider', source_artifact_id: 'artifact-1' });
    expect(parsed.expert?.header?.cost.source).toBe('unavailable');
    const foreign = structuredClone(backend); foreign.economics.tasks['task-1'].source_artifact_id = 'foreign';
    expect(parseExpertMetadata(foreign).tasks[0].usage.source_artifact_id).toBeNull();
  });
  it('binds current bodies to exact selected task and artifact refs and page revisions', () => {
    expect(parseMetadataDetail(selected(), context, selection(), cursors).expert?.tasks[0].model).toBe('gpt-6-astra');
    const bad = selected(); bad.expert!.tasks[0].id = 'foreign';
    expect(parseMetadataDetail(bad, context, selection(), cursors).expert?.kind).toBe('unavailable');
    const badLink = selected(); badLink.expert!.artifacts[0].task_id = 'foreign';
    expect(parseMetadataDetail(badLink, context, selection(), cursors).expert?.kind).toBe('unavailable');
    const badRevision = selected();
    badRevision.artifacts.page = { ...badRevision.artifacts.page, revision: badRevision.artifacts.page.revision + 1, checkpoint: badRevision.artifacts.page.revision + 1 };
    const skewed = parseMetadataDetail(badRevision, context, selection(), cursors);
    expect(skewed.expert?.kind).toBe('unavailable');
    expect(skewed.expert?.reason).toBe('lane_revision_skew');
    expect(() => parseMetadataDetail(selected(), context, { ...selection(), source: 'cli' }, cursors)).toThrow();
  });
  it('rejects unknown keys, unsafe timestamps, duplicate refs and invalid numeric facts', () => {
    expect(() => parseExpertMetadata({ ...facts(), secret: 'unexpected' })).toThrow();
    expect(parseExpertHeader({ ...facts().header, created_at: 'not-a-clock' })?.created_at).toBeNull();
    const duplicate = facts(); duplicate.tasks.push(duplicate.tasks[0]);
    expect(() => parseExpertMetadata(duplicate)).toThrow();
    const invalid = facts(); invalid.tasks[0].usage.tokens_in = Infinity;
    expect(() => parseExpertMetadata(invalid)).toThrow();
    invalid.tasks[0].usage.tokens_in = 1.5; expect(() => parseExpertMetadata(invalid)).toThrow();
  });
  it('retains economics source attribution through a bounded body omission only when the exact artifact reference remains', () => {
    const expert = parseExpertMetadata(backend);
    const payload = selected(expert);
    const capped = { ...backend, kind: 'partial', quality: 'unverified', artifacts: [], coverage: { ...backend.coverage, artifacts: 'partial' }, economics: {
      ...backend.economics, header: { ...backend.economics.header, cost: { ...backend.economics.header.cost, complete: false }, usage: { ...backend.economics.header.usage, complete: false } },
    } };
    const accepted = parseMetadataDetail({ ...payload, expert: capped }, context, selection(), cursors);
    expect(accepted.expert?.tasks[0].usage.source_artifact_id).toBe('artifact-1');
    const orphaned = parseMetadataDetail({ ...payload, artifacts: { ...payload.artifacts, rows: [] }, expert: capped }, context, selection(), cursors);
    expect(orphaned.expert?.tasks[0].usage.source_artifact_id).toBeNull();
  });
  it('preserves live economics independently from the terminal header and rejects incomplete shapes', () => {
    const live = { created_at: null, completed_at: null, updated_at: null, latest_task_updated_at: null,
      completed_workers: 1, selected_workers: 2, workers_complete: true,
      usage: { tokens: 12000, tokens_known_workers: 2, cost_known_workers: 2, selected_workers: 2, complete: true },
      cost: { selected_usd: 1.5, source: 'selected_current_records', basis: 'mixed', measured_cost_usd: 1.25, estimated_cost_usd: .25, complete: true, plan_workers: 0 },
      savings: { routing_usd: .04, cache_usd: .01, compaction_usd: null, compact_tokens: null, selected_usd: .05, basis: 'estimated', source: 'selected_current_records' } };
    const parsed = parseExpertMetadata({ ...facts(), live_economics: live });
    expect(parsed.live_economics?.cost.selected_usd).toBe(1.5);
    expect(parsed.header?.cost.selected_usd).toBeNull();
    expect(() => parseExpertHeader({ ...live, completed_workers: 3 })).toThrow();
    expect(() => parseExpertHeader({ ...live, usage: undefined })).toThrow();
  });
});
describe('original positive inspector requirements through parser/store/render', () => {
  it('drives the real SQLite list, pin and selected payload through parser/client/store/render', async () => {
    const ctx = { ...sqlite.context, scope: 'session' as const };
    const parsed = parseMetadataList(sqlite.list, ctx, { store: { source: 'harness', state_id: sqlite.selection.job_ref.state_id }, status: null }, { mode: 'snapshot', after_revision: 0, cursor: null });
    const rows = parsed.rows.filter(row => !row.deleted);
    const f = await setup(rows); const original = f.request.getMockImplementation()!;
    f.request.mockImplementation(async (method, path, body) => {
      const url = new URL(path, 'http://fixture');
      if (url.pathname.endsWith('/pins')) return { ...sqlite.pins, context: f.context() };
      if (url.pathname === '/api/jobs/metadata') return { ...sqlite.list, context: f.context(), mode: url.searchParams.get('mode'),
        rows: !url.searchParams.get('status') || url.searchParams.get('status') === sqlite.detail.lifecycle ? sqlite.list.rows : [],
      };
      return original(method, path, body);
    });
    f.selected.mockResolvedValue({ ...sqlite.detail, context: f.context() });
    await act(async () => { await f.store.advance(); await f.store.refreshHeaders(); });
    render(<f.Provider><JobsInspectHarness><MetadataJobs /></JobsInspectHarness></f.Provider>);
    await expandAndInspect(/Inspect consumer integration/);
    expect(await inspected().findByTitle('Model: gpt-6-astra')).toBeVisible();
    expect(inspected().getByText('100 compact')).toBeVisible();
    expect(inspected().getByText(/Compaction coverage: complete/)).toBeVisible();
    expect(document.querySelector('[data-job-id]')).toHaveAttribute('data-quality', 'degraded');
    fireEvent.click(inspected().getByRole('button', { name: /Reviewer/ }));
    const worker = within(inspected().getByRole('button', { name: /Reviewer/ }).closest('[data-task-id]')!);
    expect(worker.getByText('Inspect the real diff; token=REDACTED')).toBeVisible();
    expect(worker.getByText('smaller-model: insufficient context')).toBeVisible();
    expect(worker.getByText('Expected two rows; got one')).toBeVisible();
    fireEvent.click(worker.getByRole('button', { name: 'Show tokens and cost' }));
    expect(worker.getByText('Provider-reported cost $0')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Evidence', exact: true }));
    expect(within(screen.getByRole('region', { name: 'Job evidence' })).getByText('Recorded checks failed: 1')).toBeVisible();
  });
  it('renders actual producer live meters and exact zero in the selected inspector', async () => {
    const f = await setup();
    f.selected.mockImplementation(async s => ({ ...selected(facts(), s), expert: backend, context: f.context() }));
    render(<f.Provider><JobsInspectHarness><MetadataJobs /></JobsInspectHarness></f.Provider>);
    await expandAndInspect(/Audit auth flow/);
    expect(await inspected().findByText('120t')).toBeVisible();
    expect(inspected().getByRole('button', { name: 'Job cost' })).toHaveTextContent('$0');
    fireEvent.click(inspected().getByRole('button', { name: /Auditor/ }));
    fireEvent.click(inspected().getByRole('button', { name: 'Show tokens and cost' }));
    expect(screen.getByText('Provider-reported cost $0')).toBeVisible();
  });
  it('opens Evidence from the real expanded job detail', async () => {
    const f = await setup(); render(<f.Provider><JobsInspectHarness><MetadataJobs /></JobsInspectHarness></f.Provider>);
    await expandAndInspect(/Audit auth flow/);
    await inspected().findByTitle('Model: gpt-6-astra');
    fireEvent.click(inspected().getByRole('button', { name: 'Evidence', exact: true }));
    const evidence = screen.getByRole('region', { name: 'Job evidence' });
    expect(within(evidence).getByText('Recorded checks passed: 1')).toBeVisible();
    fireEvent.click(within(evidence).getByText('Regression checks'));
    expect(within(evidence).getByText('Tests passed')).toBeVisible();
    expect(f.request.mock.calls.some(([, path]) => path.includes('/evidence'))).toBe(false);
  });
  it('applies routing-only and failure-detail poll updates when counts and task state stay fixed', async () => {
    const f = await setup();
    const first = facts(); first.artifacts.push({ ...first.artifacts[0], id: 'route', type: 'routing', created_by: 'router-escalation', model: 'gpt-6-astra', headline: '', detail: null, result: null, check_result: 'unavailable' });
    f.selected.mockImplementation(async s => ({ ...selected(first, s), context: f.context() }));
    render(<f.Provider><JobsInspectHarness><MetadataJobs /></JobsInspectHarness></f.Provider>);
    await expandAndInspect(/Audit auth flow/);
    await inspected().findByTitle('Model: gpt-6-astra');
    const changed = facts('gpt-6-astra', 'Credential expired');
    changed.artifacts.push({ ...changed.artifacts[0], id: 'route', type: 'routing', created_by: 'router-escalation', model: 'gpt-6-astra-low', headline: '', detail: null, failure: null, result: null, check_result: 'unavailable' });
    changed.quality = 'degraded'; changed.artifacts[0].check_result = 'failed'; changed.artifacts[0].failure = 'auth_failure';
    f.selected.mockImplementation(async s => ({ ...selected(changed, s, 101), context: f.context() }));
    await act(async () => { await f.store.readDetail(); });
    expect(inspected().getByTitle('Model: gpt-6-astra-low')).toBeVisible();
    expect(inspected().queryByTitle('Model: gpt-6-astra')).toBeNull();
    fireEvent.click(inspected().getByRole('button', { name: /Auditor/ }));
    expect(within(inspected().getByRole('button', { name: /Auditor/ }).closest('[data-task-id]')!).getByText('Credential expired')).toBeVisible();
    expect(document.querySelector('[data-job-id]')).toHaveAttribute('data-quality', 'degraded');
    expect(screen.queryByText('Tests passed')).toBeNull();
  });
  it('hydrates colliding sources independently in the same live snapshot', async () => {
    const a = selection(), b: MetadataSelection = { ...a, source: 'cli', job_ref: { ...a.job_ref, state_id: 'store-B' } };
    const f = await setup([expertSummary(a, 'Harness evidence'), expertSummary(b, 'CLI evidence')]);
    f.selected.mockImplementation(async s => ({ ...selected(facts(s.source, s.source + ' body'), s), context: f.context() }));
    await act(async () => { f.store.select(a); await f.store.readDetail(); });
    await act(async () => { f.store.select(b); await f.store.readDetail(); });
    expect(currentExpert(f.store.getSnapshot(), metadataSelectionKey(a))?.tasks[0].model).toBe('harness');
    expect(currentExpert(f.store.getSnapshot(), metadataSelectionKey(b))?.tasks[0].model).toBe('cli');
    render(<f.Provider><JobsInspectHarness><MetadataJobs /></JobsInspectHarness></f.Provider>);
    inspectHarnessJob('harness', a.job_ref.job_id);
    inspectHarnessJob('cli', b.job_ref.job_id);
    expect(within(screen.getByTestId(`inspect-harness-${a.job_ref.job_id}`)).getAllByTitle('Model: harness')[0]).toBeVisible();
    expect(within(screen.getByTestId(`inspect-cli-${b.job_ref.job_id}`)).getAllByTitle('Model: cli')[0]).toBeVisible();
    await act(async () => f.store.invalidate());
    expect(currentExpert(f.store.getSnapshot(), metadataSelectionKey(a))).toBeUndefined();
  });
  it('isolates a header read failure from source discovery and selected facts', async () => {
    const f = await setup();
    await act(async () => { f.store.select(selection()); await f.store.readDetail(); });
    const original = f.request.getMockImplementation()!;
    f.request.mockImplementation(async (method, path, body) => {
      if (path.endsWith('/pins')) throw Error('Header service temporarily unavailable');
      return original(method, path, body);
    });
    await act(async () => { await f.store.refreshHeaders(); });
    expect(f.store.getSnapshot().headerError).not.toBeNull();
    expect(f.store.getSnapshot().observations[0].freshness).toBe('observed');
    expect(currentExpert(f.store.getSnapshot(), metadataSelectionKey(selection()))?.tasks[0].model).toBe('gpt-6-astra');
  });
  it('keeps the last header when a later pin row arrives without one', async () => {
    const f = await setup(); const original = f.request.getMockImplementation()!;
    const row = expertSummary(selection(), 'Audit auth flow');
    let withHeader = true;
    f.request.mockImplementation(async (method, path, body) => {
      if (!path.endsWith('/pins')) return original(method, path, body);
      return { version: 1, context: f.context(), results: [{ selection: row.selection, result: { kind: 'present',
        row: withHeader ? { ...row, header: facts().header } : row } }] };
    });
    await act(async () => { await f.store.refreshHeaders(); });
    const key = metadataSelectionKey(selection());
    const header = currentHeader(f.store.getSnapshot(), key);
    expect(header).toBeDefined();
    // The server skipped this header (deadline or a locked store read); the counts stay.
    withHeader = false;
    vi.spyOn(Date, 'now').mockReturnValue(Date.now() + 60000);
    await act(async () => { await f.store.refreshHeaders(); });
    expect(f.store.getSnapshot().headerError).toBeNull();
    expect(currentHeader(f.store.getSnapshot(), key)).toEqual(header);
  });
  it('updates job cost and savings while worker and artifact counts remain fixed', async () => {
    const f = await setup();
    const expert = parseExpertMetadata(backend);
    if (!expert.live_economics?.savings || !expert.live_economics.usage) throw Error('Producer economics missing');
    expert.live_economics.savings = { ...expert.live_economics.savings, routing_usd: .02, selected_usd: .02 };
    expert.live_economics.cost = { ...expert.live_economics.cost, selected_usd: 1.5, measured_cost_usd: 1.25, estimated_cost_usd: .25, basis: 'mixed' };
    f.selected.mockImplementation(async s => ({ ...selected(expert, s), context: f.context() }));
    render(<f.Provider><JobsInspectHarness><MetadataJobs /></JobsInspectHarness></f.Provider>);
    await expandAndInspect(/Audit auth flow/);
    expect(await inspected().findByText('Estimated savings ~$0.0200')).toBeVisible();
    expect(inspected().queryByText('Measured')).toBeNull();
    fireEvent.click(inspected().getByRole('button', { name: 'Job cost' }));
    expect(screen.getByText('Measured')).toBeVisible();
    expect(screen.getByText('$1.25')).toBeVisible();
    expect(screen.getByText('$0.25')).toBeVisible();
    expert.live_economics.savings = { ...expert.live_economics.savings, routing_usd: .11, selected_usd: .11 };
    expert.live_economics.cost = { ...expert.live_economics.cost, selected_usd: 2.5, measured_cost_usd: 2.25 };
    await act(async () => { await f.store.readDetail(); });
    expect(screen.getByText('Estimated savings ~$0.1100')).toBeVisible();
    expect(screen.getByText('$2.25')).toBeVisible();
    expect(screen.queryByText('$1.25')).toBeNull();
    expect(screen.queryByText('Estimated savings ~$0.0200')).toBeNull();
  });
  it('rejects a regressing selected revision and keeps the last observation visibly stale', async () => {
    const f = await setup();
    await act(async () => { f.store.select(selection()); await f.store.readDetail(); });
    f.selected.mockImplementation(async s => ({ ...selected(facts('old'), s, 99), context: f.context() }));
    await act(async () => { await f.store.readDetail(); });
    expect(f.store.getSnapshot().detail).toMatchObject({ freshness: 'stale', observation: { expert: { tasks: [{ model: 'gpt-6-astra' }] } } });
    expect(currentExpert(f.store.getSnapshot(), metadataSelectionKey(selection()))).toBeUndefined();
  });
  it('sorts active and finished jobs newest-first by creation time with explicit batches of at most eight', async () => {
    const rows = Array.from({ length: 10 }, (_, i) => expertSummary(selection(i + 1), `Job ${i + 1}`));
    const f = await setup(rows); const original = f.request.getMockImplementation()!;
    const sizes: number[] = [];
    f.request.mockImplementation(async (method, path, body) => {
      if (!path.endsWith('/pins')) return original(method, path, body);
      if (!body || typeof body !== 'object' || !('selections' in body) || !Array.isArray(body.selections)) throw Error('Missing bounded selection');
      const requested = body.selections.map(s => rows.find(r => metadataSelectionKey(r.selection) === metadataSelectionKey(s))!);
      sizes.push(requested.length);
      return { version: 1, context: f.context(), results: requested.map(row => ({ selection: row.selection, result: { kind: 'present', row: { ...row,
        header: { ...facts().header, created_at: `2026-09-${row.selection.job_ref.job_id === 'job_1' ? '01' : '07'}T12:00:00Z` } } } })) };
    });
    await act(async () => { await f.store.refreshHeaders(); await f.store.refreshHeaders(); });
    expect(sizes).toEqual([8, 2]);
    expect(Object.keys(f.store.getSnapshot().headers)).toHaveLength(10);
    expect(currentHeader(f.store.getSnapshot(), metadataSelectionKey(selection()))?.created_at).toContain('09-01');
    render(<f.Provider><JobsInspectHarness><MetadataJobs /></JobsInspectHarness></f.Provider>);
    const before = () => !!(screen.getByRole('button', { name: 'Job 2 · running' }).compareDocumentPosition(screen.getByRole('button', { name: 'Job 1 · running' })) & Node.DOCUMENT_POSITION_FOLLOWING);
    expect(before()).toBe(true);
    fireEvent.change(screen.getByLabelText('Sort jobs'), { target: { value: 'oldest' } }); expect(before()).toBe(false);
  });
});

function route(model: string | null, patch: Partial<ExpertArtifact> = {}): ExpertArtifact {
  return { ...facts().artifacts[0], id: 'route', type: 'routing', created_by: 'router', headline: '', detail: null,
    result: null, failure: null, check_result: 'unavailable', model, ...patch };
}
describe('original worker routing requirements through the selected store', () => {
  const scenarios = [
    { name: "prefers final associated ROUTING over a stale task.model preview", routes: [route('final-model')], model: 'final-model' },
    { name: "shows one final model per worker when router and router-fallback both exist", routes: [route('first'), route('fallback', { id: 'fallback', created_by: 'router-fallback' })], model: 'fallback' },
    { name: "prefers router-escalation over fallback for the worker model", routes: [route('escalated', { created_by: 'router-escalation' }), route('fallback', { id: 'fallback', created_by: 'router-fallback' })], model: 'escalated' },
    { name: "shows a routed model as soon as ROUTING resolves by role", routes: [route('role-model', { task_id: null, role: 'Auditor' })], model: 'role-model' },
    { name: "prefers task.model over an engine-only ROUTING stamp while live", routes: [route('codex')], model: 'gpt-6-astra' },
  ];
  it.each(scenarios)('$name', async ({ routes, model }) => {
    const f = await setup(); const expert = facts(); expert.artifacts.push(...routes);
    f.selected.mockImplementation(async s => ({ ...selected(expert, s), context: f.context() }));
    render(<f.Provider><JobsInspectHarness><MetadataJobs /></JobsInspectHarness></f.Provider>);
    await expandAndInspect(/Audit auth flow/);
    expect(await inspected().findByTitle(`Model: ${model}`)).toBeVisible();
    expect(document.querySelectorAll('[data-worker-model-slot]')).toHaveLength(1);
  });
  it('keeps the model slot distinct from a long role and hides collapsed instructions and alternatives', async () => {
    const f = await setup(); const expert = facts(); expert.tasks[0].role = 'A very long worker role describing the consumer integration';
    expert.artifacts.push(route('gpt-6-astra', { policy: 'explicit_pin', provider: 'openai', rejected: [{ model: 'smaller', reason: 'context too short' }] }));
    f.selected.mockImplementation(async s => ({ ...selected(expert, s), context: f.context() }));
    render(<f.Provider><JobsInspectHarness><MetadataJobs /></JobsInspectHarness></f.Provider>);
    await expandAndInspect(/Audit auth flow/);
    await inspected().findByTitle('Model: gpt-6-astra');
    expect(screen.queryByText('Exact current instruction')).toBeNull();
    expect(screen.queryByText('smaller: context too short')).toBeNull();
    expect(inspected().getByTitle('Model: gpt-6-astra')).toHaveClass('min-w-0', 'break-words', '[overflow-wrap:anywhere]');
    fireEvent.click(inspected().getByRole('button', { name: /A very long worker role/ }));
    expect(screen.getByText('Exact current instruction')).toBeVisible();
    expect(screen.getByText('Policy: explicit_pin')).toBeVisible();
    expect(screen.getByText('smaller: context too short')).toBeVisible();
  });
  it('chooses escalation over fallback for a zero-task job header model', async () => {
    const f = await setup(); const expert = facts(); expert.tasks = []; expert.quality = 'unverified';
    expert.artifacts = [route('fallback', { task_id: null, created_by: 'router-fallback' }), route('escalated', { id: 'escalation', task_id: null, created_by: 'router-escalation' })];
    f.selected.mockImplementation(async s => ({ ...selected(expert, s), context: f.context() }));
    render(<f.Provider><JobsInspectHarness><MetadataJobs /></JobsInspectHarness></f.Provider>);
    await expandAndInspect(/Audit auth flow/);
    expect(await screen.findByTitle('Model: escalated')).toBeVisible();
    expect(screen.queryByTitle('Model: fallback')).toBeNull();
    expect(document.querySelectorAll('[data-worker-model-slot]')).toHaveLength(0);
  });
  it('shows each worker own model and adapter without inheriting another worker model', async () => {
    const f = await setup(); const expert = facts();
    expert.tasks.push({ ...expert.tasks[0], id: 'task-2', role: 'Implementer', model: null, adapter: 'openrouter' });
    expert.quality = 'unverified';
    f.selected.mockImplementation(async s => ({ ...selected(expert, s), context: f.context() }));
    render(<f.Provider><JobsInspectHarness><MetadataJobs /></JobsInspectHarness></f.Provider>);
    await expandAndInspect(/Audit auth flow/);
    expect(await inspected().findByTitle('Model: gpt-6-astra')).toBeVisible();
    expect(inspected().getByTitle('Model: openrouter')).toBeVisible();
    expect(document.querySelectorAll('[data-worker-model-slot]')).toHaveLength(2);
  });
  it.each(['pending', 'queued', 'complete'])('keeps %s worker model state distinct from the job model', async status => {
    const f = await setup(); const expert = facts(); expert.tasks[0].model = null;
    f.selected.mockImplementation(async s => { const value = selected(expert, s); value.tasks.rows[0].status = status; return { ...value, context: f.context() }; });
    render(<f.Provider><JobsInspectHarness><MetadataJobs /></JobsInspectHarness></f.Provider>);
    await expandAndInspect(/Audit auth flow/);
    expect(await inspected().findByText(status === 'complete' ? 'No model recorded' : 'routing…')).toBeVisible();
    expect(document.querySelectorAll('[data-worker-model-slot]')).toHaveLength(1);
    expect(screen.queryByTitle('Model: gpt-6-astra')).toBeNull();
  });
  it('keeps job-level routing only when there are zero task rows', async () => {
    const f = await setup(); const expert = facts(); expert.tasks = []; expert.artifacts = []; expert.quality = 'unverified';
    f.selected.mockImplementation(async s => ({ ...selected(expert, s), context: f.context() }));
    render(<f.Provider><JobsInspectHarness><MetadataJobs /></JobsInspectHarness></f.Provider>);
    await expandAndInspect(/Audit auth flow/);
    expect(await screen.findByText(/routing…/)).toBeVisible();
    expect(document.querySelectorAll('[data-worker-model-slot]')).toHaveLength(0);
  });
  it('warns prompt-echo headlines without rewriting them and shows only grouped Findings counts', async () => {
    const f = await setup(); const expert = facts();
    const headline = 'Your task is to audit the selected consumer implementation';
    expert.artifacts = Array.from({ length: 4 }, (_, i) => ({ ...expert.artifacts[0], id: `finding-${i}`, type: 'finding', headline, check_result: 'unavailable' as const, result: null }));
    expert.quality = 'unverified';
    f.selected.mockImplementation(async s => ({ ...selected(expert, s), context: f.context() }));
    render(<f.Provider><JobsInspectHarness><MetadataJobs /></JobsInspectHarness></f.Provider>);
    await expandAndInspect(/Audit auth flow/);
    expect(await screen.findByText('Findings (1)')).toBeVisible();
    expect(inspected().getByText(headline, { exact: false })).toBeVisible();
    expect(screen.getByText('looks like prompt echo')).toBeVisible();
    expect(screen.queryByText('Findings (4)')).toBeNull();
    fireEvent.click(screen.getByText('Findings (1)'));
    expect(inspected().getByText(headline, { exact: false })).not.toBeVisible();
    fireEvent.click(screen.getByText('Findings (1)'));
    expect(inspected().getByText(headline, { exact: false })).toBeVisible();
  });
  it('does not paint workers degraded from job-level degraded without a task-scoped artifact', async () => {
    const f = await setup(); const expert = facts(); expert.quality = 'degraded';
    expert.artifacts.push({ ...expert.artifacts[0], id: 'job-check', task_id: null, check_result: 'failed', detail: 'Job-level verification failed' });
    f.selected.mockImplementation(async s => ({ ...selected(expert, s), context: f.context() }));
    render(<f.Provider><JobsInspectHarness><MetadataJobs /></JobsInspectHarness></f.Provider>);
    await expandAndInspect(/Audit auth flow/);
    await inspected().findByTitle('Model: gpt-6-astra');
    expect(document.querySelector('[data-job-id]')).toHaveAttribute('data-quality', 'degraded');
    expect(inspected().getByRole('button', { name: /Auditor/ }).closest('[data-task-id]')).toHaveAttribute('data-quality', 'ok');
  });
});

describe('positive owner polling and narrow worker layouts', () => {
  it('updates current worker model automatically on a later owner poll with unchanged task facts', async () => {
    const f = await setup(); const expert = facts();
    expert.artifacts.push(route('initial-model'));
    f.selected.mockImplementation(async s => ({ ...selected(expert, s), context: f.context() }));
    render(<f.Provider><JobsInspectHarness><MetadataJobs /></JobsInspectHarness></f.Provider>);
    await expandAndInspect(/Audit auth flow/);
    expect(await inspected().findByTitle('Model: initial-model')).toBeVisible();
    const next = facts(); next.artifacts.push(route('settled-model'));
    f.selected.mockImplementation(async s => ({ ...selected(next, s), context: f.context() }));
    const later = Date.now() + 5000; vi.spyOn(Date, 'now').mockReturnValue(later);
    for (let i = 0; i < 16; i++) await act(async () => { await f.store.ownerTick(); });
    expect(inspected().getByTitle('Model: settled-model')).toBeVisible();
    expect(inspected().queryByTitle('Model: initial-model')).toBeNull();
    expect(next.tasks).toEqual(expert.tasks);
  });
  it.each([320, 220])('keeps current role and model separate and breakable in a %ipx rail', async width => {
    const f = await setup(); const expert = facts();
    expert.tasks[0].role = 'Very long independently wrapping auditor role';
    expert.tasks[0].model = 'provider/an-extremely-long-current-assigned-model-identifier-with-context';
    f.selected.mockImplementation(async s => ({ ...selected(expert, s), context: f.context() }));
    render(<div style={{ width }}><f.Provider><JobsInspectHarness><MetadataJobs /></JobsInspectHarness></f.Provider></div>);
    await expandAndInspect(/Audit auth flow/);
    const inspector = await screen.findByRole('region', { name: 'Selected job inspector' });
    const model = await within(inspector).findByTitle(`Model: ${expert.tasks[0].model}`);
    expect(model).toBeVisible();
    expect(model).toHaveClass('min-w-0', 'break-words', '[overflow-wrap:anywhere]');
    const worker = model.closest('button')!;
    expect(worker).toHaveClass('grid-cols-[minmax(0,1fr)_minmax(0,1fr)]');
    expect(within(worker).getByText(expert.tasks[0].role, { exact: false })).toBeVisible();
  });
  it('keeps a trustworthy completed current job green and replaces it with exact degraded evidence', async () => {
    const f = await setup([{ ...expertSummary(selection(), 'Completed audit'), lifecycle: 'complete' }]);
    const expert = facts();
    f.selected.mockImplementation(async s => ({ ...selected(expert, s), lifecycle: 'complete', context: f.context() }));
    render(<f.Provider><JobsInspectHarness><MetadataJobs /></JobsInspectHarness></f.Provider>);
    await expandAndInspect(/Completed audit/);
    await screen.findAllByTitle('Model: gpt-6-astra');
    expect(screen.getByRole('button', { name: 'Completed audit · complete' })).toHaveTextContent('done');
    const changed = facts('gpt-6-astra', 'Broken after update'); changed.quality = 'degraded'; changed.artifacts[0].check_result = 'failed';
    f.selected.mockImplementation(async s => ({ ...selected(changed, s), lifecycle: 'complete', context: f.context() }));
    await act(async () => { await f.store.readDetail(); });
    const job = screen.getByRole('button', { name: 'Completed audit · complete' });
    expect(job).toHaveTextContent('degraded');
    expect(within(job).getByText('degraded')).toHaveClass('text-warn');
    expect(job).not.toHaveTextContent('done');
    expect(screen.queryByText('Tests passed')).toBeNull();
  });
});

it('suppresses only the duplicate unmatched header route while workers keep their own models', async () => {
  const f = await setup(); const expert = facts();
  expert.tasks.push({ ...expert.tasks[0], id: 'task-2', role: 'Implementer', model: 'worker-two' });
  expert.artifacts.push(route('job-model', { task_id: null }));
  f.selected.mockImplementation(async s => ({ ...selected(expert, s), context: f.context() }));
  const original = f.request.getMockImplementation()!;
  f.request.mockImplementation(async (method, path, body) => {
    if (!path.endsWith('/pins')) return original(method, path, body);
    const row = expertSummary(selection(), 'Audit auth flow');
    return { version: 1, context: f.context(), results: [{ selection: row.selection, result: { kind: 'present', row: {
      ...row, header: { ...backend.economics.header, model: 'job-model', model_provenance: 'job_routing' },
    } } }] };
  });
  for (let i = 0; i < 16; i++) await act(async () => { await f.store.ownerTick(); });
  render(<f.Provider><JobsInspectHarness><MetadataJobs /></JobsInspectHarness></f.Provider>);
  expect(screen.getByTitle('Model: job-model')).toBeVisible();
  await expandAndInspect(/Audit auth flow/);
  const inspector = await screen.findByRole('region', { name: 'Selected job inspector' });
  expect(await within(inspector).findByTitle('Model: worker-two')).toBeVisible();
  expect(within(inspector).getByTitle('Model: gpt-6-astra')).toBeVisible();
  expect(screen.getByTitle('Model: job-model')).toBeVisible();
  expect(screen.queryByText(/Unmatched routing/)).toBeNull();
});

it('keeps each worker token and cost disclosure separate with compact formatting', async () => {
  const f = await setup(); const expert = facts();
  expert.tasks[0].usage = { tokens_in: 120000, tokens_out: 0, est_cost_usd: .14, estimated: true, cost_provenance: 'estimate' };
  expert.tasks.push({ ...expert.tasks[0], id: 'task-2', role: 'Reviewer', usage: { tokens_in: 60000, tokens_out: 0, est_cost_usd: .07, estimated: true, cost_provenance: 'estimate' } });
  f.selected.mockImplementation(async s => ({ ...selected(expert, s), context: f.context() }));
  render(<f.Provider><JobsInspectHarness><MetadataJobs /></JobsInspectHarness></f.Provider>);
  await expandAndInspect(/Audit auth flow/);
  await screen.findAllByText('Workers (2)');
  expect(screen.queryByText('120,000t')).toBeNull();
  const tokensInspector = screen.getByRole('region', { name: 'Selected job inspector' });
  for (const [role, tokens, cost] of [['Auditor', '120,000t', 'Estimated cost ~$0.1400'], ['Reviewer', '60,000t', 'Estimated cost ~$0.0700']]) {
    fireEvent.click(within(tokensInspector).getByRole('button', { name: new RegExp(role) }));
    const worker = within(within(tokensInspector).getByRole('button', { name: new RegExp(role) }).closest('[data-task-id]')!);
    expect(worker.queryByText(tokens)).toBeNull();
    fireEvent.click(worker.getByRole('button', { name: 'Show tokens and cost' }));
    expect(worker.getByText(tokens)).toBeVisible();
    expect(worker.getByText(cost)).toBeVisible();
  }
});

it('paints mixed completed current workers as degraded without contaminating the passing worker', async () => {
  const f = await setup([{ ...expertSummary(selection(), 'Mixed audit'), lifecycle: 'complete' }]);
  const expert = facts();
  expert.tasks.push({ ...expert.tasks[0], id: 'task-2', role: 'Passing reviewer' });
  expert.artifacts.push({ ...expert.artifacts[0], id: 'check-2', task_id: 'task-2' });
  expert.artifacts[0].check_result = 'failed'; expert.artifacts[0].result = 'degraded';
  f.selected.mockImplementation(async s => {
    const d = selected(expert, s);
    return { ...d, lifecycle: 'complete', context: f.context(), tasks: { ...d.tasks, rows: d.tasks.rows.map(t => ({ ...t, status: 'complete' })) } };
  });
  render(<f.Provider><JobsInspectHarness><MetadataJobs /></JobsInspectHarness></f.Provider>);
  await expandAndInspect(/Mixed audit/);
  await screen.findAllByText('Workers (2)');
  const job = screen.getByRole('button', { name: 'Mixed audit · complete' });
  expect(within(job).getByText('degraded')).toHaveClass('text-warn');
  expect(job.querySelector('.text-good')).toBeNull();
  expect(document.querySelector('[data-task-id="task-1"]')).toHaveAttribute('data-quality', 'degraded');
  expect(document.querySelector('[data-task-id="task-2"]')).toHaveAttribute('data-quality', 'ok');
});
