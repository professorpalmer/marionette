import backendWire from './fixtures/pm125-selected-metadata.json';
import { act, fireEvent, render, screen, within, waitFor } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import SwarmPane from '../components/SwarmPane';
import { metadataSelectionKey, mergeMetadataRows, parseMetadataDetail, parseMetadataPins, parseMetadataSelection } from '../lib/jobMetadata';
import type { MetadataDetail, MetadataSelection } from '../lib/jobMetadata';
import { parseCancellationResult, selectJobControl } from '../lib/jobControl';
import { jobArtifactKey } from '../lib/jobArtifacts';
import { expertDetail, expertMetadataFixture, expertSummary } from './metadataExpert.fixtures';
import { context, token } from './jobMetadata.fixtures';
import type { HistoryLane, SelectedEconomics, SelectedHistory, SelectedMetric } from '../lib/selectedMetadataEvidence';

const selected: MetadataSelection = { repo: context.repo, session_id: context.session_id, source: 'harness',
  job_ref: { job_id: 'job_expert', state_id: 'store-A', version: 2, incarnation: '12345678-1234-4234-8234-123456789abc' } };
const other: MetadataSelection = { ...selected, job_ref: { ...selected.job_ref, version: 2, incarnation: '22345678-1234-4234-8234-123456789abc' } };
const unknown: SelectedMetric = { total: null, state: 'unknown', known_selected: null, unknown_selected: null, estimated_selected: null, conflicting_selected: null };
const measuredZero: SelectedMetric = { total: 0, state: 'measured', known_selected: 1, unknown_selected: 0, estimated_selected: 0, conflicting_selected: 0 };
function cost(selection = selected): SelectedEconomics {
  return { kind: 'available', job_ref: selection.job_ref, outcome: 'available', source: 'terminal_receipt', coverage: 'selected_receipt',
    summary_revision: 8, receipt_digest: 'a'.repeat(64), selected_count: 1, reason: null, retry_after_ms: null,
    totals: { tokens_in: unknown, tokens_out: unknown, cache_read_tokens: unknown, cache_write_tokens: unknown,
      api_cost_usd: unknown, plan_marginal_cost_usd: measuredZero, api_equivalent_cost_usd: { ...measuredZero, total: 7, state: 'estimated', estimated_selected: 1 } } };
}
function history(selection = selected): SelectedHistory {
  const lane: HistoryLane = { page: { outcome: 'complete', next_cursor: null, scanned: 0, captured_count: 0, coverage: 'captured', complete_invocation_history: false }, rows: [] };
  return { kind: 'available', counts: { captured_attempts: 2, captured_runs: 0, captured_process_outcomes: 0, captured_observations: 0,
    outcome: 'available', coverage: 'captured', complete_invocation_history: false },
    attempts: { page: { ...lane.page, outcome: 'partial', next_cursor: token(), scanned: 1, captured_count: 2 }, rows: [
      { job_ref: selection.job_ref, kind: 'attempt', sequence: 1, facts: { attempt_id: 'attempt-1', adapter: 'codex', model: 'recorded-model' } },
    ] }, runs: lane, observations: lane, process_outcomes: lane };
}
function selectedDetail(selection = selected): MetadataDetail {
  return { ...expertDetail(selection, context), display: expertSummary(selection, 'Selected expert job').display,
    task_count: 1, artifact_count: 1, history: history(selection), cost: cost(selection) };
}
let fixture: Awaited<ReturnType<typeof expertMetadataFixture>> | undefined;
afterEach(() => { fixture?.dispose(); fixture = undefined; localStorage.clear(); });

