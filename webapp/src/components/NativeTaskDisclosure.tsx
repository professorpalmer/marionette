import { useId, useState } from 'react';
import { CheckCircle2, ChevronDown, ChevronRight, Circle, Cpu, Loader2, XCircle } from 'lucide-react';
import { nativeActiveStatuses } from '../lib/localJobMetadata';
import type { LocalRoute, LocalTask } from '../lib/localJobMetadata';
import type { ExpertUsageFacts } from '../lib/expertEconomicsFacts';
import { rosterRoleName } from '../lib/expertRoutingFacts';
import { ExpertWorkerUsage } from './ExpertUsageDetails';
import WorkerInstruction from './WorkerInstruction';

const PILL = 'composer-family inline-flex items-center gap-1 px-1.5 py-px rounded-full bg-panel2/80 border border-edge/80 text-ui-9 shrink-0';

function workerGlyph(status: string) {
  if (nativeActiveStatuses.includes(status)) {
    return <Loader2 size={10} className="animate-spin semantic-activity-spinner text-accent" />;
  }
  if (/^(completed|complete|done)$/i.test(status)) {
    return <CheckCircle2 size={10} className="text-good" />;
  }
  if (/^(failed|timeout|timed_out|truncated|interrupted)$/i.test(status)) {
    return <XCircle size={10} className="text-risk" />;
  }
  return <Circle size={10} className="text-muted" />;
}

function workerTitle(task: LocalTask): string {
  return rosterRoleName(task.role || task.task_id || 'Worker', task.adapter);
}

export default function NativeTaskDisclosure({ task, route, kill, usage, onInspect }: {
  task: LocalTask; route?: LocalRoute; kill?: { disabled: boolean; request: () => void };
  usage?: ExpertUsageFacts; onInspect?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const toggle = () => setOpen(value => !value);
  const role = task.role || task.task_id || 'Task identity unavailable';
  const title = workerTitle(task);
  return <div className="min-w-0 max-w-full [overflow-wrap:anywhere] py-1.5 flex flex-col text-ui-10">
    <button type="button" className="group flex items-start gap-2 min-h-11 min-w-0 max-w-full whitespace-normal text-left px-1 py-0.5 hover:bg-panel2/25 focus-visible:outline focus-visible:outline-accent"
      aria-label={`${role}: ${task.status}`}
      aria-controls={id} aria-expanded={open}
      onKeyUp={event => event.stopPropagation()}
      onClick={event => { event.stopPropagation(); toggle(); }}
      onKeyDown={event => { event.stopPropagation(); if (event.key === ' ' || event.key === 'Enter') { event.preventDefault(); if (!event.repeat) toggle(); } }}>
      <span className="shrink-0 mt-0.5">{workerGlyph(task.status)}</span>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1 min-w-0">
          <span className="min-w-0 flex-1 truncate font-semibold text-txt">{title}</span>
          <span className="shrink-0 text-faint/60 group-hover:text-faint">{open ? <ChevronDown size={9} /> : <ChevronRight size={9} />}</span>
        </div>
        <div className="mt-0.5 flex items-center gap-1 min-w-0 flex-wrap">
          <span className={`${PILL} ${nativeActiveStatuses.includes(task.status) ? 'text-accent/80' : 'text-muted'}`}>{task.status}</span>
          {task.model_kind === 'assigned' && <span title={`Model: ${task.model}`} className={`${PILL} min-w-0 max-w-full font-mono text-accent/85`}>
            <Cpu size={9} className="shrink-0 text-accent/65" />
            {task.model} (assigned task model)
          </span>}
          {route?.model && <span className={`${PILL} min-w-0 max-w-full font-mono text-accent/85`}>
            <Cpu size={9} className="shrink-0 text-accent/65" />
            {route.model} ({route.model_kind === 'forecast' ? 'recorded route forecast' : 'recorded realized route'})
          </span>}
        </div>
      </div>
    </button>
    {usage && <div className="px-1"><ExpertWorkerUsage usage={usage} compact /></div>}
    <div id={id} hidden={!open} className="px-1 pt-1 text-ui-10 text-muted space-y-1">
      {open && <>
        {onInspect && <button type="button" className="min-h-11 px-2 focus-visible:outline focus-visible:outline-accent" onKeyUp={event => event.stopPropagation()} onKeyDown={event => event.stopPropagation()} onClick={event => { event.stopPropagation(); onInspect(); }}>Inspect</button>}
        {kill && <button type="button" className="min-h-11 px-2 focus-visible:outline focus-visible:outline-accent" aria-label="Cancel this job"
          disabled={kill.disabled} onKeyUp={event => event.stopPropagation()} onKeyDown={event => event.stopPropagation()} onClick={event => { event.stopPropagation(); kill.request(); }}>Kill</button>}
        <WorkerInstruction text={task.instruction} truncated={task.truncated} />
        <p>Task: {task.task_id ?? 'identity unavailable'}. Adapter: {task.adapter || 'unavailable'}.</p>
        {task.model_kind === 'unavailable' && !nativeActiveStatuses.includes(task.status) && <p>No assigned task model recorded.</p>}
        {task.truncated && <p>Task fields truncated.</p>}
        {route && <dl>
          <dt>Recorded routing policy</dt><dd>{route.policy || 'unavailable'}</dd>
          <dt>Recorded creator</dt><dd>{route.created_by}</dd>
          {route.created_by === 'router-fallback' && <><dt>Recorded route stage</dt><dd>fallback</dd></>}
          <dt>Association</dt><dd>{route.association}</dd>
          <dt>Recorded detail</dt><dd>{route.detail}</dd>
          {route.truncated && <dd>Routing fields truncated.</dd>}
        </dl>}
      </>}
    </div>
  </div>;
}
