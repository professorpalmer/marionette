import { CheckCircle2, Circle, Cpu, Loader2, XCircle } from 'lucide-react';
import { useMemo, useState } from 'react';
import { expertWorkerModel, isExpertEngineStamp, resolveExpertRouting, rosterRoleName, workerIsRouting } from '../lib/expertRoutingFacts';
import { expertTaskOutcome } from '../lib/expertOutcomeFacts';
import type { ExpertArtifact, ExpertMetadata, ExpertTask } from '../lib/expertMetadata';
import { nativeActiveStatuses } from '../lib/localJobMetadata';
import type { LocalRoute, LocalTask } from '../lib/localJobMetadata';
import WorkerInstruction from './WorkerInstruction';

type WorkerFact = {
  key: string;
  taskId: string | null;
  name: string;
  status: string;
  model: string | null;
  modelSource: string;
  modelKind: 'routed' | 'forecast' | 'assigned' | '';
  instruction: string;
  instructionTruncated: boolean;
  routing: string | null;
  routeDetail: string | null;
  hasRoute: boolean;
  evidence: ExpertArtifact | null;
  quality?: 'ok' | 'degraded' | 'unverified';
};

function statusIcon(status: string) {
  if (/^(running|in_progress|started|stitching)$/i.test(status)) return <Loader2 size={12} className="semantic-activity-spinner swarm-worker-icon swarm-worker-icon-active" aria-hidden />;
  if (/^(complete|completed|done)$/i.test(status)) return <CheckCircle2 size={12} className="swarm-worker-icon swarm-worker-icon-good" aria-hidden />;
  if (/^(failed|timeout|timed_out|truncated|interrupted|cancelled|partial)$/i.test(status)) return <XCircle size={12} className="swarm-worker-icon swarm-worker-icon-risk" aria-hidden />;
  return <Circle size={12} className="swarm-worker-icon" aria-hidden />;
}

function newestEvidence(artifacts: ExpertArtifact[], taskId: string): ExpertArtifact | null {
  const matches = artifacts.filter(artifact => artifact.task_id === taskId && artifact.type.toUpperCase() !== 'ROUTING');
  if (!matches.length) return null;
  // Artifacts arrive in save order and a worker saves its whole batch in one
  // second, so a tie goes to the later one: the verdict, not the run record.
  return matches.reduce((latest, candidate) => {
    const a = Date.parse(latest.created_at ?? '');
    const b = Date.parse(candidate.created_at ?? '');
    return Number.isFinite(b) && (!Number.isFinite(a) || b >= a) ? candidate : latest;
  });
}

function knownModel(value: string | null | undefined): string | null {
  const model = value?.trim() || null;
  return model && !isExpertEngineStamp(model) ? model : null;
}

function nativeFact(task: LocalTask, index: number, route?: LocalRoute, headerModel?: string): WorkerFact {
  const routed = knownModel(route?.model);
  const assigned = task.model_kind === 'assigned' ? knownModel(task.model) : null;
  const realized = route?.model_kind === 'realized' ? routed : null;
  const fallback = knownModel(headerModel);
  const pending = workerIsRouting(task.status);
  const model = realized ?? assigned ?? routed ?? fallback ?? (pending ? 'routing…' : null);
  return {
    key: `native:${task.task_id ?? index}`,
    taskId: task.task_id,
    name: rosterRoleName(task.role, task.adapter),
    status: task.status || 'unknown',
    model,
    modelSource: realized ? 'recorded realized route' : assigned ? 'assigned task model' : routed ? 'recorded route forecast'
      : fallback ? 'job display model' : pending ? 'routing in progress' : 'No model recorded',
    modelKind: realized ? 'routed' : assigned ? 'assigned' : routed ? 'forecast' : '',
    instruction: task.instruction,
    instructionTruncated: task.truncated,
    routing: route ? [route.policy || 'policy unavailable', route.created_by || 'creator unavailable', route.association].join(' · ') : null,
    routeDetail: route ? `${route.model_kind === 'forecast' ? `Forecast: ${route.model}. ` : ''}${route.detail}` : null,
    hasRoute: !!routed,
    evidence: null,
  };
}

