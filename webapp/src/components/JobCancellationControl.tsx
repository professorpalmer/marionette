import { useEffect, useRef, useSyncExternalStore } from 'react';
import { api, type Job } from '../lib/api';
import { cancellationMessage, jobControlKey, selectJobControl,
  type CancellationRequest, type CancellationReceipt } from '../lib/jobControl';

type Attempt = { request: CancellationRequest } & (
  | { kind: 'pending' }
  | { kind: 'receipt'; receipt: CancellationReceipt }
  | { kind: 'unknown'; message: string }
);
// Retain immutable authorization across polling, panel remounts and ambiguous transport failures.
const attempts = new Map<string, Attempt>();
const listeners = new Set<() => void>();
const subscribe = (listener: () => void) => { listeners.add(listener); return () => { listeners.delete(listener); }; };
function publish(key: string, attempt: Attempt) {
  attempts.set(key, attempt);
  listeners.forEach(listener => listener());
}

export default function JobCancellationControl({ job, repo, sessionId, disabled = false }: {
  job: Job; repo: string; sessionId: string; disabled?: boolean;
}) {
  const key = jobControlKey(job, repo, sessionId);
  const attempt = useSyncExternalStore(subscribe, () => attempts.get(key), () => undefined);
  const selection = selectJobControl(job, repo, sessionId);
  const context = useRef({ key, disabled, epoch: 0 });
  if (context.current.key !== key || context.current.disabled !== disabled) {
    context.current = { key, disabled, epoch: context.current.epoch + 1 };
  }
  useEffect(() => {
    const invalidate = () => { context.current.epoch += 1; };
    window.addEventListener('harness-session-changed', invalidate);
    window.addEventListener('harness-project-selected', invalidate);
    return () => {
      invalidate();
      window.removeEventListener('harness-session-changed', invalidate);
      window.removeEventListener('harness-project-selected', invalidate);
    };
  }, []);

  const run = async (operation: 'request' | 'receipt' | 'replacement') => {
    if (disabled || attempts.get(key)?.kind === 'pending') return;
    const previous = attempts.get(key);
    const retained = operation === 'replacement' ? undefined : previous?.request;
    const request = retained ?? (selection?.version === 2 ? {
      selection, request_id: crypto.randomUUID(),
    } : null);
    if (!request) return;
    const epoch = context.current.epoch;
    publish(key, { kind: 'pending', request });
    try {
      const result = operation === 'receipt' ? await api.cancellationReceipt(request) : await api.requestCancellation(request);
      if (context.current.epoch !== epoch || context.current.key !== key || context.current.disabled) {
        publish(key, { kind: 'unknown', request, message: 'Context changed; refresh the original receipt to reconcile.' });
        return;
      }
      publish(key, { kind: 'receipt', request, receipt: result.receipt });
    } catch (error) {
      publish(key, { kind: 'unknown', request,
        message: error instanceof Error ? error.message : 'Acknowledgement unavailable. Stop is unconfirmed.' });
    }
  };
  const unavailable = !selection && !attempt;
  const busy = attempt?.kind === 'pending';
  const settled = attempt?.kind === 'receipt';
  const canSelectAgain = settled && selection?.version === 2
    && (attempt.receipt.outcome === 'stale_binding' || attempt.receipt.outcome === 'already_terminal')
    && JSON.stringify(selection.bindings) !== JSON.stringify(attempt.request.selection.bindings);
  const message = attempt?.kind === 'receipt' ? cancellationMessage(attempt.receipt)
    : attempt?.kind === 'unknown' ? `Stop unconfirmed. ${attempt.message}`
    : attempt?.kind === 'pending' ? 'Awaiting cancellation acknowledgement; stop is unconfirmed.'
    : unavailable ? job.job_ref && job.job_ref.version !== 2 ? 'Stop unavailable: legacy identity is read-only. Refresh job metadata to select the current incarnation.' : 'Stop unavailable: refresh a complete task view (maximum 200 workers).' : '';
  return (
    <span className="flex flex-col items-start gap-1 text-[10px]" onClick={e => e.stopPropagation()} onKeyDown={e => e.stopPropagation()}>
      <span className="flex gap-2">
        <button type="button" aria-label="Stop selected workers" aria-disabled={disabled || unavailable || busy || settled}
          onClick={() => { if (!disabled && !unavailable && !busy && !settled) void run('request'); }}
          className="text-risk aria-disabled:opacity-50 focus-visible:outline focus-visible:outline-1 focus-visible:outline-accent">
          {settled ? 'Request recorded' : attempt ? 'Retry original stop request' : 'Stop selected workers'}
        </button>
        {attempt && <button type="button" aria-label="Refresh cancellation receipt" aria-disabled={disabled || busy}
          onClick={() => { if (!disabled && !busy) void run('receipt'); }}
          className="text-muted focus-visible:outline focus-visible:outline-1 focus-visible:outline-accent">Refresh receipt</button>}
        {canSelectAgain && <button type="button" aria-disabled={disabled || busy}
          onClick={() => { if (!disabled && !busy) void run('replacement'); }}
          className="text-risk focus-visible:outline focus-visible:outline-1 focus-visible:outline-accent">Stop refreshed workers</button>}
      </span>
      {message && <span role="status" className="max-w-xs whitespace-normal text-muted">{message}</span>}
    </span>
  );
}
