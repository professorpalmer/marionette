import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, it } from 'vitest';
import MetadataJobs from '../components/MetadataJobs';
import { expertSummary } from './metadataExpert.fixtures';
import { nativeExpertFixture, capturedModelDetail } from './nativeExpert.fixtures';
import { token } from './jobMetadata.fixtures';
let fixture: Awaited<ReturnType<typeof nativeExpertFixture>> | undefined;
afterEach(() => { cleanup(); fixture?.dispose(); fixture = undefined; localStorage.clear(); });
async function setup() { fixture = await nativeExpertFixture(); return fixture; }
it('retains tasks across routing selection and replaces nested instructions on a newer revision', async () => {
  const f = await setup();
  await act(async () => { f.store.selectLocal(f.local_ref, 'tasks'); await f.store.readLocalDetail(); });
  await act(async () => { f.store.selectLocal(f.local_ref, 'routing'); await f.store.readLocalDetail(); });
  expect(f.store.getSnapshot().localDetail?.tasks?.rows[0].instruction).toBe('Keyboard disclosure');
  expect(f.store.getSnapshot().localDetail?.routing?.rows[0].model_kind).toBe('forecast');
  f.revise('Revised instruction');
  await act(async () => { f.store.selectLocal(f.local_ref, 'tasks'); await f.store.readLocalDetail(); });
  const state = f.store.getSnapshot().localDetail;
  expect(state?.tasks?.rows[0].instruction).toBe('Revised instruction');
  expect(state?.tasks?.page.revision).toBeGreaterThan(state?.routing?.page.revision ?? 0);
  await act(async () => { f.store.selectLocal({ ...f.local_ref, job_id: 'local-other' }, 'tasks'); });
  expect(f.store.getSnapshot().localDetail?.tasks).toBeUndefined();
  expect(f.store.getSnapshot().localDetail?.routing).toBeUndefined();
});
it('withholds final route on partial history, then preserves prior-page association after completion', async () => {
  const f = await setup();
  const original = f.request.getMockImplementation();
  if (!original) throw Error('Missing responder');
  f.request.mockImplementation(async (method, path) => {
    const url = new URL(path, 'http://fixture');
    if (url.pathname.endsWith('/local/detail') && url.searchParams.get('lane') === 'routing') {
      const value = f.response('routing');
      return url.searchParams.has('cursor') ? { ...value, rows: [], page: { ...value.page, scanned: 0 } }
        : { ...value, page: { ...value.page, outcome: 'partial', next_cursor: token() } };
    }
    return original(method, path);
  });
  render(<f.Provider><MetadataJobs /></f.Provider>);
  fireEvent.click(screen.getByRole('button', { name: /Provider worker/ }));
  await screen.findByText(/routing: partial/);
  expect(screen.queryByText(/cheap-model/)).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: 'Next selected page' }));
  await screen.findByText(/cheap-model \(recorded route forecast\)/);
  expect(f.store.getSnapshot().localDetail?.routing?.rows).toHaveLength(1);
  expect(screen.queryByTitle('Model: cheap-model')).toBeNull();
  f.revise('New task body');
  fireEvent.click(screen.getByRole('button', { name: 'Inspect workers' }));
  await waitFor(() => expect(f.store.getSnapshot().localDetail?.tasks?.page.revision).toBe(f.response('tasks').page.revision));
  expect(screen.queryByText(/cheap-model/)).toBeNull();
});
it('keeps forecast unavailable after an expired selected read', async () => {
  const f = await setup();
  await act(async () => { f.store.selectLocal(f.local_ref, 'tasks'); await f.store.readLocalDetail(); });
  await act(async () => { f.store.selectLocal(f.local_ref, 'routing'); await f.store.readLocalDetail(); });
  const original = f.request.getMockImplementation();
  if (!original) throw Error('Missing responder');
  f.request.mockImplementation(async (method, path) => path.includes('/local/detail') ? { ...f.response('routing'), summary: undefined,
    rows: [], page: { ...f.response('routing').page, outcome: 'expired', scanned: 0 } } : original(method, path));
  await act(async () => { await f.store.readLocalDetail(); });
  expect(f.store.getSnapshot().localDetail?.summaryFreshness).toBe('stale');
  expect(f.store.getSnapshot().localDetail?.laneFreshness).toBe('stale');
});
it('keeps separate captured attempt models historical even with partial coverage and a matching task', async () => {
  const f = await setup();
  const selected = { repo: '/repo', session_id: 'sess-test', source: 'harness' as const,
    job_ref: { job_id: 'job_1', state_id: 'store-A', version: 2 as const, incarnation: '12345678-1234-4234-8234-123456789abc' } };
  await f.replace([expertSummary(selected, 'Historical model inspection')]);
  f.selected.mockImplementation(async value => {
    const detail = capturedModelDetail(value, f.context());
    const attempt = detail.history.attempts.rows[0];
    return { ...detail, history: { ...detail.history, attempts: { ...detail.history.attempts,
      page: { ...detail.history.attempts.page, outcome: 'partial', next_cursor: token(), scanned: 2, captured_count: 3 },
      rows: [attempt, { ...attempt, sequence: attempt.sequence + 1, facts: { ...attempt.facts, model: 'other-recorded-model', attempt_id: 'second-attempt' } }] } } };
  });
  render(<f.Provider><MetadataJobs /></f.Provider>);
  fireEvent.click(screen.getByRole('button', { name: /Historical model inspection/ }));
  fireEvent.click(screen.getByRole('button', { name: 'Inspect tasks and artifacts' }));
  const routing = await screen.findByRole('button', { name: 'Routing', exact: true });
  expect(screen.queryByTitle('Model: grok-4-5')).toBeNull();
  fireEvent.click(routing);
  expect(await screen.findByTitle('Model: grok-4-5')).toHaveTextContent('Historical model; current job and worker model unconfirmed');
  expect(screen.getByTitle('Model: other-recorded-model').parentElement).toHaveTextContent('second-attempt');
  expect(screen.getByRole('region', { name: 'Routing', exact: true })).toHaveTextContent('2 attempts shown; 3 captured.');
  expect(screen.getByRole('region', { name: 'Routing', exact: true })).toHaveTextContent('Page: partial');
  expect(screen.getByTitle('Model: grok-4-5').parentElement).toHaveTextContent(/Task: .+Run:/);
  fireEvent.click(screen.getByRole('button', { name: 'Tasks', exact: true }));
  expect(screen.queryByTitle('Model: grok-4-5')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: /task_.*queued/ }));
  expect(screen.getByText('Model, adapter and live progress unavailable.')).toBeVisible();
});
