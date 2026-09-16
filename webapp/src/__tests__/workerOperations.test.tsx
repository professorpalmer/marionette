import { act, cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import WorkerOperations from '../components/WorkerOperations';
import { fetchWorkerOperations, parseWorkerOperations } from '../lib/workerOperations';
import type { WorkerOperationsSnapshot } from '../lib/workerOperations';
import type { MetadataSelection } from '../lib/jobMetadata';
import { getJSON } from '../lib/transport';
import { context } from './jobMetadata.fixtures';

vi.mock('../lib/transport', () => ({ getJSON: vi.fn() }));
const selected: MetadataSelection = { repo: context.repo, session_id: context.session_id, source: 'harness',
  job_ref: { job_id: 'job_operations', state_id: 'store-A', version: 2, incarnation: '12345678-1234-4234-8234-123456789abc' } };
const other: MetadataSelection = { ...selected, job_ref: { ...selected.job_ref, version: 2, incarnation: '22345678-1234-4234-8234-123456789abc' } };
function snapshot(selection = selected): WorkerOperationsSnapshot {
  const unsupported = { state: 'unsupported', reason: 'public_operations_contract_required' } satisfies WorkerOperationsSnapshot['capabilities']['quality_loop'];
  return { version: 1, context, selection, kernel_version: '1.27.22', lifecycle: 'complete',
    capabilities: { verdicts: { state: 'available', reason: null }, quality_loop: unsupported, cleanup: unsupported,
      failure_routes: unsupported, steering: unsupported, claims: unsupported },
    ledger: { outcome: 'complete', rows: [], next_cursor: null, scanned: 0, coverage: 'page', reason: null },
    verdict: { state: 'missing', authority: 'worker_advisory' }, accounting: 'references_only' };
}
function withVerdict(value = 'PASS'): WorkerOperationsSnapshot {
  const snap = snapshot();
  if (value !== 'PASS' && value !== 'FAIL' && value !== 'PARTIAL' && value !== 'unknown') throw Error('test verdict');
  return { ...snap, verdict: { state: 'recorded', authority: 'worker_advisory' },
    ledger: { ...snap.ledger, scanned: 1, rows: [{ id: 'artifact-1', kind: 'verdict', task_id: 'task-1',
      created_at: '2026-09-16T00:00:00Z', verdict: value, reason: 'Seeded review', reason_truncated: false, advisory: true }] } };
}
function openOperations() {
  const summary = screen.getByText('Worker operations', { selector: 'summary' });
  const details = summary.closest('details');
  if (!details) throw Error('missing details');
  details.open = true;
  fireEvent(details, new Event('toggle'));
}
beforeEach(() => { vi.mocked(getJSON).mockReset(); });
afterEach(cleanup);

it('rejects other stores, reused job ids, sessions and ABA generations at the response boundary', () => {
  const original = snapshot();
  for (const value of [snapshot(other), { ...original, selection: { ...selected, source: 'cli' } },
    { ...original, context: { ...context, session_id: 'session-B' } },
    { ...original, context: { ...context, repo: '/different' } },
    { ...original, context: { ...context, view_generation: 'generation-2' } }]) {
    expect(() => parseWorkerOperations(value, context, selected)).toThrow();
  }
});

it('does not promote lifecycle done to PASS or accept an unknown protocol verdict', () => {
  expect(parseWorkerOperations(snapshot(), context, selected).verdict.state).toBe('missing');
  const value = withVerdict('unknown');
  expect(parseWorkerOperations(value, context, selected).ledger.rows[0].verdict).toBe('unknown');
  expect(() => parseWorkerOperations({ ...value, ledger: { ...value.ledger,
    rows: [{ ...value.ledger.rows[0], verdict: 'SUCCESS' }] } }, context, selected)).toThrow();
  expect(() => parseWorkerOperations({ ...value, accounting: 'add_to_job_total' }, context, selected)).toThrow();
});

it('rejects oversized ledgers, repeated cursors and contradictory receipt claims', () => {
  const value = withVerdict();
  for (const ledger of [
    { ...value.ledger, rows: Array(21).fill(value.ledger.rows[0]), scanned: 21 },
    { ...value.ledger, outcome: 'partial', next_cursor: 'cursor-1' },
    { ...value.ledger, outcome: 'unavailable' },
    { ...value.ledger, rows: [value.ledger.rows[0], value.ledger.rows[0]], scanned: 2 },
  ]) expect(() => parseWorkerOperations({ ...value, ledger }, context, selected, 'cursor-1')).toThrow();
});

it('captures the complete selection and context in the actual transport request', async () => {
  vi.mocked(getJSON).mockResolvedValue(snapshot());
  await fetchWorkerOperations(context, selected);
  const [path, transportContext] = vi.mocked(getJSON).mock.calls[0];
  const url = new URL(path, 'http://localhost');
  expect(url.searchParams.get('view_generation')).toBe(context.view_generation);
  expect(url.searchParams.get('incarnation')).toBe(selected.job_ref.incarnation);
  expect(url.searchParams.get('source')).toBe(selected.source);
  expect(transportContext).toEqual({ sessionId: context.session_id, repo: context.repo });
});

it('shows missing verdict separately from completion and keeps unsupported controls disabled', async () => {
  vi.mocked(getJSON).mockResolvedValue(snapshot());
  render(<WorkerOperations context={context} selection={selected} />);
  openOperations();
  expect(await screen.findByText(/No structured worker verdict recorded/)).toBeVisible();
  expect(screen.getByText(/Lifecycle: complete/)).toBeVisible();
  expect(screen.getByLabelText('Maximum iterations')).toHaveValue(3);
  for (const name of ['Start quality loop', 'Stop quality loop', 'Send to worker', 'Broadcast to live workers']) {
    const control = screen.getByRole('button', { name });
    expect(control).toBeDisabled();
    fireEvent.click(control);
  }
  expect(getJSON).toHaveBeenCalledTimes(1);
  expect(screen.getByText(/separate from the recurring pilot session loop/)).toBeVisible();
});

it('pages by replacing the bounded ledger and displays FAIL and PARTIAL as supplied', async () => {
  const first = withVerdict('FAIL');
  first.ledger.outcome = 'partial'; first.ledger.next_cursor = 'cursor-1';
  vi.mocked(getJSON).mockResolvedValueOnce(first).mockResolvedValueOnce(withVerdict('PARTIAL'));
  render(<WorkerOperations context={context} selection={selected} />);
  openOperations();
  expect(await screen.findByText('task-1: FAIL')).toBeVisible();
  fireEvent.click(screen.getByRole('button', { name: 'Next operations page' }));
  expect(await screen.findByText('task-1: PARTIAL')).toBeVisible();
  expect(screen.queryByText('task-1: FAIL')).toBeNull();
  expect(within(screen.getByLabelText('Operations ledger')).getAllByText('Seeded review')).toHaveLength(1);
  expect(vi.mocked(getJSON).mock.calls[1][0]).toContain('cursor=cursor-1');
});

it('drops a late result after the selected job incarnation changes', async () => {
  let complete: (value: unknown) => void = () => { throw Error('not started'); };
  vi.mocked(getJSON).mockImplementationOnce(() => new Promise(resolve => { complete = resolve; })).mockResolvedValueOnce(snapshot(other));
  const rendered = render(<WorkerOperations context={context} selection={selected} />);
  openOperations();
  rendered.rerender(<WorkerOperations context={context} selection={other} />);
  openOperations();
  await screen.findByText(/No structured worker verdict recorded/);
  await act(async () => { complete(withVerdict('PASS')); });
  expect(screen.queryByText('task-1: PASS')).toBeNull();
});

it('reports a failed read inline and refreshes explicitly', async () => {
  vi.mocked(getJSON).mockRejectedValueOnce(new Error('Selected view changed')).mockResolvedValueOnce(snapshot());
  render(<WorkerOperations context={context} selection={selected} />);
  openOperations();
  expect(await screen.findByRole('alert')).toHaveTextContent('Selected view changed');
  fireEvent.click(screen.getByRole('button', { name: 'Refresh operations' }));
  expect(await screen.findByText(/No structured worker verdict recorded/)).toBeVisible();
});
