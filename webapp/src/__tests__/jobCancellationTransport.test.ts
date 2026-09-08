import { beforeEach, expect, it, vi } from 'vitest';
import { api } from '../lib/api';
import { getJSON, postJSON } from '../lib/transport';
import { parseCancellationResult, type CancellationRequest } from '../lib/jobControl';
vi.mock('../lib/transport', async importOriginal => ({
  ...await importOriginal<typeof import('../lib/transport')>(), getJSON: vi.fn(), postJSON: vi.fn(),
}));
const request: CancellationRequest = { request_id: 'one-request', selection: {
  version: 2, source: 'cli', repo: '/repo', session_id: 'session', job_ref: { job_id: 'job', state_id: 'store' },
  bindings: [{ task_id: 'task', generation: 3, lease_id: 'lease', owner: 'owner' }],
} };
function result() {
  return { ok: true, ...structuredClone(request), receipt: {
    request_id: request.request_id, job_ref: request.selection.job_ref,
    bindings: request.selection.bindings, revision: 1, outcome: 'requested', cleanup: 'unknown',
  } };
}
beforeEach(() => vi.resetAllMocks());
it('writes once and receipt reconciliation uses GET with captured context', async () => {
  vi.mocked(postJSON).mockResolvedValue(result()); vi.mocked(getJSON).mockResolvedValue(result());
  await api.requestCancellation(request); await api.cancellationReceipt(request);
  expect(postJSON).toHaveBeenCalledTimes(1);
  expect(postJSON).toHaveBeenCalledWith(expect.stringContaining('/api/swarm/cancel'), request,
    { sessionId: 'session', repo: '/repo' });
  expect(getJSON).toHaveBeenCalledWith(expect.stringContaining('/api/swarm/cancellation-receipt?'),
    { sessionId: 'session', repo: '/repo' });
});
it('propagates typed transport errors without replaying writes', async () => {
  const error = Object.assign(new Error('Context changed'), { code: 'cancellation_context_changed', status: 409 });
  vi.mocked(postJSON).mockRejectedValue(error);
  await expect(api.requestCancellation(request)).rejects.toBe(error);
  expect(postJSON).toHaveBeenCalledTimes(1);
});
it.each(['session', 'repo', 'source', 'state', 'request', 'binding', 'outcome', 'cleanup', 'revision'])(
  'rejects a mismatched %s acknowledgement', field => {
    const value = result();
    if (field === 'session') value.selection.session_id = 'foreign';
    if (field === 'repo') value.selection.repo = '/foreign';
    if (field === 'source') value.selection.source = 'harness';
    if (field === 'state') value.selection.job_ref.state_id = 'foreign';
    if (field === 'request') value.request_id = 'foreign';
    if (field === 'binding') value.selection.bindings[0].generation = 9;
    if (field === 'outcome') value.receipt.outcome = 'done';
    if (field === 'cleanup') value.receipt.cleanup = 'all-stopped';
    if (field === 'revision') value.receipt.revision = -1;
    expect(() => parseCancellationResult(value, request)).toThrow();
  });
it('copies the native incarnation into the exact cancellation envelope before caller mutation', async () => {
  const selected = { version: 1, source: 'local', local_incarnation: 'incarnation-A', repo: '/repo', session_id: 'session', job_ref: { job_id: 'local-one', state_id: null } } satisfies Parameters<typeof api.swarmCancel>[0];
  vi.mocked(postJSON).mockResolvedValue({ ok: true });
  const pending = api.swarmCancel(selected);
  selected.local_incarnation = 'incarnation-B'; selected.job_ref.job_id = 'local-two';
  await pending;
  expect(postJSON).toHaveBeenCalledExactlyOnceWith(expect.stringContaining('/api/swarm/cancel'), {
    selection: { version: 1, source: 'local', local_incarnation: 'incarnation-A', repo: '/repo', session_id: 'session', job_ref: { job_id: 'local-one', state_id: null } },
  }, { sessionId: 'session', repo: '/repo' });
});
it('refuses malformed native incarnation instead of downgrading the request', async () => {
  await expect(api.swarmCancel({ version: 1, source: 'local', local_incarnation: '', repo: '/repo', session_id: 'session', job_ref: { job_id: 'local-one', state_id: null } })).rejects.toThrow('incarnation');
  expect(postJSON).not.toHaveBeenCalled();
});
