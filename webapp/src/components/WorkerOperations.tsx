import { useEffect, useRef, useState } from 'react';
import type { MetadataContext, MetadataSelection } from '../lib/jobMetadata';
import { fetchWorkerOperations, operationsKey } from '../lib/workerOperations';
import type { QualityLoopOptions, WorkerOperationsSnapshot } from '../lib/workerOperations';

type Load = { kind: 'idle' | 'loading' } | { kind: 'loaded'; value: WorkerOperationsSnapshot } | { kind: 'error'; message: string };
const input = 'rounded border border-edge bg-panel2 px-2 py-1 text-xs text-txt focus-visible:outline focus-visible:outline-accent disabled:opacity-50';
const button = `${input} hover:bg-panel disabled:cursor-not-allowed`;

export default function WorkerOperations(props: { context: MetadataContext; selection: MetadataSelection }) {
  return <SelectedOperations key={operationsKey(props.context, props.selection)} {...props} />;
}

function SelectedOperations({ context, selection }: { context: MetadataContext; selection: MetadataSelection }) {
  const [load, setLoad] = useState<Load>({ kind: 'idle' });
  const [options, setOptions] = useState<QualityLoopOptions>({ mode: 'goal', max_iterations: 3, cost_cap_usd: 1, cleanup: false });
  const [target, setTarget] = useState('');
  const [message, setMessage] = useState('');
  const request = useRef(0);
  const busy = useRef(false);
  useEffect(() => () => { request.current += 1; }, []);
  async function refresh(cursor: string | null = null) {
    if (busy.current) return;
    busy.current = true;
    const generation = ++request.current;
    setLoad({ kind: 'loading' });
    try {
      const value = await fetchWorkerOperations(context, selection, cursor);
      if (request.current === generation) setLoad({ kind: 'loaded', value });
    } catch (error) {
      if (request.current === generation) setLoad({ kind: 'error', message: error instanceof Error ? error.message : 'Worker operations unavailable.' });
    } finally { if (request.current === generation) busy.current = false; }
  }
  const value = load.kind === 'loaded' ? load.value : null;
  return <details className="border-t border-edge pt-2" onToggle={event => {
    if (event.currentTarget.open && load.kind === 'idle') void refresh();
  }}>
    <summary className="cursor-pointer text-xs text-txt focus-visible:outline focus-visible:outline-accent">Worker operations</summary>
    <div className="flex flex-col gap-3 py-2 text-xs text-muted" aria-label="Worker operations">
      {load.kind === 'loading' && <p role="status">Loading worker operations...</p>}
      {load.kind === 'error' && <p role="alert">{load.message}</p>}
      <div className="flex gap-2">
        <button type="button" className={button} disabled={load.kind === 'loading'} onClick={() => void refresh()}>Refresh operations</button>
        <button type="button" className={button} disabled={!value?.ledger.next_cursor} onClick={() => void refresh(value?.ledger.next_cursor ?? null)}>Next operations page</button>
      </div>
      {value && <>
        <p>Lifecycle: {value.lifecycle}. Worker verdicts are advisory; completion alone does not mean PASS.</p>
        <p>Puppetmaster {value.kernel_version}</p>
        <div aria-label="Operations ledger" className="flex max-h-64 flex-col gap-2 overflow-auto">
          {value.ledger.rows.length === 0 && <p>No structured worker verdict recorded on this page.</p>}
          {value.ledger.rows.map(row => <div key={row.id} className="border-l border-edge pl-2">
            <p className="text-txt">{row.task_id}: {row.verdict === 'unknown' ? 'Verdict unknown' : row.verdict}</p>
            <p className="whitespace-pre-wrap break-words">{row.reason}{row.reason_truncated ? ' (truncated)' : ''}</p>
            <p className="text-faint">Worker advisory · {row.created_at}</p>
          </div>)}
        </div>
        <p>{value.ledger.outcome === 'partial' ? 'More records are available.' : value.ledger.outcome === 'complete' ? 'End of this traversal.' : 'Ledger unavailable; refresh to retry.'} Each page scans at most 21 artifact references.</p>
        <p>Cleanup and iteration costs reference existing usage; this panel adds no spend to job totals.</p>
      </>}
      <fieldset className="flex flex-col gap-2" disabled>
        <legend className="text-txt">Quality loop</legend>
        <p>A bounded worker goal or review pass is separate from the recurring pilot session loop.</p>
        <label className="flex items-center gap-2">Mode<select className={input} value={options.mode} onChange={event => {
          const mode = event.target.value;
          if (mode === 'goal' || mode === 'review_pass') setOptions({ ...options, mode });
        }}><option value="goal">Goal</option><option value="review_pass">Review pass</option></select></label>
        <label className="flex items-center gap-2">Maximum iterations<input className={input} type="number" min={1} max={10} value={options.max_iterations} onChange={event => setOptions({ ...options, max_iterations: event.target.valueAsNumber })} /></label>
        <label className="flex items-center gap-2">Cost cap (USD)<input className={input} type="number" min={0.01} max={1000} step={0.01} value={options.cost_cap_usd} onChange={event => setOptions({ ...options, cost_cap_usd: event.target.valueAsNumber })} /></label>
        <label className="flex items-center gap-2"><input type="checkbox" checked={options.cleanup} onChange={event => setOptions({ ...options, cleanup: event.target.checked })} />Cleanup edited files</label>
        <div className="flex gap-2"><button type="button" className={button}>Start quality loop</button><button type="button" className={button}>Stop quality loop</button></div>
      </fieldset>
      <p>Quality loops and cleanup are unavailable with this kernel binding. Stop must cancel current children and prevent future iterations.</p>
      <fieldset className="flex flex-col gap-2" disabled>
        <legend className="text-txt">Steer workers</legend>
        <label className="flex flex-col gap-1">Selected worker<input className={input} value={target} onChange={event => setTarget(event.target.value)} /></label>
        <label className="flex flex-col gap-1">Message<textarea className={input} maxLength={4096} value={message} onChange={event => setMessage(event.target.value)} /></label>
        <div className="flex gap-2"><button type="button" className={button}>Send to worker</button><button type="button" className={button}>Broadcast to live workers</button></div>
      </fieldset>
      <p>Steering is unavailable with this kernel binding. Delivery needs separate accepted and consumed receipts for every recipient.</p>
      <p>Failure routes: unavailable. File claims, expiry and steal history: unavailable.</p>
    </div>
  </details>;
}
