import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import JobCancellationControl from '../components/JobCancellationControl';
import { api, type Job } from '../lib/api';
import { cancellationMessage, type CancellationReceipt, type CancellationRequest } from '../lib/jobControl';
vi.mock('../lib/api', () => ({ api: { requestCancellation: vi.fn(), cancellationReceipt: vi.fn() } }));
let serial = 0;
function job(): Job {
  const id = `cancel-${++serial}`;
  return { id, goal: 'Bound workers', status: 'running', source: 'harness', session_id: 's', cwd: '/repo',
    job_ref: { job_id: id, state_id: 'state-one', version: 2, incarnation: '12345678-1234-4234-8234-123456789abc' }, cancellation_view: { status: 'complete', limit: 200,
      bindings: [{ task_id: 'task-a', generation: 1, lease_id: 'lease-a', owner: 'worker-a' }] } };
}
function response(request: CancellationRequest, outcome: CancellationReceipt['outcome'] = 'requested',
  cleanup: CancellationReceipt['cleanup'] = 'unknown') {
  return { ok: true as const, receipt: { job_ref: request.selection.job_ref, request_id: request.request_id,
    bindings: request.selection.bindings, outcome, cleanup, revision: 1 } };
}
beforeEach(() => { vi.resetAllMocks(); });
it('keeps input and button focus, shows pending, and reconciles without a second write', async () => {
  const row = job();
  let resolve: (value: ReturnType<typeof response>) => void = () => {};
  vi.mocked(api.requestCancellation).mockReturnValue(new Promise(r => { resolve = r; }));
  vi.mocked(api.cancellationReceipt).mockImplementation(async request => response(request, 'observed_stop', 'local_process_exited'));
  render(<><input aria-label="Draft" defaultValue="keep my input" /><JobCancellationControl job={row} repo="/repo" sessionId="s" /></>);
  const stop = screen.getByRole('button', { name: 'Stop selected workers' });
  stop.focus(); fireEvent.click(stop);
  expect(document.activeElement).toBe(stop);
  expect(screen.getByRole('status')).toHaveTextContent('stop is unconfirmed');
  const request = vi.mocked(api.requestCancellation).mock.calls[0][0];
  await act(async () => resolve(response(request)));
  expect(screen.getByRole('status')).toHaveTextContent('awaiting worker acknowledgement');
  expect(document.activeElement).toBe(stop);
  const input = screen.getByRole('textbox'); input.focus();
  fireEvent.click(screen.getByRole('button', { name: 'Refresh cancellation receipt' }));
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent('Local worker stop observed'));
  expect(screen.getByRole('status')).toHaveTextContent('descendants and remote effects remain unresolved');
  expect(input).toHaveValue('keep my input'); expect(document.activeElement).toBe(input);
  expect(api.requestCancellation).toHaveBeenCalledTimes(1);
  expect(api.cancellationReceipt).toHaveBeenCalledWith(request);
  expect(row.status).toBe('running');
});
it('retains authorization through ambiguous failure, successor render, remount and explicit retry', async () => {
  const row = job();
  vi.mocked(api.requestCancellation).mockRejectedValue(new Error('Connection lost'));
  const view = render(<JobCancellationControl job={row} repo="/repo" sessionId="s" />);
  fireEvent.click(screen.getByRole('button', { name: 'Stop selected workers' }));
  await screen.findByText(/Stop unconfirmed/);
  const request = vi.mocked(api.requestCancellation).mock.calls[0][0];
  expect(api.requestCancellation).toHaveBeenCalledTimes(1);
  view.unmount();
  const successor: Job = { ...row, cancellation_view: { status: 'complete', limit: 200,
    bindings: [{ task_id: 'task-a', generation: 2, lease_id: 'lease-b', owner: 'worker-b' }] } };
  render(<JobCancellationControl job={successor} repo="/repo" sessionId="s" />);
  fireEvent.click(screen.getByRole('button', { name: 'Stop selected workers' }));
  await waitFor(() => expect(api.requestCancellation).toHaveBeenCalledTimes(2));
  expect(vi.mocked(api.requestCancellation).mock.calls[1][0]).toEqual(request);
  expect(request.selection.bindings[0].generation).toBe(1);
});
it('fences acknowledgement when context changes during the request', async () => {
  const row = job();
  let resolve: (value: ReturnType<typeof response>) => void = () => {};
  vi.mocked(api.requestCancellation).mockReturnValue(new Promise(r => { resolve = r; }));
  const view = render(<JobCancellationControl job={row} repo="/repo" sessionId="s" />);
  fireEvent.click(screen.getByRole('button', { name: 'Stop selected workers' }));
  const request = vi.mocked(api.requestCancellation).mock.calls[0][0];
  view.rerender(<JobCancellationControl job={{ ...row, session_id: 'other' }} repo="/other" sessionId="other" />);
  await act(async () => resolve(response(request, 'observed_stop')));
  expect(screen.queryByText(/Local worker stop observed/)).not.toBeInTheDocument();
  expect(api.requestCancellation).toHaveBeenCalledTimes(1);
});
it.each(['partial', 'unavailable', 'cursor_expired'] as const)('refuses a %s view explicitly', status => {
  render(<JobCancellationControl job={{ ...job(), cancellation_view: { status, limit: 200 } }} repo="/repo" sessionId="s" />);
  const stop = screen.getByRole('button', { name: 'Stop selected workers' });
  expect(stop).toHaveAttribute('aria-disabled', 'true'); fireEvent.click(stop);
  expect(api.requestCancellation).not.toHaveBeenCalled();
  expect(screen.getByRole('status')).toHaveTextContent('maximum 200');
});
it.each(['stale_binding', 'already_terminal', 'conflict', 'requested', 'observed_stop'] as const)('renders %s separately', async outcome => {
  vi.mocked(api.requestCancellation).mockImplementation(async request => response(request, outcome, 'partial'));
  render(<JobCancellationControl job={job()} repo="/repo" sessionId="s" />);
  fireEvent.click(screen.getByRole('button', { name: 'Stop selected workers' }));
  await waitFor(() => expect(screen.getByRole('status')).toHaveTextContent(cancellationMessage(
    response(vi.mocked(api.requestCancellation).mock.calls[0][0], outcome, 'partial').receipt)));
});
it('requires an explicit new selection after a stale binding before targeting a successor', async () => {
  const row = job();
  vi.mocked(api.requestCancellation).mockImplementation(async request => response(request, 'stale_binding'));
  const view = render(<JobCancellationControl job={row} repo="/repo" sessionId="s" />);
  fireEvent.click(screen.getByRole('button', { name: 'Stop selected workers' }));
  await screen.findByText(/Successor workers were not stopped/);
  const original = vi.mocked(api.requestCancellation).mock.calls[0][0];
  const successor: Job = { ...row, cancellation_view: { status: 'complete', limit: 200,
    bindings: [{ task_id: 'task-a', generation: 2, lease_id: 'lease-b', owner: 'worker-b' }] } };
  view.rerender(<JobCancellationControl job={successor} repo="/repo" sessionId="s" />);
  expect(api.requestCancellation).toHaveBeenCalledTimes(1);
  fireEvent.click(screen.getByRole('button', { name: 'Stop refreshed workers' }));
  await waitFor(() => expect(api.requestCancellation).toHaveBeenCalledTimes(2));
  const next = vi.mocked(api.requestCancellation).mock.calls[1][0];
  expect(next.request_id).not.toBe(original.request_id);
  expect(next.selection.bindings[0].generation).toBe(2);
});
