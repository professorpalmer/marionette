import { MetadataOutcomeCounts, MetadataOutcomeLabel, metadataOutcomeLabel } from './MetadataOutcomeChrome';
import MetadataActivityIndicator from './MetadataActivityIndicator';
import NativeTaskDisclosure from './NativeTaskDisclosure';
import MetadataExpertPanels from './MetadataExpertPanels';
import { navigationMatches, peekPendingSwarmNavigation, queuePendingSwarmNavigation, swarmNavigationTarget, takePendingSwarmNavigation } from '../lib/pendingSwarmOpenJob';
import type { SwarmNavigationTarget } from '../lib/pendingSwarmOpenJob';
import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import { selectNativeMetadataControl } from '../lib/jobControl';
import { api } from '../lib/api';
import { jobArtifactKey, selectJobRef } from '../lib/jobArtifacts';
import type { Job } from '../lib/api';
import { useSharedJobMetadata, metadataActivity, metadataJobs } from '../lib/jobMetadataContext';
import { metadataSelectionKey, metadataStreamKey, pmActiveStatuses } from '../lib/jobMetadata';
import { localKey, nativeActiveStatuses, nativeAttentionStatuses } from '../lib/localJobMetadata';
import type { LocalDetail, LocalSummary } from '../lib/localJobMetadata';
import JobCancellationControl from './JobCancellationControl';