it('keeps legacy identity explicit and carries v2 identity through parsing, cache keys and pins', () => {
  const legacy = { ...selected, job_ref: { job_id: selected.job_ref.job_id, state_id: selected.job_ref.state_id } };
  expect(parseMetadataSelection(legacy, context)).toEqual(legacy);
  expect(parseMetadataSelection(selected, context)).toEqual(selected);
  expect(metadataSelectionKey(selected)).not.toBe(metadataSelectionKey(other));
  expect(jobArtifactKey(selected)).not.toBe(jobArtifactKey(other));
  expect(metadataSelectionKey(selected)).not.toBe(metadataSelectionKey(legacy));
  const rows = mergeMetadataRows([], [expertSummary(selected, 'one'), expertSummary(other, 'two')]);
  expect(rows.observations).toHaveLength(2);
  expect(parseMetadataPins({ version: 1, context, results: [{ selection: selected, result: { kind: 'present', row: expertSummary(selected, 'one') } }] }, context, [selected]).results[0].selection).toEqual(selected);
  expect(() => parseMetadataPins({ version: 1, context, results: [{ selection: other, result: { kind: 'present', row: expertSummary(other, 'two') } }] }, context, [selected])).toThrow();
  for (const job_ref of [{ ...selected.job_ref, version: 3 }, { job_id: 'job_expert', state_id: 'store-A', incarnation: 'orphan' }, { ...selected.job_ref, incarnation: '' }])
    expect(() => parseMetadataSelection({ ...selected, job_ref }, context)).toThrow();
});

it('rejects a history or economics receipt from a different incarnation and refuses invented unknown zero', () => {
  expect(parseMetadataDetail(selectedDetail(), context, selected, { task_cursor: null, artifact_cursor: null }).history.kind).toBe('available');
  expect(() => parseMetadataDetail({ ...selectedDetail(), cost: cost(other) }, context, selected, { task_cursor: null, artifact_cursor: null })).toThrow();
  expect(() => parseMetadataDetail({ ...selectedDetail(), history: history(other) }, context, selected, { task_cursor: null, artifact_cursor: null })).toThrow();
  const economics = cost();
  expect(() => parseMetadataDetail({ ...selectedDetail(), cost: { ...economics, totals: { ...economics.totals, api_cost_usd: { ...unknown, total: 0 } } } }, context, selected, { task_cursor: null, artifact_cursor: null })).toThrow();
});

it('preserves incarnation on scoped control requests and validates both acknowledgement identities', () => {
  const bindings = [{ task_id: 'task', generation: 1, lease_id: 'lease', owner: 'owner' }];
  const control = selectJobControl({ id: selected.job_ref.job_id, job_ref: selected.job_ref, source: 'harness', session_id: context.session_id,
    goal: 'expert', status: 'running', cancellation_view: { status: 'complete', limit: 200, bindings } }, context.repo, context.session_id);
  expect(control?.job_ref).toEqual(selected.job_ref);
  expect(selectJobControl({ id: selected.job_ref.job_id, job_ref: { job_id: selected.job_ref.job_id, state_id: selected.job_ref.state_id }, source: 'harness', session_id: context.session_id,
    goal: 'legacy', status: 'running', cancellation_view: { status: 'complete', limit: 200, bindings } }, context.repo, context.session_id)).toBeNull();
  if (!control || control.source === 'local') throw Error('Expected scoped PM control');
  const request = { selection: control, request_id: 'request-1' };
  const receipt = { job_ref: selected.job_ref, request_id: request.request_id, bindings, outcome: 'requested', revision: 1, cleanup: 'unknown' };
  expect(parseCancellationResult({ ok: true, selection: control, request_id: request.request_id, receipt }, request).receipt.job_ref).toEqual(selected.job_ref);
  expect(() => parseCancellationResult({ ok: true, selection: { ...control, job_ref: other.job_ref }, request_id: request.request_id, receipt }, request)).toThrow();
  expect(() => parseCancellationResult({ ok: true, selection: control, request_id: request.request_id, receipt: { ...receipt, job_ref: other.job_ref } }, request)).toThrow();
});

