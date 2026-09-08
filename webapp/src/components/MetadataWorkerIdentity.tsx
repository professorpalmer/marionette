import { useId, useState } from 'react';
import MetadataActivityIndicator from './MetadataActivityIndicator';
import { failedOutcomeStatuses } from './MetadataOutcomeChrome';
import type { MetadataDetail, MetadataTask } from '../lib/jobMetadata';

export default function MetadataWorkerIdentity({ task, history, running }: {
  task: MetadataTask; history: MetadataDetail['history']; running: boolean;
}) {
  const [open, setOpen] = useState(false);
  const detailsId = useId();
  const runs = history.kind === 'unavailable' ? [] : history.runs.rows.filter(row => row.facts.task_id === task.id);
  const attempts = history.kind === 'unavailable' ? [] : history.attempts.rows.filter(row => row.facts.task_id === task.id);
  const outcomes = history.kind === 'unavailable' ? [] : history.process_outcomes.rows.filter(row => row.facts.task_id === task.id && row.facts.identity_state === 'available');
  const observations = history.kind === 'unavailable' ? [] : history.observations.rows.filter(row =>
    row.facts.task_id === task.id && row.facts.identity_state === 'available'
    && typeof row.facts.run_id === 'string' && row.facts.run_id.length > 0);
  return <div className="rounded border border-edge p-2">
    <button type="button" className="cursor-pointer min-h-11 focus-visible:outline focus-visible:outline-accent"
      aria-expanded={open} aria-controls={detailsId} onClick={() => setOpen(value => !value)}>
      {running && <MetadataActivityIndicator />}<span className={failedOutcomeStatuses.has(task.status ?? '') ? 'text-risk' : 'text-muted'}>{task.id}: {task.status ?? 'unknown'}</span>
    </button>
    <div id={detailsId} hidden={!open}>
      <dl className="break-all"><dt>Task</dt><dd>{task.id}</dd></dl>
      <p>Revision {task.revision}. Stamp: {task.stamp}.</p>
      <p>Model, adapter and live progress unavailable.</p>
      <p>Pin attribution unknown</p>
      {task.binding && <dl className="break-all"><dt>Generation</dt><dd>{task.binding.generation ?? 'unknown'}</dd>
        <dt>Lease</dt><dd>{task.binding.lease_id ?? 'unknown'}</dd><dt>Owner</dt><dd>{task.binding.owner ?? 'unknown'}</dd></dl>}
      {attempts.map(attempt => <div key={attempt.sequence} className="border-t border-edge">
        <p>Captured attempt {String(attempt.facts.attempt_id ?? attempt.sequence)}. Historical identity; current worker model unconfirmed.</p>
        <dl className="break-all">{['model', 'adapter', 'provider', 'run_id', 'started_at'].map(key => {
          const value = attempt.facts[key];
          return value == null || value === '' ? null : <div key={key}><dt>Captured {key.replaceAll('_', ' ')}</dt><dd>{String(value)}</dd></div>;
        })}</dl>
      </div>)}
      {outcomes.map(outcome => <div key={outcome.sequence} className="border-t border-edge">
        <p>Captured process outcome {String(outcome.facts.observation_id ?? outcome.sequence)}. Historical process evidence; not a failure diagnosis.</p>
        <dl className="break-all">{['attempt_id', 'run_id', 'returncode', 'timed_out'].map(key => {
          const value = outcome.facts[key];
          return value == null ? null : <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd>{String(value)}</dd></div>;
        })}</dl>
      </div>)}
      {open && observations.length > 0 && <section aria-label={`Captured usage for ${task.id}`}>
        <p>Captured usage observations. Separate source snapshots; not live worker totals. Observations must not be summed.</p>
        {observations.map(observation => <details key={observation.sequence} className="border-t border-edge">
          <summary className="cursor-pointer min-h-11 focus-visible:outline">Captured usage observation {String(observation.facts.observation_id ?? observation.sequence)}</summary>
          <dl className="break-all">{['task_id', 'run_id', 'attempt_id', 'observation_id', 'source', 'observed_at', 'identity_state', 'usage_state', 'tokens_in', 'tokens_out', 'cache_read_tokens', 'cache_write_tokens', 'cost_state', 'cost_usd', 'cost_basis'].map(key => {
            const value = observation.facts[key];
            return <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd>{value == null ? 'unknown' : String(value)}</dd></div>;
          })}</dl>
        </details>)}
      </section>}
      {runs.map(run => <div key={run.sequence} className="border-t border-edge">
        <p>Captured run {String(run.facts.id ?? run.sequence)}. Historical identity; not current worker presentation.</p>
        <dl className="break-all">{['role', 'worker_id', 'status', 'started_at', 'completed_at'].map(key => {
          const value = run.facts[key];
          return value == null ? null : <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd>{String(value)}</dd></div>;
        })}</dl>
      </div>)}
    </div>
  </div>;
}