const button = 'min-h-11 px-2 text-sm text-muted hover:text-txt focus-visible:outline focus-visible:outline-accent disabled:opacity-50';
export function MetadataStatus() {
  const { store, state } = useSharedJobMetadata();
  const activity = metadataActivity(state);
  const [now, setNow] = useState(Date.now);
  useEffect(() => { const clock = setInterval(() => { if (!document.hidden) setNow(Date.now()); }, 10000); return () => clearInterval(clock); }, []);
  const times = [...Object.values(state.observedAt), ...(state.local.observedAt === null ? [] : [state.local.observedAt]), ...(state.localActive.observedAt === null ? [] : [state.localActive.observedAt])];
  const running = metadataJobs(state).filter(job => !job.local_ref && job.read_status !== 'unavailable' && job.status === 'running').length;
  const oldest = times.length ? Math.max(0, Math.floor((now - Math.min(...times)) / 1000)) : null;
  return <div className="px-2 py-1 text-xs text-muted">
    {running > 0 && <div className="flex items-center gap-2"><span title={`${running} running`} className="h-2 w-2 rounded-full bg-accent animate-pulse" /><span><MetadataActivityIndicator /> {running} running</span><span>observed PM jobs; coverage incomplete</span></div>}
    <p role="status">{activity.label}{oldest === null ? '' : ` · Oldest observation ${oldest}s ago`}</p>
    {state.error && <p role="alert">Job updates unavailable ({state.error.replaceAll('_', ' ')}). Retained observations may be stale.</p>}
    <div className="flex flex-wrap gap-1">
      <button className={button} disabled={state.working} onClick={() => { store.restartTraversal(); void store.readView(); }}>Retry updates</button>
      <button className={button} disabled={state.working} onClick={() => void store.advance()}>Next page</button>
      <button className={button} disabled={state.working || state.view.kind !== 'view'} onClick={() => void store.refreshView()}>Refresh sources</button>
    </div>
    <details><summary className="cursor-pointer focus-visible:outline">Coverage</summary>
      <p>Only observed jobs are shown. Older and undiscovered work may be missing. Native spend is shown only when reported with provenance. Observations do not authorize totals.</p>
      {state.displayLimited && <p>Display window limited to 200 observations, with up to 100 native jobs.</p>}
      <p>Native active: {state.localActive.state} · {state.localActive.missing.join(', ').replaceAll('_', ' ')}</p>
      <p>Native history: {state.local.state} · {state.local.missing.join(', ').replaceAll('_', ' ')}</p>
      {state.streams.map(s => <p key={metadataStreamKey(s.stream)} className="break-all">{s.stream.store.source} / {s.stream.store.state_id}: {s.state} {s.stream.status ?? 'history'}{state.observedAt[metadataStreamKey(s.stream)] ? ` · ${Math.max(0, Math.floor((now - state.observedAt[metadataStreamKey(s.stream)]) / 1000))}s ago` : ' · not observed'}</p>)}
    </details>
  </div>;
}
export function NativeOperatorFacts({ row }: { row: LocalSummary }) {
  const e = row.economics;
  return <div className="space-y-1 text-xs text-muted">
    {row.display && <p>{row.display.label} · {row.kind === 'provider' ? `Model: ${row.display.model || 'unknown'}` : 'No model (native execution)'} · Adapter: {row.display.adapter || 'unknown'}{row.display.truncated ? ' (truncated)' : ''}</p>}
    <p>{row.usage?.kind === 'reported' ? `${row.usage.tokens.toLocaleString()} combined tokens · reported by native job` : 'Token usage unknown'}</p>
    <p>{e.kind === 'unavailable' ? 'Spend unavailable' : `${e.kind === 'estimated' ? 'Estimated spend' : e.kind === 'measured' ? 'Measured spend' : 'Provider spend'}: $${e.spend_usd.toLocaleString(undefined, { maximumFractionDigits: 8 })} · ${e.cost_provenance} pricing · financial receipt`}</p>
    {e.route_forecast_usd !== undefined && <p>Route forecast: ${e.route_forecast_usd.toFixed(4)} · financial receipt estimate; not spend.</p>}
    {e.estimated_savings_usd !== undefined && <p>Estimated savings: ${e.estimated_savings_usd.toFixed(4)} · financial receipt estimate; not measured savings.</p>}
    <p>{row.accounting?.kind === 'declared' ? 'Accounting ownership: declared' : row.accounting?.kind === 'excluded' ? 'Accounting exclusion: reported' : 'Accounting ownership: unresolved'}. This view does not calculate totals.</p>
  </div>;
}
export function MetadataInspection({ job, navigation }: { job: Job; navigation?: SwarmNavigationTarget }) {
  const { state } = useSharedJobMetadata();
  const identity = JSON.stringify([job.metadata_key, job.local_ref, state.contextEpoch]);
  return <SelectedInspection key={identity} job={job} navigation={navigation} />;
}
function SelectedInspection({ job, navigation }: { job: Job; navigation?: SwarmNavigationTarget }) {
  const { store, state } = useSharedJobMetadata();
  const [notice, setNotice] = useState('');
  const [stopping, setStopping] = useState(false);
  const [stopAcknowledged, setStopAcknowledged] = useState(false);
  const mounted = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const local = job.local_ref;
  const listed = local ? state.local.observations.find(o => localKey(o.row.local_ref) === localKey(local)) : null;
  const listedSummary = listed?.row;
  const selectedSummary = local && state.localDetail && localKey(state.localDetail.selection) === localKey(local) ? state.localDetail.observation?.summary : null;
  const nativeSummary = selectedSummary && (!listedSummary || selectedSummary.revision >= listedSummary.revision) ? selectedSummary : listedSummary;
  const pm = [...state.observations, ...state.pins.flatMap(p => p.observation ? [p.observation] : [])].find(o => metadataSelectionKey(o.row.selection) === job.metadata_key);
  const detail = state.detail.kind === 'selected' && metadataSelectionKey(state.detail.selection) === job.metadata_key
    ? state.detail : job.metadata_key ? state.detailCache[job.metadata_key] ?? null : null;
  const candidatePM = pm?.row.selection ?? detail?.selection;
  const previewSelection = state.view.kind === 'view' ? selectJobRef(job, state.view.context.repo, state.view.context.session_id) : null;
  const selectedPM = candidatePM && previewSelection && jobArtifactKey(candidatePM) === jobArtifactKey(previewSelection) ? candidatePM : null;
  const native = local && state.localDetail && localKey(local) === localKey(state.localDetail.selection) ? state.localDetail : null;
  const nativeFresh = nativeSummary && (nativeSummary === selectedSummary
    ? native?.summaryFreshness === 'observed'
    : listed?.freshness === 'observed');
  const inspect = (lane: LocalDetail['lane'] = 'actions') => {
    if (state.working || state.view.kind !== 'view') return;
    if (local) { if (!native || native.lane !== lane) store.selectLocal(local, lane); void store.readLocalDetail(); }
    else if (selectedPM) { if (state.detail.kind !== 'selected' || metadataSelectionKey(state.detail.selection) !== metadataSelectionKey(selectedPM)) store.select(selectedPM); void store.readDetail(); }
  };
  const initialNativeRead = useRef(false);
  useEffect(() => {
    if (initialNativeRead.current || !local || nativeSummary?.kind !== 'provider' || state.working || state.view.kind !== 'view') return;
    initialNativeRead.current = true;
    store.selectLocal(local, 'tasks');
    void store.readLocalDetail();
  }, [local, nativeSummary?.kind, state.working, state.view, store]);
  const initialRoutingRead = useRef(false);
  useEffect(() => {
    if (initialRoutingRead.current || !local || !native?.tasks || state.working) return;
    initialRoutingRead.current = true;
    store.selectLocal(local, 'routing');
    void store.readLocalDetail();
  }, [local, native?.tasks, state.working, store]);
  const attemptedNavigation = useRef<SwarmNavigationTarget | null>(null);
  useEffect(() => {
    if (!navigation?.artifactId || navigation.kind !== 'pm' || !selectedPM || state.working
      || state.view.kind !== 'view' || peekPendingSwarmNavigation() !== navigation
      || attemptedNavigation.current === navigation) return;
    attemptedNavigation.current = navigation;
    store.select(selectedPM);
    void store.readDetail();
  }, [navigation, selectedPM, state.working, state.view, store]);
  const observation = selectedPM ? detail?.observation : undefined;
  const bindings = observation?.tasks.rows.flatMap(t => t.binding ? [t.binding] : []) ?? [];
  const authorizedJob: Job = { ...job, unavailable_fields: ['artifacts'], cancellation_view:
    observation && detail?.freshness === 'observed' && !detail.cursors.task_cursor && observation.tasks.page.outcome === 'complete'
      && bindings.length > 0 && bindings.length === observation.tasks.rows.length
      ? { status: 'complete', limit: 200, bindings } : { status: 'unavailable', limit: 200 } };
  const view = state.view;
  const nativeSelection = local && view.kind === 'view' ? selectNativeMetadataControl(local, view.context) : null;
  const nativeStop = async () => {
    if (!local || !nativeSelection || view.kind !== 'view' || state.working || stopping || job.session_id !== view.context.session_id) return;
    if (!native) store.selectLocal(local);
    const captured = store.getSnapshot();
    const current = () => mounted.current && store.getSnapshot().contextEpoch === captured.contextEpoch;
    setStopping(true);
    setStopAcknowledged(false);
    setNotice('Awaiting stop acknowledgement.');
    try {
      const result = await api.swarmCancel(nativeSelection);
      if (!current()) return;
      if (!result.ok) { setNotice(result.error || 'Stop was refused.'); return; }
      setStopAcknowledged(true);
      setNotice('Stop request accepted; awaiting lifecycle observation.');
      const refresh = store.getSnapshot().epoch === captured.epoch ? await store.readLocalDetail() : 'discarded';
      if (!current()) return;
      const observed = store.getSnapshot().localDetail;
      if (refresh !== 'applied' || observed?.summaryFreshness !== 'observed' || !observed?.observation?.summary) {
        setNotice('Stop request accepted; selected refresh was not confirmed. Retry inspection to observe the outcome.');
      } else if (nativeActiveStatuses.includes(observed.observation.summary.lifecycle)) {
        setNotice('Stop request accepted; awaiting lifecycle observation. Retry inspection to check again.');
      } else {
        setNotice('Stop request accepted; selected lifecycle observation refreshed.');
      }
    } catch {
      if (current()) {
        setNotice('Stop outcome is unconfirmed. Inspect the job before retrying.');
      }
    }
    finally { if (current()) setStopping(false); }
  };
  const stopNotice = stopAcknowledged && nativeFresh && nativeSummary && !nativeActiveStatuses.includes(nativeSummary.lifecycle)
    ? `Stop request accepted; observed lifecycle: ${nativeSummary.lifecycle}.` : notice;
  return <div className="px-2 py-1 space-y-1 text-xs text-muted">
    <button className={button} aria-label={`Job ${job.id}`} onClick={() => {
      void navigator.clipboard.writeText(job.id).then(() => setNotice('Job ID copied.'), () => setNotice('Unable to copy job ID.'));
    }}>Job {job.id}</button>
    <p className="break-all">{job.source} / {local ? `native ${local.incarnation}` : job.job_ref?.state_id} / {job.id}</p>
    {nativeSummary && <p>Native {nativeSummary.kind.replaceAll('_', ' ')} · Actions {nativeSummary.action_count ?? 'unknown'} · Children {nativeSummary.child_count ?? 'unknown'}{nativeSummary.parent_ref ? ` · Parent ${nativeSummary.parent_ref.job_id} / ${nativeSummary.parent_ref.incarnation}` : ' · Parent relationship unknown'}. Receipt presence: {Object.entries(nativeSummary.receipts).filter(([, present]) => present).map(([name]) => name).join(', ') || 'none observed'}.</p>}
    {nativeSummary && <NativeOperatorFacts row={nativeSummary} />}
    {nativeSummary?.kind !== 'provider' && native?.observation?.selected_context && <div>
      <p>{native.observation.selected_context.source === 'command_preview' ? 'Command preview' : 'Requested instruction'}{native.summaryFreshness === 'stale' ? ' (stale)' : ''}</p>
      {native.observation.selected_context.request ? <pre className="whitespace-pre-wrap break-words">{native.observation.selected_context.request.text}{native.observation.selected_context.request.truncated ? '\n(truncated to 2048 UTF-8 bytes)' : ''}</pre> : <p>Request unavailable.</p>}
      {native.observation.selected_context.omission === 'raw_command_not_retained' && <p>Stored preview may be redacted or shortened; raw command is not retained.</p>}
      {native.observation.selected_context.cwd ? <p className="break-all">Working directory: {native.observation.selected_context.cwd.text}{native.observation.selected_context.cwd.truncated ? ' (truncated to 512 UTF-8 bytes)' : ''}</p> : <p>Working directory unavailable.</p>}
    </div>}
    <p>Lifecycle: {nativeSummary?.lifecycle ?? job.status}. {!local && (!observation || observation.cost.kind === 'unavailable') ? 'Cost unavailable.' : ''} {(local ? !nativeFresh : job.read_status === 'unavailable') ? 'Observation is stale.' : ''}</p>
    <div className="flex flex-wrap gap-1">
      <button className={button} disabled={state.working || (!local && !selectedPM)} onClick={() => inspect()}>Inspect {local ? 'actions' : 'tasks and artifacts'}</button>
      {local && <><button className={button} disabled={state.working} onClick={() => inspect('tasks')}>Inspect workers</button><button className={button} disabled={state.working} onClick={() => inspect('routing')}>Inspect routing</button><button className={button} disabled={state.working} onClick={() => inspect('output')}>Inspect output</button><button className={button} disabled={state.working} onClick={() => inspect('children')}>Inspect children</button></>}
    </div>
    {!local && !selectedPM && <p>Artifact preview is unavailable for this selection.</p>}
    {view.kind === 'view' && !local && <JobCancellationControl job={authorizedJob} repo={view.context.repo} sessionId={view.context.session_id} disabled={job.read_status === 'unavailable' || state.working} />}
    {local && view.kind === 'view' && job.session_id === view.context.session_id && <button className={button} disabled={!nativeSelection || !nativeFresh || (nativeSummary && terminal.has(nativeSummary.lifecycle)) || state.working || stopping} onClick={() => void nativeStop()}>Request native stop</button>}
    {local && !nativeSelection && <p>Native stop unavailable: this identity is not supported by the current execution control API.</p>}
    {stopNotice && <p role="status">{stopNotice}</p>}
    {detail?.error && <div><p role="alert">Selected read unavailable. Retry inspection.</p><button className={button} disabled={state.working} onClick={() => inspect()}>Retry</button></div>}
    {state.working && !observation && state.detail.kind === 'selected' && pm && metadataSelectionKey(state.detail.selection) === metadataSelectionKey(pm.row.selection) && <p role="status">Loading artifacts...</p>}
    {observation && <MetadataExpertPanels key={metadataSelectionKey(observation.selection)} detail={observation} navigation={navigation} store={store} busy={state.working} stale={detail?.freshness !== 'observed'} />}
    {native?.tasks && <section aria-label="Native workers">
      {native.tasks.page.revision !== nativeSummary?.revision && <p>Retained task details are stale. Inspect workers to refresh.</p>}
      {native.tasks.rows.map((task, index) => {
        const routing = native.routing;
        const route = native.summaryFreshness === 'observed' && task.task_id && routing?.page.outcome === 'complete' && routing.page.revision === native.tasks?.page.revision
          && routing.page.revision === nativeSummary?.revision && !routing.missing.includes('frontend_routing_limit')
          ? routing.rows.filter(row => row.task_id === task.task_id && row.association !== 'unavailable').at(-1) : undefined;
        return <NativeTaskDisclosure key={`${task.task_id}:${index}`} task={task} route={route}
          kill={nativeSummary?.kind === 'provider' && view.kind === 'view' && job.session_id === view.context.session_id ? {
            disabled: !nativeSelection || !nativeFresh || terminal.has(nativeSummary.lifecycle) || state.working || stopping,
            request: () => void nativeStop(),
          } : undefined} />;
      })}
      {native.routing?.missing.includes('frontend_routing_limit') && <p>Routing display limit reached; final route unavailable.</p>}
      {native.routing?.rows.some(row => row.association === 'unavailable') && <p>Some recorded routes have unavailable task association.</p>}
      {native.routing?.page.outcome !== 'complete' && <p>Final recorded route unavailable until routing traversal completes.</p>}
    </section>}
    {native?.error && <p role="alert">{native.summaryFreshness === 'observed' ? 'Selected lane is stale or unavailable. Retry inspection.' : 'Selected read unavailable. Retry inspection.'}</p>}
    {native?.observation && <>
      {native.laneFreshness === 'stale' && native.observation.rows.length > 0 && <p>Retained {native.observation.lane} rows are stale. Requested lane: {native.lane}.</p>}
      <p>{native.observation.lane}: {native.observation.page.outcome} · {native.observation.total ?? 'Unknown'} retained {native.observation.lane === 'output' ? 'characters' : 'entries'}. Selected observation at revision {native.observation.page.revision}.{nativeSummary && nativeSummary.revision > native.observation.page.revision ? ' Newer job activity observed; inspect again to refresh.' : ''}</p>
      {native.observation.lane === 'actions' && native.observation.rows.map((a, i) => 'unavailable' in a ? <p key={`unavailable:${i}`}>Action unavailable</p> : <p key={`${a.action_id}:${i}`}>{a.worker_id} {a.kind}: {a.goal} · {a.status} {a.error} {a.truncated ? '(truncated)' : ''}</p>)}
      {native.observation.lane === 'output' && <><p>In-memory output only. {native.observation.output?.spilled ? 'Additional output was spilled.' : ''}</p>{native.observation.rows.map(r => <pre className="whitespace-pre-wrap break-words" key={r.offset}>{r.text}</pre>)}</>}
      {native.observation.lane === 'children' && native.observation.rows.map((r, i) => <p key={i}>{'local_ref' in r ? `${r.local_ref.job_id} / ${r.local_ref.incarnation}` : 'Child unavailable'}</p>)}
      <button className={button} disabled={state.working || native.laneFreshness !== 'observed' || native.observation.page.outcome !== 'partial'} onClick={() => void store.readLocalDetail(true)}>Next selected page</button>
    </>}
  </div>;
}
type JobPreferences = { expanded: string[]; dismissed: string[] };
function readPreferences(key: string): JobPreferences {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(key) || 'null');
    if (value && typeof value === 'object' && 'expanded' in value && 'dismissed' in value
      && Array.isArray(value.expanded) && Array.isArray(value.dismissed)) {
      return { expanded: value.expanded.filter((v): v is string => typeof v === 'string').slice(-8),
        dismissed: value.dismissed.filter((v): v is string => typeof v === 'string').slice(-200) };
    }
  } catch { /* Preferences do not affect observed work or control authority. */ }
  return { expanded: [], dismissed: [] };
}
const terminal = new Set(['completed', 'complete', 'done', 'failed', 'cancelled', 'timeout', 'timed_out', 'truncated', 'partial', 'interrupted']);
function isFinished(job: Job): boolean {
  // PM stalled is terminal for liveness but recoverable; native stalled has no terminal guarantee.
  return terminal.has(job.status) || (!job.local_ref && job.status === 'stalled');
}
function isActive(job: Job): boolean {
  return job.read_status !== 'unavailable' && (job.local_ref ? nativeActiveStatuses : pmActiveStatuses).some(status => status === job.status);
}
function isNativeActivity(job: Job): boolean {
  return !!job.local_ref && ['run_command', 'run_command_batch', 'parallel_wave'].includes(job.job_kind ?? '');
}
function lifecycleGroup(job: Job): string {
  if (isNativeActivity(job)) return 'native';
  return job.read_status === 'unavailable' ? 'unknown' : isActive(job) ? 'active' : isFinished(job) ? 'finished' : 'unknown';
}