it('opens bounded selected panels with explicit unknowns and pages only the requested history lane', async () => {
  fixture = await expertMetadataFixture([expertSummary(selected, 'Expert inspection')]);
  const f = fixture;
  f.selected.mockImplementation(async (selection, cursors) => {
    const result = { ...selectedDetail(selection), context: f.context() };
    if (cursors.attempt_cursor && result.history.kind === 'available') result.history.attempts = {
      page: { ...result.history.attempts.page, outcome: 'complete', next_cursor: null }, rows: [{ job_ref: selection.job_ref, kind: 'attempt', sequence: 2, facts: { attempt_id: 'attempt-2' } }],
    };
    return result;
  });
  render(<f.Provider><SwarmPane /></f.Provider>);
  fireEvent.click(await screen.findByRole('button', { name: /Expert inspection/ }));
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' })); });
  const inspector = await screen.findByRole('region', { name: 'Selected job inspector' });
  expect(within(inspector).getByText('task-1: running')).toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Stop selected workers' })).toHaveAttribute('aria-disabled', 'false');
  const detailURL = f.request.mock.calls.find(([, path]) => path.includes('/metadata/detail?'))?.[1];
  expect(detailURL).toContain('version=2'); expect(detailURL).toContain(`incarnation=${selected.job_ref.incarnation}`);
  fireEvent.click(within(inspector).getByRole('button', { name: 'Economics' }));
  expect(within(inspector).getByText('Selected API cost: unknown.')).toBeVisible();
  expect(within(inspector).getByText('Selected plan marginal cost: $0 (measured).')).toBeVisible();
  expect(within(inspector).getByText('API-equivalent estimate: $7 (estimated); not spend.')).toBeVisible();
  expect(within(inspector).getByText('All-attempt spend: unknown; invocation coverage is unverified.')).toBeVisible();
  fireEvent.click(within(inspector).getByRole('button', { name: 'Checks', exact: true }));
  expect(within(inspector).getByText(/Check assertions are unavailable/)).toBeVisible();
  fireEvent.click(within(inspector).getByRole('button', { name: 'Routing', exact: true }));
  expect(within(inspector).getByText(/model: recorded-model/)).toBeVisible();
  expect(f.selected).toHaveBeenCalledTimes(1);
  fireEvent.click(within(inspector).getByRole('button', { name: 'History', exact: true }));
  fireEvent.click(within(inspector).getByRole('button', { name: 'Next attempts' }));
  await waitFor(() => expect(f.selected).toHaveBeenCalledTimes(2));
  expect(f.selected.mock.calls[1][1]).toEqual({ task_cursor: null, artifact_cursor: null, attempt_cursor: token(), run_cursor: null, process_outcome_cursor: null, observation_cursor: null });
  await waitFor(() => expect(within(inspector).getByRole('button', { name: 'Next attempts' })).toBeDisabled());
  expect(f.store.getSnapshot().detail.kind).toBe('selected');
});

it('keeps two colliding selected inspections cached and does not consume a different selected cursor', async () => {
  const cli: MetadataSelection = { ...selected, source: 'cli', job_ref: { ...selected.job_ref, state_id: 'store-B' } };
  fixture = await expertMetadataFixture([expertSummary(selected, 'Harness selection'), expertSummary(cli, 'CLI selection')]);
  const f = fixture;
  f.selected.mockImplementation(async selection => ({ ...selectedDetail(selection), context: f.context() }));
  render(<f.Provider><SwarmPane /></f.Provider>);
  fireEvent.click(screen.getByRole('button', { name: /Harness selection/ }));
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' })); });
  await screen.findByRole('region', { name: 'Selected job inspector' });
  fireEvent.click(screen.getByRole('button', { name: /CLI selection/ }));
  const cliRow = screen.getByRole('button', { name: /CLI selection/ }).closest('[data-job-id]');
  if (!(cliRow instanceof HTMLElement)) throw Error('Missing CLI row');
  await act(async () => { fireEvent.click(within(cliRow).getByRole('button', { name: 'Inspect tasks and artifacts' })); });
  await waitFor(() => expect(screen.getAllByRole('region', { name: 'Selected job inspector' })).toHaveLength(2));
  expect(Object.keys(f.store.getSnapshot().detailCache)).toHaveLength(2);
  const row = screen.getByRole('button', { name: /Harness selection/ }).closest('[data-job-id]');
  if (!(row instanceof HTMLElement)) throw Error('Missing harness row');
  const first = within(row).getByRole('region', { name: 'Selected job inspector' });
  fireEvent.click(within(first).getByRole('button', { name: 'History', exact: true }));
  fireEvent.click(within(first).getByRole('button', { name: 'Next attempts' }));
  await waitFor(() => expect(f.selected).toHaveBeenCalledTimes(3));
  expect(f.selected.mock.calls[2][0]).toEqual(selected);
});