function expertFact(task: ExpertTask, status: string, expert: ExpertMetadata, route?: ExpertArtifact): WorkerFact {
  const model = expertWorkerModel(task, route);
  const routed = !!route?.model?.trim() && !isExpertEngineStamp(route.model);
  const pending = workerIsRouting(status);
  return {
    key: `expert:${task.id}`,
    taskId: task.id,
    name: rosterRoleName(task.role, task.adapter),
    status,
    model: model ?? (pending ? 'routing…' : null),
    modelSource: routed ? `recorded ${route?.created_by || 'route'}` : model ? 'assigned task model'
      : pending ? 'routing in progress' : 'No model recorded',
    modelKind: routed ? 'routed' : model ? 'assigned' : '',
    instruction: task.instruction,
    instructionTruncated: task.instruction_truncated,
    routing: route ? [route.policy || 'policy unavailable', route.created_by || 'creator unavailable', route.adapter || task.adapter || 'adapter unavailable'].join(' · ') : null,
    routeDetail: route?.detail || null,
    hasRoute: routed,
    evidence: newestEvidence(expert.artifacts, task.id),
    quality: expertTaskOutcome(expert, task.id),
  };
}

function metric(label: string, value: string, detail?: string) {
  return <div className="swarm-metric" title={detail}><span>{label}</span><strong>{value}</strong></div>;
}

