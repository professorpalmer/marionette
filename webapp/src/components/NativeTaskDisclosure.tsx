import { useId, useState } from 'react';
import type { LocalRoute, LocalTask } from '../lib/localJobMetadata';

export default function NativeTaskDisclosure({ task, route, kill }: { task: LocalTask; route?: LocalRoute; kill?: { disabled: boolean; request: () => void } }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const toggle = () => setOpen(value => !value);
  return <div className="min-w-0 max-w-full [overflow-wrap:anywhere] rounded border border-edge p-2">
    <button type="button" className="min-h-11 min-w-0 max-w-full whitespace-normal text-left focus-visible:outline focus-visible:outline-accent"
      aria-label={`${task.role || task.task_id || 'Task identity unavailable'}: ${task.status}`}
      aria-controls={id} aria-expanded={open}
      onKeyUp={event => event.stopPropagation()}
      onClick={event => { event.stopPropagation(); toggle(); }}
      onKeyDown={event => { event.stopPropagation(); if (event.key === ' ' || event.key === 'Enter') { event.preventDefault(); if (!event.repeat) toggle(); } }}>
      {task.role || task.task_id || 'Task identity unavailable'} · {task.status}
      {task.model_kind === 'assigned' && <span title={`Model: ${task.model}`}> · {task.model} (assigned task model)</span>}
      {route?.model && <span> · {route.model} ({route.model_kind === 'forecast' ? 'recorded route forecast' : 'recorded realized route'})</span>}
    </button>
    {kill && <button type="button" className="min-h-11 px-2 focus-visible:outline focus-visible:outline-accent" aria-label="Cancel this job"
      disabled={kill.disabled} onKeyUp={event => event.stopPropagation()} onKeyDown={event => event.stopPropagation()} onClick={event => { event.stopPropagation(); kill.request(); }}>Kill</button>}
    <div id={id} hidden={!open}>
      {open && <>
        <p>{task.instruction}</p>
        <p>Task: {task.task_id ?? 'identity unavailable'}. Adapter: {task.adapter || 'unavailable'}.</p>
        {task.model_kind === 'unavailable' && <p>Assigned task model unavailable.</p>}
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