it('discards late history when the selected incarnation changes and clears caches on context invalidation', async () => {
  fixture = await expertMetadataFixture([expertSummary(selected, 'old'), expertSummary(other, 'new')]);
  const f = fixture;
  let finish: (value: MetadataDetail) => void = () => {};
  f.selected.mockImplementationOnce(() => new Promise(resolve => { finish = resolve; }));
  f.store.select(selected);
  const pending = f.store.readDetail();
  await act(async () => {});
  f.store.select(other);
  await act(async () => { finish({ ...selectedDetail(), context: f.context() }); await pending; });
  expect(f.store.getSnapshot().detail).toMatchObject({ selection: other, observation: null });
  expect(f.store.getSnapshot().detailCache).toEqual({});
  await act(async () => { await f.store.readDetail(); });
  expect(Object.keys(f.store.getSnapshot().detailCache)).toHaveLength(1);
  f.store.invalidate();
  expect(f.store.getSnapshot().detailCache).toEqual({});
});


it('parses the actual PM a213 selected response produced by the Python bridge', () => {
  const context = { ...backendWire.context, scope: 'session' } satisfies Parameters<typeof parseMetadataDetail>[1];
  const selection = parseMetadataSelection(backendWire.selection, context);
  const parsed = parseMetadataDetail(backendWire.detail, context, selection, { task_cursor: null, artifact_cursor: null });
  expect(parsed.selection.job_ref.version).toBe(2);
  expect(parsed.display).toMatchObject({ kind: 'available', delivery: 'unverified', quality: 'unverified' });
  expect(parsed.artifacts.rows[0].check_result).toBe('unavailable');
  expect(parsed.history.kind).toBe('available');
  if (parsed.history.kind === 'unavailable') throw Error('Real captured history was rejected');
  expect(parsed.history.counts.captured_attempts).toBe(3);
  expect(parsed.history.observations.rows[0].facts.tokens_out).toBe(0);
  expect(parsed.history.runs.rows[0].completion?.outcome).toBe('legacy_unknown');
  expect(parsed.cost.kind).toBe('available');
  if (!('source' in parsed.cost)) throw Error('Real economics was rejected');
  expect(parsed.cost.source).toBe('terminal_receipt');
  expect(parsed.cost.totals?.tokens_out).toMatchObject({ total: 0, state: 'measured' });
  expect(parsed.cost.totals?.cache_read_tokens.total).toBeNull();
});