export default function MetadataJobs({ enabled = true }: { enabled?: boolean }) {
  const { state } = useSharedJobMetadata();
  const target = state.view.kind === 'idle' ? null : state.view.target;
  const key = JSON.stringify([target?.repo, target?.session_id]);
  return <ObservedJobs key={key} preferenceKey={`pmharness.metadata.jobs:${key}`} enabled={enabled} />;
}
function ObservedJobs({ enabled, preferenceKey }: { enabled: boolean; preferenceKey: string }) {
  const { state } = useSharedJobMetadata();
  const [preferences, setPreferences] = useState(() => readPreferences(preferenceKey));
  const [filter, setFilter] = useState('all');
  const [sort, setSort] = useState<'newest' | 'oldest'>('newest');
  const [collapsedGroups, setCollapsedGroups] = useState<string[]>([]);
  const [notice, setNotice] = useState('');
  const jobs = useMemo(() => metadataJobs(state), [state]);
  const rowButtons = useRef(new Map<string, HTMLButtonElement>());
  const previousGroups = useRef(new Map<string, string>());
  const focusedRow = useRef<string | null>(null);
  const [visible, setVisible] = useState(() => !document.hidden);
  useEffect(() => {
    const update = () => setVisible(!document.hidden);
    document.addEventListener('visibilitychange', update);
    return () => document.removeEventListener('visibilitychange', update);
  }, []);
  useLayoutEffect(() => {
    const focused = jobs.find(job => job.metadata_key === focusedRow.current);
    if (enabled && visible && focused && previousGroups.current.has(focused.metadata_key ?? '')
      && previousGroups.current.get(focused.metadata_key ?? '') !== lifecycleGroup(focused)) {
      setCollapsedGroups(groups => groups.filter(group => group !== lifecycleGroup(focused)));
    }
    previousGroups.current = new Map(jobs.map(job => [job.metadata_key ?? '', lifecycleGroup(job)]));
  }, [jobs, enabled, visible]);
  const [pending, setPending] = useState(peekPendingSwarmNavigation);
  const handled = useRef<SwarmNavigationTarget | null>(null);
  const [focusRequest, setFocusRequest] = useState<{ key: string; target: SwarmNavigationTarget } | null>(null);
  const context = state.view.kind === 'idle' ? null : { ...state.view.target, contextEpoch: state.contextEpoch };
  useEffect(() => { try { localStorage.setItem(preferenceKey, JSON.stringify(preferences)); } catch { /* Optional UI preference. */ } }, [preferences, preferenceKey]);
  useEffect(() => {
    const live = new Set(jobs.filter(j => j.read_status !== 'unavailable' && (j.local_ref ? nativeActiveStatuses : pmActiveStatuses).some(status => status === j.status)).map(j => j.metadata_key));
    setPreferences(p => p.dismissed.some(key => live.has(key)) ? { ...p, dismissed: p.dismissed.filter(key => !live.has(key)) } : p);
  }, [jobs]);
  useEffect(() => {
    const onOpen = (event: Event) => {
      if (!(event instanceof CustomEvent) || !event.detail || typeof event.detail.jobId !== 'string') return;
      const jobId = event.detail.jobId.trim();
      if (!jobId) return;
      const queued = peekPendingSwarmNavigation();
      // The typed producer queues before focusing the pane. Never rebuild its identity.
      if (queued && event.detail.target === queued) { setPending(queued); return; }
      if (event.detail.target) return; // A superseded typed event cannot replace a newer click.
      const artifactId = typeof event.detail.artifactId === 'string' ? event.detail.artifactId : undefined;
      const metadataKey = typeof event.detail.metadataKey === 'string' ? event.detail.metadataKey : undefined;
      const matches = jobs.filter(job => job.id === jobId && (!metadataKey || job.metadata_key === metadataKey));
      const target = swarmNavigationTarget(jobId, context, matches.length === 1 ? matches[0] : undefined, artifactId, metadataKey);
      setPending(queuePendingSwarmNavigation(target));
    };
    window.addEventListener('harness-open-swarm-job', onOpen);
    return () => window.removeEventListener('harness-open-swarm-job', onOpen);
  }, [jobs, state.contextEpoch, state.view]);
  useEffect(() => {
    if (!enabled || !visible || !pending || handled.current === pending) return;
    if (peekPendingSwarmNavigation() !== pending) return;
    const matches = jobs.filter(job => navigationMatches(pending, context, job));
    if (matches.length !== 1) {
      setNotice(!pending.context ? 'Target context is unknown. Select the intended job below. Navigation remains pending.' : jobs.filter(job => job.id === pending.jobId).length > 1 ? 'This job ID exists in several sources. Select its exact source below.' : 'Target job is not observed in this view. Navigation remains pending.');
      return;
    }
    const key = matches[0].metadata_key;
    if (!key) return;
    handled.current = pending;
    setFilter('all');
    setCollapsedGroups([]);
    setPreferences(p => ({ expanded: [...p.expanded.filter(k => k !== key), key].slice(-8), dismissed: p.dismissed.filter(k => k !== key) }));
    setFocusRequest({ key, target: pending });
    setNotice(pending.artifactId ? 'Opening the recorded artifact target. If it is outside the loaded page, use Next artifacts; navigation remains pending until it is observed.' : '');
  }, [jobs, pending, enabled, visible, state.contextEpoch, state.view]);
  useLayoutEffect(() => {
    if (!enabled || !visible || !focusRequest || peekPendingSwarmNavigation() !== focusRequest.target
      || !jobs.some(job => job.metadata_key === focusRequest.key && navigationMatches(focusRequest.target, context, job))) return;
    const button = rowButtons.current.get(focusRequest.key);
    if (!button) return;
    button.focus({ preventScroll: true });
    button.scrollIntoView({ block: 'nearest' });
    if (!focusRequest.target.artifactId) {
      takePendingSwarmNavigation(focusRequest.target);
      setPending(p => p === focusRequest.target ? null : p);
    }
    setFocusRequest(null);
  }, [focusRequest, enabled, visible, jobs, state.contextEpoch, state.view]);
  if (!enabled) return <p className="p-2 text-xs text-muted">Job metadata paused for this view.</p>;
  const hidden = jobs.filter(j => isFinished(j) && preferences.dismissed.includes(j.metadata_key ?? ''));
  const createdAt = new Map(state.local.observations.map(o => [localKey(o.row.local_ref), o.row.created_at]));
  const selectedSummary = state.localDetail?.observation?.summary;
  if (selectedSummary && !createdAt.has(localKey(selectedSummary.local_ref))) createdAt.set(localKey(selectedSummary.local_ref), selectedSummary.created_at);
  const shown = jobs.filter(j => !hidden.includes(j)).filter(j => {
    switch (filter) {
      case 'all': return true;
      case 'session': return state.view.kind === 'view' && j.session_id === state.view.context.session_id;
      case 'repo': return !j.cross_project;
      case 'finished': return isFinished(j);
      case 'attention': return j.status === 'interrupted' || (j.local_ref ? nativeAttentionStatuses : ['failed', 'stalled']).includes(j.status);
      case 'active': return isActive(j);
      case 'failed': return ['failed', 'timeout', 'timed_out', 'truncated', 'interrupted'].includes(j.status);
      case 'cancelled': return j.status === 'cancelled';
      case 'complete': return ['completed', 'complete', 'done'].includes(j.status);
      // Lifecycle alone cannot establish a failed check or a trustworthy result.
      case 'untrustworthy': return false;
      default: return false;
    }
  }).sort((a, b) => {
    const left = createdAt.get(a.metadata_key ?? '') ?? null, right = createdAt.get(b.metadata_key ?? '') ?? null;
    if (left !== null && right !== null && left !== right) return sort === 'newest' ? right - left : left - right;
    if (left === null && right !== null) return 1;
    if (left !== null && right === null) return -1;
    return (a.metadata_key ?? '').localeCompare(b.metadata_key ?? '');
  });
  const trackerCount = jobs.filter(job => !isNativeActivity(job)).length;
  const groups = [
    { key: 'active', label: 'Active', rows: shown.filter(j => lifecycleGroup(j) === 'active') },
    { key: 'finished', label: 'Finished', rows: shown.filter(j => lifecycleGroup(j) === 'finished') },
    { key: 'unknown', label: 'Activity unconfirmed', rows: shown.filter(j => lifecycleGroup(j) === 'unknown') },
    { key: 'native', label: 'Native activity', rows: shown.filter(isNativeActivity) },
  ];
  const failedRead = state.error || state.view.kind === 'view' && (state.view.view.availability === 'unavailable'
    || state.streams.some(s => s.state === 'unavailable' || s.state === 'cursor_expired')
    || state.local.state === 'unavailable' || state.local.state === 'expired');
  return <section aria-label="Jobs" className="h-full overflow-auto text-txt">
    <h2 className="px-2 text-sm font-medium"><span>Swarm Tracker</span> ({trackerCount} observed)</h2>
    {trackerCount === 0 && state.view.kind === 'view' && !state.working && <p className="px-2 text-xs text-muted">No swarm jobs observed in this view.</p>}
    <MetadataStatus />
    <div className="grid grid-cols-2 gap-1 px-2 text-sm">
      <select aria-label="Filter swarms" className="min-h-11 w-full min-w-0 bg-panel text-txt focus-visible:outline focus-visible:outline-accent" value={filter} onChange={e => setFilter(e.target.value)}>
        <option value="all">All observed</option><option value="session">This session</option><option value="repo">This repo</option>
        <option value="active">Active</option><option value="attention">Needs attention</option><option value="finished">Finished</option>
        <option value="failed">Failed</option><option value="cancelled">Cancelled</option><option value="complete">Completed lifecycle</option>
        <option value="untrustworthy">Untrustworthy (quality unavailable)</option>
      </select>
      <button className={`${button} w-full min-w-0`} aria-label="Sort swarms" onClick={() => setSort(s => s === 'newest' ? 'oldest' : 'newest')}>{sort === 'newest' ? 'Newest' : 'Oldest'} first</button>
    </div>
    <p className="px-2 text-xs text-muted">Sorting and filters apply only to observed jobs; history coverage is incomplete. Known creation times are ordered within each lifecycle group; undated jobs remain last. PM creation times are unavailable.</p>
    <div className="flex flex-wrap items-center gap-1 px-2 text-sm">
      <button className={button} onClick={() => setPreferences(p => ({ ...p, dismissed: [...new Set([...p.dismissed, ...groups.filter(g => !collapsedGroups.includes(g.key)).flatMap(g => g.rows).filter(j => isFinished(j) && j.read_status !== 'unavailable').flatMap(j => j.metadata_key ? [j.metadata_key] : [])])].slice(-200) }))}>Hide finished</button>
      {hidden.length > 0 && <button className={button} onClick={() => setPreferences(p => ({ ...p, dismissed: [] }))}>Show {hidden.length} hidden</button>}
      {filter !== 'all' && <button className={button} onClick={() => setFilter('all')}>Clear filter</button>}
    </div>
    {filter === 'untrustworthy' && <p role="status" className="px-2 text-sm text-muted">Quality cannot be assessed from the current metadata contract. Failed lifecycle is separate from failed verification; no trustworthy or untrustworthy result is inferred.</p>}
    {filter === 'complete' && <p className="px-2 text-xs text-muted">Completed lifecycle does not establish successful verification.</p>}
    {notice && <p role="status" className="px-2 text-sm text-muted">{notice}</p>}
    <MetadataOutcomeCounts jobs={shown} />
    {groups.flatMap(group => group.rows.length === 0 ? [] : [
      <button key={`group:${group.key}`} className={`${button} w-full text-left font-medium`} aria-expanded={!collapsedGroups.includes(group.key)} onClick={() => setCollapsedGroups(current => current.includes(group.key) ? current.filter(key => key !== group.key) : [...current, group.key])}>{group.label} ({group.rows.length} observed)</button>,
      ...group.rows.map(job => {
        const key = job.metadata_key ?? '', open = preferences.expanded.includes(key);
        // Keep one keyed sibling list so group changes preserve inspector state and focus.
        return <div className="border-b border-edge" key={key} hidden={collapsedGroups.includes(group.key)} data-job-id={job.id} data-job-source={job.source}
          onFocus={() => { focusedRow.current = key; }} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) focusedRow.current = null; }}>
          <button ref={element => { if (element) rowButtons.current.set(key, element); else rowButtons.current.delete(key); }} className={`${button} flex items-center w-full text-left`} aria-label={`${job.goal} · ${metadataOutcomeLabel(job.status)}`} aria-expanded={open} onClick={() => setPreferences(p => ({ ...p, expanded: open ? p.expanded.filter(k => k !== key) : [...p.expanded, key].slice(-8) }))}>{!job.local_ref && job.read_status !== 'unavailable' && job.status === 'running' && <MetadataActivityIndicator />}<span className="truncate">{job.goal}</span><span className="shrink-0"> · <MetadataOutcomeLabel status={job.status} /></span></button>
          {job.read_status === 'unavailable' && <p className="px-2 text-xs text-muted">Retained observation is stale; current lifecycle is unconfirmed.</p>}
          {job.status === 'stalled' && <p className="px-2 text-xs text-muted">{job.local_ref ? 'May still be active; terminal state unconfirmed.' : 'Finished for liveness; recoverable.'}</p>}
          {isFinished(job) && job.read_status !== 'unavailable' && <button className={button} aria-label={`Dismiss from tracker: ${job.goal}`} onClick={() => setPreferences(p => ({ ...p, dismissed: [...p.dismissed.filter(k => k !== key), key].slice(-200) }))}>Dismiss</button>}
          {open && <MetadataInspection job={job} navigation={enabled && visible && pending && handled.current === pending && navigationMatches(pending, context, job) ? pending : undefined} />}
        </div>;
      }),
    ])}
    {!shown.length && filter !== 'untrustworthy' && <p className="p-2 text-sm text-muted">{failedRead
      ? 'Job observations are unavailable. Retry updates; an empty view does not establish no work.'
      : state.view.kind !== 'view' || !jobs.length && state.working
        ? <><span>Loading swarm jobs...</span> Waiting for job metadata; no lifecycle result is known yet.</>
        : hidden.length && filter === 'all' ? 'Observed finished jobs are hidden. Show hidden jobs to restore them.'
          : !jobs.length ? 'No jobs observed in this view. Older or undiscovered work may still exist.'
            : 'No jobs observed in this filter. Coverage may be incomplete.'}</p>}
  </section>;
}