export default function CompactSwarmDashboard({
  title,
  lifecycle,
  workerStatuses,
  expert,
  headerModel,
  nativeTasks,
  nativeRoutes,
  routeCoverage,
  artifactCount,
  workerCount,
  workerCoverage = 'complete',
  usage,
  cancel,
}: {
  title: string;
  lifecycle: string;
  workerStatuses: ReadonlyMap<string, string>;
  expert?: ExpertMetadata;
  headerModel?: string;
  nativeTasks?: readonly LocalTask[];
  nativeRoutes?: ReadonlyMap<string, LocalRoute>;
  routeCoverage?: 'complete' | 'partial' | 'unavailable';
  artifactCount?: number | null;
  workerCount?: number | null;
  workerCoverage?: 'complete' | 'partial' | 'unavailable';
  usage?: number | null;
  cancel?: { disabled: boolean; request: () => void };
}) {
  const workers = useMemo(() => {
    if (nativeTasks?.length) return nativeTasks.map((task, index) => nativeFact(task, index, task.task_id ? nativeRoutes?.get(task.task_id) : undefined, headerModel));
    if (expert && expert.kind !== 'unavailable') {
      const routes = resolveExpertRouting(expert, headerModel).routingForTask;
      return expert.tasks.map(task => expertFact(task, workerStatuses.get(task.id) ?? 'unknown', expert, routes.get(task.id)));
    }
    return [];
  }, [expert, headerModel, nativeRoutes, nativeTasks, workerStatuses]);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [instructionKey, setInstructionKey] = useState<string | null>(null);
  const selected = workers.find(worker => worker.key === selectedKey) ?? workers[0] ?? null;
  const finished = workers.filter(worker => /^(complete|completed|done|failed|cancelled|stalled|timeout|timed_out|interrupted|truncated)$/i.test(worker.status)).length;
  const active = workers.filter(worker => nativeActiveStatuses.includes(worker.status)).length;
  const failed = workers.filter(worker => /^(failed|stalled|timeout|timed_out|interrupted|truncated)$/i.test(worker.status) || worker.quality === 'degraded').length;
  const allWorkersKnown = workerCoverage === 'complete' && (!expert || expert.coverage.tasks === 'complete')
    && (workerCount == null || workerCount === workers.length);
  const totalWorkers = workerCount ?? (allWorkersKnown ? workers.length : null);
  const evidenceKnown = expert ? expert.artifacts.filter(artifact => artifact.type.toUpperCase() !== 'ROUTING').length : artifactCount ?? null;
  const taskUsage = expert?.tasks.flatMap(task => [task.usage.tokens_in, task.usage.tokens_out]) ?? [];
  const knownTaskUsage = taskUsage.filter((value): value is number => typeof value === 'number');
  const usageTotal = usage ?? (knownTaskUsage.length ? knownTaskUsage.reduce((sum, value) => sum + value, 0) : null);
  const usagePartial = usage == null && (!allWorkersKnown || knownTaskUsage.length !== taskUsage.length);
  const usageText = usageTotal === null ? 'unknown' : `${usageTotal.toLocaleString()}${usagePartial ? '+' : ''}`;
  const routed = workers.filter(worker => worker.hasRoute).length;

  return <section className="swarm-compact-dashboard" aria-label="Swarm overview">
    <header className="swarm-overview-heading" title={title}>
      <h3>Worker orchestration</h3>
      <span className="swarm-overview-status" data-status={lifecycle}>{lifecycle || 'unknown'}</span>
      <p className="swarm-overview-caption">{active} active{failed ? ` · ${failed} with issues` : ''} · {routed}/{workers.length} routes recorded</p>
    </header>
    <div className="swarm-metric-strip" aria-label="Known swarm metrics">
      {metric('Workers', totalWorkers === null ? `${workers.length}+` : String(totalWorkers), allWorkersKnown ? 'All worker records loaded' : `${workers.length} worker records loaded; coverage incomplete`)}
      {metric('Finished', totalWorkers === null ? `${finished}+` : `${finished}/${totalWorkers}`, 'Terminal worker states, including failures and cancellations')}
      {metric('Evidence', evidenceKnown === null ? 'unknown' : `${evidenceKnown}${expert?.coverage.artifacts === 'partial' ? '+' : ''}`, expert?.coverage.artifacts === 'partial' ? 'Partial artifact coverage' : undefined)}
      {metric('Tokens', usageText, usageText === 'unknown' ? 'Usage not reported' : usagePartial ? 'Known token usage; coverage incomplete' : 'Reported worker tokens')}
    </div>
    {!allWorkersKnown && <p className="swarm-coverage-note" role="status">{workers.length} workers loaded · partial coverage</p>}
    {!!workers.length && <div className="swarm-worker-progress" aria-label="Observed worker states">
      {workers.map(worker => <span key={worker.key} data-status={worker.status} data-quality={worker.quality} title={`${worker.name}: ${worker.status}`} />)}
    </div>}
    {!workers.length ? <p className="swarm-dashboard-empty">Worker records are unavailable. Inspect the source tabs for retained facts.</p> : <div className="swarm-dashboard-body">
      <div className="swarm-worker-roster" role="group" aria-label="Workers">
        {workers.map(worker => <button type="button" key={worker.key} aria-pressed={selected?.key === worker.key}
          data-task-id={worker.taskId ?? undefined} data-quality={worker.quality}
          data-status={worker.status} className="swarm-worker-row" onClick={() => setSelectedKey(worker.key)}
          onKeyDown={event => { if ((event.key === 'Enter' || event.key === ' ') && !event.repeat) { event.preventDefault(); setSelectedKey(worker.key); } }}>
          {statusIcon(worker.status)}
          <span className="swarm-worker-name">{worker.name}</span>
          <span className="swarm-worker-model" title={worker.modelKind && worker.model ? `Model: ${worker.model}` : worker.model ?? 'No model recorded'}><Cpu size={11} aria-hidden /><span>{worker.model ?? 'No model recorded'}</span><small>{worker.modelKind}</small></span>
          <span className="swarm-worker-status">{worker.status}</span>
        </button>)}
      </div>
      {selected && <section className="swarm-worker-inspector" aria-label="Selected worker">
        <div className="swarm-worker-inspector-title"><span>{statusIcon(selected.status)}</span><div><p>Selected worker</p><h4>{selected.name}</h4></div></div>
        <dl>
          <div><dt>Routing</dt><dd>{selected.model ? `${selected.model} · ${selected.modelSource}` : selected.modelSource}</dd></div>
          <div><dt>Route detail</dt><dd>{selected.routeDetail ? <details><summary>{selected.routing}</summary><p className="swarm-detail-prose">{selected.routeDetail}</p></details> : selected.routing ?? (routeCoverage === 'partial' ? 'Final route unavailable while coverage is partial.' : 'No recorded route for this worker.')}</dd></div>
          <div><dt>Latest evidence</dt><dd>{selected.evidence ? <details className="swarm-evidence-preview"><summary>{`${selected.evidence.headline || selected.evidence.type} · ${selected.evidence.check_result}`}</summary><p className="swarm-detail-prose">{selected.evidence.detail || selected.evidence.failure || 'No additional detail recorded.'}</p></details> : 'No matched evidence recorded.'}</dd></div>
        </dl>
        <details key={selected.key} open={instructionKey === selected.key} className="swarm-worker-instruction">
          <summary onClick={event => { event.preventDefault(); setInstructionKey(instructionKey === selected.key ? null : selected.key); }}>Instruction</summary>
          {instructionKey === selected.key && <WorkerInstruction key={selected.key} text={selected.instruction} truncated={selected.instructionTruncated} />}
        </details>
        {cancel && <button type="button" className="swarm-worker-cancel" disabled={cancel.disabled} onClick={cancel.request}>Stop selected workers</button>}
      </section>}
    </div>}
  </section>;
}