it('pages captured Routing attempts through the bounded cursor and stops at the final page', async () => {
  fixture = await expertMetadataFixture([expertSummary(selected, 'Routing continuation')]);
  const f = fixture;
  f.selected.mockImplementation(async (selection, cursors) => {
    const result = { ...selectedDetail(selection), context: f.context() };
    if (result.history.kind === 'unavailable') throw Error('Missing history');
    result.history.attempts.rows[0].facts.started_at = '2026-09-07T12:00:00Z';
    if (cursors.attempt_cursor) result.history.attempts = {
      page: { ...result.history.attempts.page, outcome: 'complete', next_cursor: null },
      rows: [{ job_ref: selection.job_ref, kind: 'attempt', sequence: 2, facts: { attempt_id: 'next-capture', model: 'next-model' } }],
    };
    return result;
  });
  render(<f.Provider><SwarmPane /></f.Provider>);
  fireEvent.click(screen.getByRole('button', { name: /Routing continuation/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
  const inspector = await screen.findByRole('region', { name: 'Selected job inspector' });
  fireEvent.click(within(inspector).getByRole('button', { name: 'Routing', exact: true }));
  expect(screen.getByText('Captured started at: 2026-09-07T12:00:00Z')).toBeVisible();
  fireEvent.click(within(inspector).getByRole('button', { name: 'Next attempts' }));
  await waitFor(() => expect(screen.getByText(/model: next-model/)).toBeVisible());
  expect(f.selected.mock.calls[1][1]).toEqual({ task_cursor: null, artifact_cursor: null, attempt_cursor: token(), run_cursor: null, process_outcome_cursor: null, observation_cursor: null });
  expect(screen.queryByText(/model: recorded-model/)).not.toBeInTheDocument();
  expect(within(inspector).getByRole('button', { name: 'Next attempts' })).toBeDisabled();
});

it('discloses publication digest and exact-task captured usage without aggregating observations', async () => {
  fixture = await expertMetadataFixture([expertSummary(selected, 'Captured disclosures')]);
  const f = fixture;
  f.selected.mockImplementation(async selection => {
    const result = { ...selectedDetail(selection), context: f.context() };
    if (result.history.kind === 'unavailable') throw Error('Missing history');
    result.history.attempts.rows[0].facts = { attempt_id: 'attempt-1', task_id: 'task-1', run_id: 'run-1', started_at: 'captured-start' };
    const rows = [
      { observation_id: 'usage-one', task_id: 'task-1', run_id: 'run-1', identity_state: 'available', tokens_in: 17, cost_usd: 0, cost_state: 'measured', usage_state: 'available' },
      { observation_id: 'usage-two', task_id: 'task-1', run_id: 'run-2', identity_state: 'available', tokens_in: 19, cost_usd: null, cost_state: 'unknown', usage_state: 'available' },
      { observation_id: 'other-task', task_id: 'task-2', run_id: 'run-3', identity_state: 'available', tokens_in: 100 },
      { observation_id: 'unbound', task_id: 'task-1', run_id: 'run-1', identity_state: 'unavailable', tokens_in: 200 },
      { observation_id: 'missing-run', task_id: 'task-1', run_id: null, identity_state: 'available', tokens_in: 300 },
    ].map((facts, index) => ({ job_ref: selection.job_ref, kind: 'observation', sequence: index + 1,
      facts: { ...facts, attempt_id: `attempt-${index}`, observed_at: 'captured-observed', source: 'provider', cost_basis: 'provider_reported' } }));
    result.history.observations = { page: { ...result.history.observations.page, scanned: rows.length, captured_count: rows.length }, rows };
    result.history.runs = { page: { ...result.history.runs.page, scanned: 1, captured_count: 1 }, rows: [
      { job_ref: selection.job_ref, kind: 'run', sequence: 1, facts: { id: 'run-1', task_id: 'task-1' },
        completion: { job_ref: selection.job_ref, run_id: 'run-1', intent_digest: 'publication-digest', outcome: 'published' } },
    ] };
    return result;
  });
  render(<f.Provider><SwarmPane /></f.Provider>);
  fireEvent.click(screen.getByRole('button', { name: /Captured disclosures/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
  const inspector = await screen.findByRole('region', { name: 'Selected job inspector' });
  expect(screen.queryByRole('region', { name: 'Captured usage for task-1' })).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: 'task-1: running' }));
  expect(screen.getByText('Captured started at')).toBeVisible();
  expect(screen.getByText('captured-start')).toBeVisible();
  const usage = screen.getByRole('region', { name: 'Captured usage for task-1' });
  expect(within(usage).getAllByText(/^Captured usage observation /)).toHaveLength(2);
  expect(usage).not.toHaveTextContent('other-task');
  expect(usage).not.toHaveTextContent('unbound');
  expect(usage).not.toHaveTextContent('missing-run');
  expect(within(usage).getByText(/must not be summed/)).toBeVisible();
  fireEvent.click(within(usage).getByText('Captured usage observation usage-one'));
  expect(within(usage).getByText('17')).toBeVisible();
  expect(within(usage).getByText('0')).toBeVisible();
  expect(within(usage).getByText('measured')).toBeVisible();
  fireEvent.click(within(inspector).getByRole('button', { name: 'History', exact: true }));
  fireEvent.click(screen.getByText('run 1'));
  expect(screen.getByText('Publication intent digest')).toBeVisible();
  expect(screen.getByText('publication-digest')).toBeVisible();
  expect(screen.getByText(/Publication: published.*not a quality verdict/)).toBeVisible();
});
