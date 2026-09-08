import MetadataWorkerProgress from './MetadataWorkerProgress';
import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { peekPendingSwarmNavigation, takePendingSwarmNavigation } from '../lib/pendingSwarmOpenJob';
import type { SwarmNavigationTarget } from '../lib/pendingSwarmOpenJob';
import MetadataWorkerIdentity from './MetadataWorkerIdentity';
import MetadataFindingCount from './MetadataFindingCount';
import { metadataSelectionKey } from '../lib/jobMetadata';
import type { MetadataDetail } from '../lib/jobMetadata';
import type { JobMetadataStore } from '../lib/useJobMetadata';
import { historyCursors } from '../lib/selectedMetadataEvidence';
import type { HistoryLaneName, SelectedMetric } from '../lib/selectedMetadataEvidence';

const button = 'min-h-11 px-2 text-sm text-muted hover:text-txt focus-visible:outline focus-visible:outline-accent disabled:opacity-50';
const tabs = ['Tasks', 'Artifacts', 'Routing', 'Checks', 'History', 'Economics'] as const;
type Panel = typeof tabs[number];
function metricText(metric: SelectedMetric | undefined, dollars = false): string {
  if (!metric || metric.total === null || metric.state === 'unknown') return 'unknown';
  return `${dollars ? '$' : ''}${metric.total.toLocaleString(undefined, { maximumFractionDigits: dollars ? 8 : 0 })} (${metric.state})`;
}
export default function MetadataExpertPanels({ detail, store, busy, stale, navigation }: {
  navigation?: SwarmNavigationTarget; detail: MetadataDetail; store: JobMetadataStore; busy: boolean; stale: boolean;
}) {
  const [panel, setPanel] = useState<Panel>('Tasks');
  const artifactRows = useRef(new Map<string, HTMLDetailsElement>());
  const target = navigation?.kind === 'pm' && navigation.artifactId
    && metadataSelectionKey(navigation.selection) === metadataSelectionKey(detail.selection)
    && peekPendingSwarmNavigation() === navigation ? navigation : null;
  useEffect(() => { if (target) setPanel('Artifacts'); }, [target]);
  useLayoutEffect(() => {
    if (stale || !target?.artifactId || panel !== 'Artifacts' || peekPendingSwarmNavigation() !== target) return;
    const row = artifactRows.current.get(target.artifactId);
    if (!row) return;
    row.open = true;
    row.focus({ preventScroll: true });
    row.scrollIntoView({ block: 'nearest' });
    takePendingSwarmNavigation(target);
  }, [target, panel, detail, stale]);
  const display = detail.display;
  const history = detail.history;
  const cost = detail.cost;
  const read = (lane: 'tasks' | 'artifacts' | HistoryLaneName) => {
    const selected = store.getSnapshot().detail;
    if (selected.kind !== 'selected' || metadataSelectionKey(selected.selection) !== metadataSelectionKey(detail.selection)) store.select(detail.selection);
    void store.readDetail(lane);
  };
  const lanes: HistoryLaneName[] = ['attempts', 'runs', 'process_outcomes', 'observations'];
  return <section aria-label="Selected job inspector" className="space-y-2 border-t border-edge pt-2">
    {display?.kind === 'available' && <div>
      <p className="text-txt break-words">{display.goal_preview}{display.goal_preview_truncated ? ' (preview truncated)' : ''}</p>
      <p>Delivery: {display.delivery}. Quality: {display.quality}. Publication and lifecycle do not certify verification.</p>
    </div>}
    {detail.artifacts.page.outcome === 'complete' && detail.artifacts.rows.length === 0 && <p>No artifacts recorded</p>}
    {detail.artifacts.rows.length > 0 && <p>{detail.artifacts.rows.length} {detail.artifacts.rows.length === 1 ? 'artifact recorded' : 'artifacts recorded'} without a summary</p>}
    <MetadataFindingCount detail={detail} />
    <p>Historical receipts: {history.kind === 'available' ? 'captured records available' : history.kind === 'partial' ? 'partially available' : 'unavailable'}. Artifact bodies and check assertions are unavailable through the bounded public reader.</p>
    {stale && <p role="status">Retained selected details are stale. Retry inspection to refresh.</p>}
    <nav aria-label="Inspector panels" className="flex flex-wrap gap-1">{tabs.map(tab => <button type="button" className={button}
      key={tab} aria-pressed={panel === tab} onClick={() => setPanel(tab)}>{tab}</button>)}</nav>
    {panel === 'Tasks' && <section aria-label="Tasks" className="space-y-1">
      <MetadataWorkerProgress tasks={detail.tasks} />
      <p>{detail.tasks.rows.length} tasks shown{detail.task_count == null ? '; total unknown' : ` of ${detail.task_count}`}. Page: {detail.tasks.page.outcome}.</p>
      {detail.tasks.rows.length === 0 && <p>{detail.tasks.page.outcome === 'complete' ? 'No tasks recorded.' : 'Task records unavailable.'}</p>}
      {detail.tasks.rows.map(task => <MetadataWorkerIdentity key={task.id} task={task} history={history} running={!stale && detail.lifecycle === 'running' && task.status === 'running'} />)}
      {detail.artifacts.rows.map(artifact => <p key={artifact.id}>{artifact.type ?? 'Artifact'} / {artifact.id}: recorded, check result unavailable</p>)}
      <button className={button} disabled={busy || stale || detail.tasks.page.outcome !== 'partial'} onClick={() => read('tasks')}>Next tasks</button>
    </section>}
    {panel === 'Artifacts' && <section aria-label="Artifacts" className="space-y-1">
      {target && !detail.artifacts.rows.some(artifact => artifact.id === target.artifactId) && <p role="status">Requested artifact {target.artifactId} is not in the loaded records. Navigation remains pending; inspect again or load the next artifacts page.</p>}
      <p>{detail.artifacts.rows.length} artifact records shown{detail.artifact_count == null ? '; total unknown' : ` of ${detail.artifact_count}`}. Page: {detail.artifacts.page.outcome}.</p>
      {detail.artifacts.rows.length === 0 && <p>{detail.artifacts.page.outcome === 'complete' ? 'No artifacts recorded.' : 'Artifact records unavailable.'}</p>}
      {detail.artifacts.rows.map(artifact => <details key={artifact.id} data-artifact-ids={artifact.id} data-finding-id={artifact.id} tabIndex={-1}
        ref={element => { if (element) artifactRows.current.set(artifact.id, element); else artifactRows.current.delete(artifact.id); }} className="rounded border border-edge p-2">
        <summary className="cursor-pointer min-h-11 focus-visible:outline">{artifact.type ?? 'Artifact'} / {artifact.id}: {artifact.status ?? 'unknown'}</summary>
        <dl className="break-all"><dt>Task</dt><dd>{artifact.task_id ?? 'link missing'}</dd><dt>SHA-256</dt><dd>{artifact.sha256 ?? 'unknown'}</dd></dl>
        <p>Recorded; contents not independently verified. Artifact body unavailable.</p>
        <p>Check result unavailable.</p>
      </details>)}
      <button className={button} disabled={busy || stale || detail.artifacts.page.outcome !== 'partial'} onClick={() => read('artifacts')}>Next artifacts</button>
    </section>}
    {panel === 'Checks' && <section aria-label="Checks"><p>Check assertions are unavailable in this reader. Recorded artifacts and completed lifecycle do not establish that checks passed.</p>
      {detail.artifacts.rows.filter(a => ['gate', 'verification', 'check', 'test'].includes(a.type?.toLowerCase() ?? '')).map(a => <p key={a.id}>{a.type} / {a.id}: recorded, check result unavailable</p>)}
    </section>}
    {panel === 'Routing' && <section aria-label="Routing"><p>Routing decisions and forecasts are unavailable. Captured attempt facts do not prove why a route was selected.</p>
      {history.kind !== 'unavailable' && <p>{history.attempts.rows.length} attempts shown; {history.attempts.page.captured_count ?? 'unknown'} captured. Coverage: {history.attempts.page.coverage}. Page: {history.attempts.page.outcome}.</p>}
      {history.kind !== 'unavailable' && history.attempts.rows.map(row => <dl key={row.sequence} className="break-all border-b border-edge py-2">
        <dt>Captured attempt {String(row.facts.attempt_id ?? row.sequence)}</dt>
        {typeof row.facts.model === 'string' && row.facts.model && <dd title={`Model: ${row.facts.model}`}>
          model: {row.facts.model} · Historical model; current job and worker model unconfirmed.
        </dd>}
        <dd>Task: {String(row.facts.task_id ?? 'unknown')}. Run: {String(row.facts.run_id ?? 'unknown')}.</dd>
        {row.facts.started_at != null && <dd>Captured started at: {String(row.facts.started_at)}</dd>}
        <dd>{Object.entries(row.facts).filter(([key]) => key !== 'model' && /model|provider|adapter|route/.test(key)).map(([key, value]) => `${key}: ${value ?? 'unknown'}`).join(' · ') || 'Routing facts unknown'}</dd>
      </dl>)}
      <button className={button} disabled={busy || stale || history.kind === 'unavailable' || history.attempts.page.outcome !== 'partial'} data-cursor-lane={historyCursors.attempts} onClick={() => read('attempts')}>Next attempts</button>
    </section>}
    {panel === 'History' && <section aria-label="History" className="space-y-2">
      <p>Captured records only. Complete provider-invocation history is unverified.</p>
      {history.kind === 'partial' && <p role="status">History is partially unavailable: {history.missing?.join(', ').replaceAll('_', ' ') || 'coverage unknown'}.</p>}
      {history.kind === 'unavailable' ? <p>History unavailable: {history.reason.replaceAll('_', ' ')}.</p> : lanes.map(lane => <section key={lane} aria-label={lane.replaceAll('_', ' ')}>
        <h4 className="text-txt font-semibold">{lane.replaceAll('_', ' ')}</h4>
        <p>{history[lane].rows.length} shown; {history[lane].page.captured_count ?? 'unknown'} captured. Coverage: {history[lane].page.coverage}. Page: {history[lane].page.outcome}.</p>
        {history[lane].rows.map(row => <details key={row.sequence} className="rounded border border-edge p-2">
          <summary className="cursor-pointer min-h-11 focus-visible:outline">{row.kind} {row.sequence}</summary>
          <dl className="break-all">{Object.entries(row.facts).map(([key, value]) => <div key={key}><dt>{key.replaceAll('_', ' ')}</dt><dd>{value === null ? 'unknown' : String(value)}</dd></div>)}</dl>
          {row.completion?.intent_digest != null && <dl className="break-all"><dt>Publication intent digest</dt><dd>{row.completion.intent_digest}</dd></dl>}
          {row.completion && <p>Publication: {row.completion.outcome.replaceAll('_', ' ')}. Run: {row.completion.run_id}. This is not a quality verdict.</p>}
        </details>)}
        <button className={button} disabled={busy || stale || history[lane].page.outcome !== 'partial'} data-cursor-lane={historyCursors[lane]} onClick={() => read(lane)}>Next {lane.replaceAll('_', ' ')}</button>
      </section>)}
    </section>}
    {panel === 'Economics' && <section aria-label="Economics" className="space-y-1 tabular-nums">
      <p>All-attempt spend: unknown; invocation coverage is unverified.</p>
      {'source' in cost && cost.source === 'terminal_receipt' && cost.coverage === 'selected_receipt' ? <>
        <p>Source: frozen terminal receipt. Selected records: {cost.selected_count ?? 'unknown'}.</p>
        <p>Selected API cost: {metricText(cost.totals?.api_cost_usd, true)}.</p>
        <p>Selected plan marginal cost: {metricText(cost.totals?.plan_marginal_cost_usd, true)}.</p>
        <p>API-equivalent estimate: {metricText(cost.totals?.api_equivalent_cost_usd, true)}; not spend.</p>
        <p>Input tokens: {metricText(cost.totals?.tokens_in)}. Output tokens: {metricText(cost.totals?.tokens_out)}.</p>
        <p>Cache read tokens: {metricText(cost.totals?.cache_read_tokens)}. Cache write tokens: {metricText(cost.totals?.cache_write_tokens)}.</p>
        {cost.totals && Object.entries(cost.totals).map(([name, metric]) => <p key={name}>{name}: {metric.known_selected ?? 'unknown'} known; {metric.unknown_selected ?? 'unknown'} unknown; {metric.estimated_selected ?? 'unknown'} estimated; {metric.conflicting_selected ?? 'unknown'} conflicting.</p>)}
        <p className="break-all">Receipt: {cost.receipt_digest ?? 'unknown'}. Summary revision: {cost.summary_revision ?? 'unknown'}.</p>
      </> : <p>Selected cost unavailable. No spend is inferred from terminal lifecycle.</p>}
    </section>}
  </section>;
}
