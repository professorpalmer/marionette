import { SessionWorkerUsage } from './SessionWorkerUsage';
import { expertHeaderModel, expertJobModel } from '../lib/expertRoutingFacts';
import { expertJobQuality, stickyJobQuality } from '../lib/expertOutcomeFacts';
import { ExpertCost } from './ExpertCurrentFacts';
import { failedOutcomeStatuses, MetadataOutcomeLabel, metadataOutcomeLabel } from './MetadataOutcomeChrome';
import MetadataActivityIndicator from './MetadataActivityIndicator';
import CompactSwarmDashboard from './CompactSwarmDashboard';
import MetadataExpertPanels from './MetadataExpertPanels';
import { navigationMatches, peekPendingSwarmNavigation, queuePendingSwarmNavigation, swarmNavigationTarget, takePendingSwarmNavigation } from '../lib/pendingSwarmOpenJob';
import type { SwarmNavigationTarget } from '../lib/pendingSwarmOpenJob';
import { useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Activity, AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, Circle, ExternalLink, Loader2, Network, X, XCircle } from 'lucide-react';
import { selectNativeMetadataControl } from '../lib/jobControl';
import { api } from '../lib/api';
import { jobArtifactKey, selectJobRef } from '../lib/jobArtifacts';
import type { Job } from '../lib/api';
import { filterJobsByScope, JOB_SCOPE_CHANGED_EVENT, loadJobScope, saveJobScope, type JobScope } from '../lib/jobScope';
// Jobs rail is a compact native strip. The Puppetmaster dashboard stays a
// webpage — Board pops it out. Row click never replaces this list with an iframe.
import { useSharedJobMetadata, metadataActivity, metadataJobs, metadataViewSessionId, currentExpert, currentHeader } from '../lib/jobMetadataContext';
import { dashboardJobId, isSwarmTrackerJob } from '../lib/jobClassification';
import { jobDisplayTitle } from '../lib/jobDisplayTitle';
import { dashboardLocateError, dashboardUnavailableMessage, jobsListEmptyTruth } from '../lib/jobsDashboard';
import { lastSelectedProjectRoot } from '../lib/panelTransition';
import { openAgentUrlExternal } from '../lib/agentLinks';
import { canonicalExpertSelection, expertLookupKey, metadataSelectionKey, metadataStreamKey, pmActiveStatuses } from '../lib/jobMetadata';
import { localKey, nativeActiveStatuses, nativeAttentionStatuses } from '../lib/localJobMetadata';
import type { LocalDetail, LocalRoute, LocalSummary } from '../lib/localJobMetadata';
import type { MetadataActionResult } from '../lib/useJobMetadata';
import JobCancellationControl from './JobCancellationControl';

const button = 'px-1.5 py-0.5 text-[10.5px] text-muted hover:text-txt focus-visible:outline focus-visible:outline-accent disabled:opacity-50';
const compactSelect = 'w-full h-6 rounded border border-edge bg-panel2/40 px-1.5 text-[10px] text-muted focus:outline-none focus:border-accent/60';
export function MetadataStatus({ hidden = false }: { hidden?: boolean }) {
  const { store, state } = useSharedJobMetadata();
  const activity = metadataActivity(state);
  const [now, setNow] = useState(Date.now);
  useEffect(() => { const clock = setInterval(() => { if (!document.hidden) setNow(Date.now()); }, 10000); return () => clearInterval(clock); }, []);
  const times = [...Object.values(state.observedAt), ...(state.local.observedAt === null ? [] : [state.local.observedAt]), ...(state.localActive.observedAt === null ? [] : [state.localActive.observedAt])];
  const oldest = times.length ? Math.max(0, Math.floor((now - Math.min(...times)) / 1000)) : null;
  return <div className={hidden ? 'sr-only' : 'px-2 py-1 text-[10px] text-muted'}>
    {!hidden && state.error && <p role="alert">Job updates unavailable ({state.error.replaceAll('_', ' ')}). Retained observations may be stale.</p>}
    <p role="status" className={activity.count > 0 ? "text-[10px] text-accent" : "sr-only"}>{activity.label}{oldest === null ? '' : ` · Oldest observation ${oldest}s ago`}</p>
    <div className="flex flex-wrap gap-1">
      <button className={button} disabled={state.working} onClick={() => { store.restartTraversal(); void store.readView(); }}>Retry updates</button>
      <button className={button} disabled={state.working} onClick={() => void store.advance()}>Next page</button>
      <button className={button} disabled={state.working || state.view.kind !== 'view'} onClick={() => void store.refreshView()}>Refresh sources</button>
    </div>
    <details className="mt-0.5"><summary className="cursor-pointer focus-visible:outline text-faint">Coverage</summary>
      <p>Only observed jobs are shown. Older and undiscovered work may be missing. Native spend is shown only when reported with provenance. Observations do not authorize totals.</p>
      {state.displayLimited && <p>Display window limited to 200 observations, with up to 100 native jobs.</p>}
      <p>Native active: {state.localActive.state} · {state.localActive.missing.join(', ').replaceAll('_', ' ')}</p>
      <p>Native history: {state.local.state} · {state.local.missing.join(', ').replaceAll('_', ' ')}</p>
      <details><summary className="cursor-pointer focus-visible:outline">Sources</summary>
        {state.streams.map(s => <p key={metadataStreamKey(s.stream)} className="break-all">{s.stream.store.source} / {s.stream.store.state_id}: {s.state} {s.stream.status ?? 'history'}{state.observedAt[metadataStreamKey(s.stream)] ? ` · ${Math.max(0, Math.floor((now - state.observedAt[metadataStreamKey(s.stream)]) / 1000))}s ago` : ' · not observed'}</p>)}
      </details>
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
function relativeSince(ts: number | string | null | undefined, now: number): string {
  if (ts == null || ts === '') return '';
  const t = typeof ts === 'number' ? (ts < 1e12 ? ts * 1000 : ts) : Date.parse(String(ts));
  if (!Number.isFinite(t)) return '';
  const secs = Math.max(0, Math.round((now - t) / 1000));
  if (secs < 60) return `${secs}s ago`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  return `${Math.floor(mins / 60)}h ago`;
}
export function MetadataInspection({ job, navigation, compact = false, onReveal, onOpenDashboard, deferAutoRead = false }: {
  job: Job; navigation?: SwarmNavigationTarget; compact?: boolean; onReveal?: () => void; onOpenDashboard?: () => void; deferAutoRead?: boolean;
}) {
  const identity = job.local_ref ? JSON.stringify(['local', job.local_ref.job_id]) : (job.metadata_key ?? job.id);
  return <SelectedInspection key={identity} job={job} navigation={navigation} compact={compact} onReveal={onReveal} onOpenDashboard={onOpenDashboard} deferAutoRead={deferAutoRead} />;
}
function SelectedInspection({ job, navigation, compact, onReveal, onOpenDashboard, deferAutoRead }: {
  job: Job; navigation?: SwarmNavigationTarget; compact: boolean; onReveal?: () => void; onOpenDashboard?: () => void; deferAutoRead: boolean;
}) {
  const { store, state } = useSharedJobMetadata();
  const [notice, setNotice] = useState('');
  const [stopping, setStopping] = useState(false);
  const [stopAcknowledged, setStopAcknowledged] = useState(false);
  const [dialogClosed, setDialogClosed] = useState(false);
  // Compact never hosts the inspection overlay.
  const showDump = !compact && !dialogClosed;
  const [now, setNow] = useState(Date.now);
  const revealInspection = () => { onReveal?.(); setDialogClosed(false); };
  useEffect(() => { if (!compact && navigation?.artifactId) { onReveal?.(); setDialogClosed(false); } }, [compact, navigation, onReveal]);
  const mounted = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  useEffect(() => {
    const clock = setInterval(() => { if (!document.hidden) setNow(Date.now()); }, 10000);
    return () => clearInterval(clock);
  }, []);
  const local = job.local_ref;
  const listed = local ? state.local.observations.find(o => localKey(o.row.local_ref) === localKey(local)) : null;
  const listedSummary = listed?.row;
  const selectedSummary = local && state.localDetail && localKey(state.localDetail.selection) === localKey(local) ? state.localDetail.observation?.summary : null;
  const nativeSummary = selectedSummary && (!listedSummary || selectedSummary.revision >= listedSummary.revision) ? selectedSummary : listedSummary;
  const canonical = nativeSummary?.canonical;
  const canonicalSelection = canonical && state.view.kind === 'view'
    ? canonicalExpertSelection(canonical, state.view.context.repo)
    : null;
  const detailKeys = [
    job.metadata_key,
    canonicalSelection ? metadataSelectionKey(canonicalSelection) : undefined,
  ].filter((key): key is string => Boolean(key));
  const pm = [...state.observations, ...state.pins.flatMap(p => p.observation ? [p.observation] : [])].find(o => detailKeys.includes(metadataSelectionKey(o.row.selection)));
  const detail = state.detail.kind === 'selected' && detailKeys.includes(metadataSelectionKey(state.detail.selection))
    ? state.detail : detailKeys.map(key => state.detailCache[key]).find(Boolean) ?? null;
  const candidatePM = pm?.row.selection ?? detail?.selection;
  const previewSelection = state.view.kind === 'view' ? selectJobRef(job, state.view.context.repo, state.view.context.session_id) : null;
  const selectedPM = candidatePM && previewSelection && jobArtifactKey(candidatePM) === jobArtifactKey(previewSelection) ? candidatePM : null;
  const native = local && state.localDetail && localKey(local) === localKey(state.localDetail.selection) ? state.localDetail : null;
  const nativeFresh = nativeSummary && (nativeSummary === selectedSummary
    ? native?.summaryFreshness === 'observed'
    : listed?.freshness === 'observed');
  const initialPMRead = useRef(false);
  const inspect = (lane: LocalDetail['lane'] = 'actions') => {
    if (compact) return;
    revealInspection();
    if (state.view.kind !== 'view') return;
    if (local) { if (!native || native.lane !== lane) store.selectLocalIfCurrent(local, lane); void store.readLocalDetail(); }
    else if (selectedPM) {
      initialPMRead.current = true;
      if (state.detail.kind !== 'selected' || metadataSelectionKey(state.detail.selection) !== metadataSelectionKey(selectedPM)) store.select(selectedPM);
      void store.readDetail();
    }
  };
  // First read selects the PM job. A detail left errored or stale by a failed lane read is
  // re-read on a 4s cadence instead of staying blank until a manual Retry.
  const lastPMRead = useRef(0);
  const detailNeedsRead = !detail?.observation || detail.error !== null || detail.freshness === 'stale';
  useEffect(() => {
    if (deferAutoRead || !selectedPM || !detailNeedsRead || state.working || state.view.kind !== 'view') return;
    const at = Date.now();
    if (initialPMRead.current && at - lastPMRead.current < 4000) return;
    const first = !initialPMRead.current;
    initialPMRead.current = true;
    lastPMRead.current = at;
    // The store is single-flight. A read that lost to the list cadence or another
    // job's refresh must not spend this 4s slot: re-arm so the next working=false
    // publish retries at once instead of leaving a freshly expanded row blank.
    const rearmIfSkipped = (result: Promise<MetadataActionResult>) => {
      void result.then(outcome => { if (outcome === 'skipped') { lastPMRead.current = 0; if (first) initialPMRead.current = false; } });
    };
    try {
      const own = state.detail.kind === 'selected' && metadataSelectionKey(state.detail.selection) === metadataSelectionKey(selectedPM);
      if (own) rearmIfSkipped(store.readDetail());
      else if (first || state.detail.kind === 'none') { store.select(selectedPM); rearmIfSkipped(store.readDetail()); }
      else rearmIfSkipped(store.hydrateDetail(selectedPM, { prefetch: true })); // cache-only; do not clear selected stale
    } catch {
      initialPMRead.current = false;
    }
  }, [deferAutoRead, selectedPM, detailNeedsRead, state.working, state.view, state.detail, store]);
  // A local alias with a canonical PM ref hydrates that job cache-only: it never takes over
  // the selection. While the job is live it re-hydrates when the listed row's revision moves
  // past the cached detail or every 4s, so the roster follows routing and completion instead
  // of freezing on the first observation. A selected inspection owns its own refresh cadence.
  const canonicalKey = canonicalSelection ? metadataSelectionKey(canonicalSelection) : '';
  const canonicalRef = useRef(canonicalSelection);
  canonicalRef.current = canonicalSelection;
  useEffect(() => {
    if (deferAutoRead || !canonicalKey || state.view.kind !== 'view') return;
    let last: number | null = null;
    const attempt = () => {
      const selection = canonicalRef.current;
      const snap = store.getSnapshot();
      if (!selection || snap.working || snap.view.kind !== 'view') return;
      if (snap.detail.kind === 'selected' && metadataSelectionKey(snap.detail.selection) === canonicalKey) return;
      const current = snap.detailCache[canonicalKey];
      const at = Date.now();
      const cadence = last === null || at - last >= 4000;
      let due = !current;
      if (current && !current.observation) due = cadence;
      else if (current?.observation) {
        const hydrated = Math.max(current.observation.tasks.page.revision, current.observation.artifacts.page.revision);
        const listedRevision = snap.observations.find(o => metadataSelectionKey(o.row.selection) === canonicalKey)?.row.revision ?? 0;
        const live = current.observation.lifecycle === null || !terminal.has(current.observation.lifecycle);
        due = listedRevision > hydrated || ((live || current.error !== null || current.freshness === 'stale') && cadence);
      }
      if (!due || (last !== null && at - last < 2000)) return;
      last = at;
      void store.hydrateDetail(selection, { prefetch: true });
    };
    attempt();
    const tick = setInterval(() => { if (!document.hidden) attempt(); }, 2000);
    const unsubscribe = store.subscribe(attempt);
    return () => { clearInterval(tick); unsubscribe(); };
  }, [deferAutoRead, canonicalKey, state.view.kind, store]);
  const initialNativeRead = useRef(false);
  const initialRoutingRead = useRef(false);
  useEffect(() => {
    initialNativeRead.current = false;
    initialRoutingRead.current = false;
    setStopping(false);
    setStopAcknowledged(false);
    setNotice('');
  }, [local?.incarnation]);
  useEffect(() => {
    if (deferAutoRead || initialNativeRead.current || !local || !nativeSummary || ['run_command', 'run_command_batch', 'parallel_wave'].includes(nativeSummary.kind) || state.working) return;
    if (!store.selectLocalIfCurrent(local, 'tasks')) return;
    initialNativeRead.current = true;
    void store.readLocalDetail();
  }, [local, nativeSummary?.kind, state.working, state.view, store]);
  useEffect(() => {
    if (deferAutoRead || initialRoutingRead.current || !local || !native?.tasks || state.working) return;
    if (!store.selectLocalIfCurrent(local, 'routing')) return;
    initialRoutingRead.current = true;
    void store.readLocalDetail();
  }, [local, native?.tasks, state.working, state.view, store]);
  const attemptedNavigation = useRef<SwarmNavigationTarget | null>(null);
  useEffect(() => {
    if (compact || deferAutoRead || !navigation?.artifactId || navigation.kind !== 'pm' || !selectedPM || state.working
      || state.view.kind !== 'view' || peekPendingSwarmNavigation() !== navigation
      || attemptedNavigation.current === navigation) return;
    attemptedNavigation.current = navigation;
    store.select(selectedPM);
    void store.readDetail();
  }, [compact, deferAutoRead, navigation, selectedPM, state.working, state.view, store]);
  const observation = selectedPM ?? canonicalSelection ? detail?.observation : undefined;
  const detailFresh = detail?.freshness === 'observed' && !detail.error
    && !state.observations.some(o => observation && metadataSelectionKey(o.row.selection) === metadataSelectionKey(observation.selection) && o.freshness === 'stale');
  const bindings = observation?.tasks.rows.flatMap(t => t.binding ? [t.binding] : []) ?? [];
  const authorizedJob: Job = { ...job, unavailable_fields: ['artifacts'], cancellation_view:
    observation && detailFresh && detail?.kind === 'selected' && !detail.cursors.task_cursor && observation.tasks.page.outcome === 'complete'
      && bindings.length > 0 && bindings.length === observation.tasks.rows.length
      ? { status: 'complete', limit: 200, bindings } : { status: 'unavailable', limit: 200 } };
  const view = state.view;
  const nativeSelection = local && view.kind === 'view' ? selectNativeMetadataControl(local, view.context) : null;
  const nativeStop = async () => {
    if (!local || !nativeSelection || view.kind !== 'view' || state.working || stopping || job.session_id !== view.context.session_id) return;
    if (!native) store.selectLocalIfCurrent(local);
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
  const expertKey = expertLookupKey(
    job.metadata_key,
    canonical,
    state.view.kind === 'view' ? state.view.context.repo : undefined,
  );
  const expert = currentExpert(state, expertKey);
  const nativeRows = native?.tasks?.rows ?? [];
  const preferCanonicalRoster = !!canonical && !!expert && expert.kind !== 'unavailable' && expert.tasks.length > 0;
  const expertRows = (preferCanonicalRoster || nativeRows.length === 0) && expert && expert.kind !== 'unavailable' ? expert.tasks : [];
  const taskStatusById = new Map((observation?.tasks.rows ?? []).map(task => [task.id, task.status ?? 'unknown']));
  for (const task of expertRows) {
    if (!taskStatusById.has(task.id)) taskStatusById.set(task.id, observation?.lifecycle ?? job.status);
  }
  const headerJobId = canonical?.job_ref.job_id ?? job.id;
  const canonicalGoal = observation?.display?.kind === 'available' ? observation.display.goal_preview : undefined;
  const instructionFallback = native?.observation?.selected_context?.request?.text?.trim() || undefined;
  const cardTitle = canonicalGoal || (canonical ? instructionFallback : undefined) || jobDisplayTitle(job);
  const adapter = (preferCanonicalRoster ? expertRows[0]?.adapter : undefined)
    || nativeSummary?.display?.adapter || nativeRows[0]?.adapter || expertRows[0]?.adapter || '';
  const activityAt = nativeSummary && nativeActiveStatuses.includes(nativeSummary.lifecycle)
    ? nativeSummary.updated_at ?? nativeSummary.created_at : null;
  const since = relativeSince(activityAt, now);
  const workerCount = preferCanonicalRoster
    ? expertRows.length
    : nativeRows.length || expertRows.length || nativeSummary?.task_count || 0;
  const workerKill = local && view.kind === 'view' && job.session_id === view.context.session_id ? {
    disabled: !nativeSelection || !nativeFresh || (nativeSummary != null && terminal.has(nativeSummary.lifecycle)) || state.working || stopping,
    request: () => void nativeStop(),
  } : undefined;
  const costHeader = (expert && expert.kind !== 'unavailable' ? expert.live_economics ?? expert.header : null)
    ?? (expertKey ? currentHeader(state, expertKey) : null)
    ?? null;
  const nativeRouteCoverage = native?.routing
    ? native.summaryFreshness === 'observed' && nativeFresh
      && native.routing.page.outcome === 'complete' && native.routing.page.revision === nativeSummary?.revision
      && native.routing.page.revision === native.tasks?.page.revision
      && !native.routing.missing.includes('frontend_routing_limit') ? 'complete' as const : 'partial' as const
    : 'unavailable' as const;
  const nativeRouteByTask = useMemo(() => {
    const routes = new Map<string, LocalRoute>();
    for (const route of native?.routing?.rows ?? []) {
      if (route.task_id && route.association !== 'unavailable') routes.set(route.task_id, route);
    }
    return routes;
  }, [native?.routing?.rows]);
  const dump = showDump ? <>
      <p className="break-all">{job.source} / {local ? `native ${local.incarnation}` : job.job_ref?.state_id} / {job.id}</p>
      {nativeSummary && <p>Native {nativeSummary.kind.replaceAll('_', ' ')} · Actions {nativeSummary.action_count ?? 'unknown'} · Children {nativeSummary.child_count ?? 'unknown'}{nativeSummary.parent_ref ? ` · Parent ${nativeSummary.parent_ref.job_id} / ${nativeSummary.parent_ref.incarnation}` : ' · Parent relationship unknown'}. Receipt presence: {Object.entries(nativeSummary.receipts).filter(([, present]) => present).map(([name]) => name).join(', ') || 'none observed'}.</p>}
      {nativeSummary && <NativeOperatorFacts row={nativeSummary} />}
      {nativeSummary?.kind !== 'provider' && native?.observation?.selected_context && <div>
        <p>{native.observation.selected_context.source === 'command_preview' ? 'Command preview' : 'Requested instruction'}{native.summaryFreshness === 'stale' ? ' (stale)' : ''}</p>
        {native.observation.selected_context.request ? <pre className="whitespace-pre-wrap break-words">{native.observation.selected_context.request.text}{native.observation.selected_context.request.truncated ? '\n(truncated to 2048 UTF-8 bytes)' : ''}</pre> : <p>Request unavailable.</p>}
        {native.observation.selected_context.omission === 'raw_command_not_retained' && <p>Stored preview may be redacted or shortened; raw command is not retained.</p>}
        {native.observation.selected_context.cwd ? <p className="break-all">Working directory: {native.observation.selected_context.cwd.text}{native.observation.selected_context.cwd.truncated ? ' (truncated to 512 UTF-8 bytes)' : ''}</p> : <p>Working directory unavailable.</p>}
      </div>}
      {!local && job.metadata_key && !observation?.expert && <ExpertCost header={currentHeader(state, job.metadata_key) ?? null} />}
      <p>Lifecycle: {nativeSummary?.lifecycle ?? job.status}. {!local && (!observation || observation.cost.kind === 'unavailable') ? 'Cost unavailable.' : ''} {(local ? !nativeFresh : job.read_status === 'unavailable') ? 'Observation is stale.' : ''}</p>
      {state.working && !observation && state.detail.kind === 'selected' && pm && metadataSelectionKey(state.detail.selection) === metadataSelectionKey(pm.row.selection) && <p role="status">Loading artifacts...</p>}
      {observation && <MetadataExpertPanels compact={compact} key={metadataSelectionKey(observation.selection)} detail={observation} navigation={navigation} store={store} busy={state.working} stale={!detailFresh} />}
      {native?.tasks && <>
        {native.tasks.page.revision !== nativeSummary?.revision && <p>Retained task details are stale. Inspect workers to refresh.</p>}
        {native.routing?.missing.includes('frontend_routing_limit') && <p>Routing display limit reached; final route unavailable.</p>}
        {native.routing?.rows.some(row => row.association === 'unavailable') && <p>Some recorded routes have unavailable task association.</p>}
        {native.routing?.page.outcome !== 'complete' && <p>Final recorded route unavailable until routing traversal completes.</p>}
      </>}
      {native?.observation && <>
        {native.laneFreshness === 'stale' && native.observation.rows.length > 0 && <p>Retained {native.observation.lane} rows are stale. Requested lane: {native.lane}.</p>}
        <p>{native.observation.lane}: {native.observation.page.outcome} · {native.observation.total ?? 'Unknown'} retained {native.observation.lane === 'output' ? 'characters' : 'entries'}. Selected observation at revision {native.observation.page.revision}.{nativeSummary && nativeSummary.revision > native.observation.page.revision ? ' Newer job activity observed; inspect again to refresh.' : ''}</p>
        {native.observation.lane === 'actions' && native.observation.rows.map((a, i) => 'unavailable' in a ? <p key={`unavailable:${i}`}>Action unavailable</p> : <p key={`${a.action_id}:${i}`}>{a.worker_id} {a.kind}: {a.goal} · {a.status} {a.error} {a.truncated ? '(truncated)' : ''}</p>)}
        {native.observation.lane === 'output' && <><p>In-memory output only. {native.observation.output?.spilled ? 'Additional output was spilled.' : ''}</p>{native.observation.rows.map(r => <pre className="whitespace-pre-wrap break-words" key={r.offset}>{r.text}</pre>)}</>}
        {native.observation.lane === 'children' && native.observation.rows.map((r, i) => <p key={i}>{'local_ref' in r ? `${r.local_ref.job_id} / ${r.local_ref.incarnation}` : 'Child unavailable'}</p>)}
        <button className={button} disabled={state.working || native.laneFreshness !== 'observed' || native.observation.page.outcome !== 'partial'} onClick={() => void store.readLocalDetail(true)}>Next selected page</button>
      </>}
    </> : null;
  return <div className="px-2 pb-2 pt-1 flex flex-col gap-2 bg-panel2/10 text-xs text-muted">
    <div className="flex flex-col gap-1.5 border-b border-edge/20 pb-2">
      <button className="self-start font-mono text-[9px] text-faint hover:text-muted" aria-label={`Job ${headerJobId}`} onClick={() => {
        void navigator.clipboard.writeText(headerJobId).then(() => setNotice('Job ID copied.'), () => setNotice('Unable to copy job ID.'));
      }}>Job {headerJobId}</button>
      {compact && !showDump && <ExpertCost header={costHeader} now={now} compact />}
      {adapter && <p className="text-faint lowercase">{adapter}</p>}
      {since && <div className="flex items-center gap-1 text-[9px] text-faint tabular-nums">
        <Activity size={9} className="text-accent/60 animate-pulse" />
        {since}
      </div>}
    </div>
    {workerCount > 0 && <CompactSwarmDashboard
      title={cardTitle} lifecycle={terminal.has(job.status) ? job.status : observation?.lifecycle ?? nativeSummary?.lifecycle ?? job.status}
      workerStatuses={taskStatusById}
      expert={(preferCanonicalRoster || nativeRows.length === 0) && expert && expert.kind !== 'unavailable' ? expert : undefined}
      headerModel={expertHeaderModel(currentHeader(state, expertKey)) ?? nativeSummary?.display?.model ?? undefined}
      nativeTasks={preferCanonicalRoster ? undefined : nativeRows}
      nativeRoutes={preferCanonicalRoster ? undefined : nativeRouteByTask}
      routeCoverage={preferCanonicalRoster ? undefined : (nativeRows.length ? nativeRouteCoverage : undefined)}
      artifactCount={preferCanonicalRoster
        ? (observation?.artifact_count ?? expert?.artifacts.filter(a => a.type.toUpperCase() !== 'ROUTING').length ?? null)
        : (nativeSummary?.artifact_count ?? observation?.artifact_count ?? null)}
      workerCount={preferCanonicalRoster
        ? (observation?.task_count ?? expert?.header?.selected_workers ?? expertRows.length)
        : (nativeSummary?.task_count ?? observation?.task_count ?? job.task_count ?? null)}
      workerCoverage={preferCanonicalRoster
        ? (expert?.coverage.tasks === 'complete' ? 'complete' : 'partial')
        : local ? nativeFresh && native?.tasks?.page.outcome === 'complete'
        && native.tasks.page.revision === nativeSummary?.revision ? 'complete' : 'partial'
        : expert?.coverage.tasks === 'complete' ? 'complete' : 'partial'}
      usage={costHeader?.usage?.tokens ?? (nativeSummary?.usage?.kind === 'reported' ? nativeSummary.usage.tokens : null)}
      cancel={preferCanonicalRoster ? undefined : (nativeRows.length ? workerKill : undefined)}
    />}
    {compact && onOpenDashboard && <div className="flex flex-wrap gap-1">
      <button type="button" className={button} onClick={onOpenDashboard}>See in Puppetmaster dashboard</button>
    </div>}
    {!compact && <div className="flex flex-wrap gap-1">
      <button className={button} disabled={!local && !selectedPM} onClick={() => inspect()}>Inspect {local ? 'actions' : 'tasks and artifacts'}</button>
      {local && <><button className={button} onClick={() => inspect('tasks')}>Inspect workers</button><button className={button} onClick={() => inspect('routing')}>Inspect routing</button><button className={button} onClick={() => inspect('output')}>Inspect output</button><button className={button} onClick={() => inspect('children')}>Inspect children</button></>}
    </div>}
    {!compact && !local && !selectedPM && <p>Artifact preview is unavailable for this selection.</p>}
    {view.kind === 'view' && !local && !deferAutoRead && <JobCancellationControl job={authorizedJob} repo={view.context.repo} sessionId={view.context.session_id} disabled={job.read_status === 'unavailable' || state.working} />}
    {!compact && !deferAutoRead && local && view.kind === 'view' && job.session_id === view.context.session_id && <button className={button} disabled={!nativeSelection || !nativeFresh || (nativeSummary && terminal.has(nativeSummary.lifecycle)) || state.working || stopping} onClick={() => void nativeStop()}>Request native stop</button>}
    {local && !nativeSelection && <p>Native stop unavailable: this identity is not supported by the current execution control API.</p>}
    {stopNotice && <p role="status">{stopNotice}</p>}
    {detail?.error && <div><p role="alert">Selected read unavailable. Retry inspection.</p><button className={button} disabled={state.working} onClick={() => { if (compact) { if (selectedPM) { store.select(selectedPM); void store.readDetail(); } } else inspect(); }}>Retry</button></div>}
    {native?.error && <p role="alert">{native.summaryFreshness === 'observed' ? 'Selected lane is stale or unavailable. Retry inspection.' : 'Selected read unavailable. Retry inspection.'}</p>}
    {!compact && dump}
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
  return (job.local_ref ? nativeActiveStatuses : pmActiveStatuses).some(status => status === job.status);
}
function isLiveObservation(job: Job): boolean {
  return job.read_status !== 'unavailable' && isActive(job);
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
  const { store, state } = useSharedJobMetadata();
  const [preferences, setPreferences] = useState(() => readPreferences(preferenceKey));
  const [filter, setFilter] = useState('all');
  const [sort, setSort] = useState<'newest' | 'oldest'>('newest');
  const [jobScope, setJobScope] = useState<JobScope>(loadJobScope);
  const [finishedOpen, setFinishedOpen] = useState(true);

  const [notice, setNotice] = useState('');
  const liveJobs = useMemo(() => metadataJobs(state).filter(isSwarmTrackerJob), [state]);
  const retainedJobs = useRef<Job[]>([]);
  if (liveJobs.length > 0) retainedJobs.current = liveJobs;
  const discoveryIdle = state.view.kind === 'view' && !state.working && !state.error
    && !state.streams.some(s => s.state === 'unavailable' || s.state === 'cursor_expired')
    && state.local.state !== 'unavailable' && state.local.state !== 'expired';
  const invalidated = state.view.kind === 'target' && state.view.reason === 'invalidated';
  if (invalidated) retainedJobs.current = [];
  const jobs = liveJobs.length > 0 ? liveJobs
    : (!discoveryIdle && !invalidated && retainedJobs.current.length ? retainedJobs.current : liveJobs);
  const rowButtons = useRef(new Map<string, HTMLButtonElement>());
  const previousGroups = useRef(new Map<string, string>());
  const focusedRow = useRef<string | null>(null);
  const stickyQualityRef = useRef<Record<string, string>>(state.stickyQuality);
  const [visible, setVisible] = useState(() => !document.hidden);
  useEffect(() => {
    const update = () => setVisible(!document.hidden);
    document.addEventListener('visibilitychange', update);
    return () => document.removeEventListener('visibilitychange', update);
  }, []);
  useLayoutEffect(() => {
    const focused = jobs.find(job => job.metadata_key === focusedRow.current);
    if (enabled && visible && focused && previousGroups.current.has(focused.metadata_key ?? '')
      && previousGroups.current.get(focused.metadata_key ?? '') !== lifecycleGroup(focused)
      && isFinished(focused)) {
      setFinishedOpen(true);
    }
    previousGroups.current = new Map(jobs.map(job => [job.metadata_key ?? '', lifecycleGroup(job)]));
  }, [jobs, enabled, visible]);
  const [pending, setPending] = useState(peekPendingSwarmNavigation);
  const handled = useRef<SwarmNavigationTarget | null>(null);
  const [focusRequest, setFocusRequest] = useState<{ key: string; target: SwarmNavigationTarget } | null>(null);
  const context = state.view.kind === 'idle' ? null : { ...state.view.target, contextEpoch: state.contextEpoch };
  useEffect(() => { try { localStorage.setItem(preferenceKey, JSON.stringify(preferences)); } catch { /* Optional UI preference. */ } }, [preferences, preferenceKey]);
  useEffect(() => {
    const sync = () => setJobScope(loadJobScope());
    window.addEventListener(JOB_SCOPE_CHANGED_EVENT, sync);
    return () => window.removeEventListener(JOB_SCOPE_CHANGED_EVENT, sync);
  }, []);
  useEffect(() => {
    const live = new Set(jobs.filter(isLiveObservation).map(j => j.metadata_key));
    setPreferences(p => p.dismissed.some(key => live.has(key)) ? { ...p, dismissed: p.dismissed.filter(key => !live.has(key)) } : p);
  }, [jobs]);
  useEffect(() => {
    if (!enabled || !visible || state.view.kind !== 'view' || state.working) return;
    void store.hydrateListedDetails();
  }, [enabled, visible, jobs, state.working, state.view.kind, state.detailCache, state.advanceNumber, store]);
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
    setFinishedOpen(true);
    setPreferences(p => ({ expanded: [...p.expanded.filter(k => k !== key), key].slice(-8), dismissed: p.dismissed.filter(k => k !== key) }));
    setNotice(pending.artifactId ? 'Job opened. Artifact details are in the Puppetmaster dashboard.' : '');
    setFocusRequest({ key, target: pending });
  }, [jobs, pending, enabled, visible, state.contextEpoch, state.view]);
  useLayoutEffect(() => {
    if (!enabled || !visible || !focusRequest || peekPendingSwarmNavigation() !== focusRequest.target
      || !jobs.some(job => job.metadata_key === focusRequest.key && navigationMatches(focusRequest.target, context, job))) return;
    const button = rowButtons.current.get(focusRequest.key);
    if (!button) return;
    button.focus({ preventScroll: true });
    button.scrollIntoView({ block: 'nearest' });
    takePendingSwarmNavigation(focusRequest.target);
    setPending(p => p === focusRequest.target ? null : p);
    setFocusRequest(null);
  }, [focusRequest, enabled, visible, jobs, state.contextEpoch, state.view]);
  if (!enabled) return <p className="p-2 text-xs text-muted">Job metadata paused for this view.</p>;
  const activeSessionId = metadataViewSessionId(state);
  const scoped = filterJobsByScope(jobs, jobScope, activeSessionId, {
    includeJobIds: pending?.jobId ? [pending.jobId] : undefined,
  });
  const hidden = scoped.filter(j => !isActive(j) && !isNativeActivity(j) && preferences.dismissed.includes(j.metadata_key ?? ''));
  const createdAt = new Map(state.local.observations.map(o => [localKey(o.row.local_ref), o.row.created_at]));
  for (const job of jobs) {
    const listedLocal = job.local_ref
      ? state.local.observations.find(o => localKey(o.row.local_ref) === localKey(job.local_ref!))?.row : undefined;
    const key = expertLookupKey(job.metadata_key, listedLocal?.canonical, state.view.kind === 'view' ? state.view.context.repo : undefined);
    const date = currentHeader(state, key)?.created_at;
    if (date) createdAt.set(job.metadata_key ?? '', Date.parse(date));
  }
  const quality = (job: Job) => {
    const listedLocal = job.local_ref
      ? state.local.observations.find(o => localKey(o.row.local_ref) === localKey(job.local_ref!))?.row
        ?? (state.localDetail?.observation?.summary && localKey(state.localDetail.selection) === localKey(job.local_ref)
          ? state.localDetail.observation.summary : undefined)
      : undefined;
    const key = expertLookupKey(job.metadata_key, listedLocal?.canonical, state.view.kind === 'view' ? state.view.context.repo : undefined);
    const expert = currentExpert(state, key);
    const live = expert ? expertJobQuality(expert) : currentHeader(state, key)?.quality ?? 'unverified';
    const resolved = stickyJobQuality(key, live, stickyQualityRef.current);
    stickyQualityRef.current = resolved.next;
    return resolved.quality;
  };
  const selectedSummary = state.localDetail?.observation?.summary;
  if (selectedSummary && !createdAt.has(localKey(selectedSummary.local_ref))) createdAt.set(localKey(selectedSummary.local_ref), selectedSummary.created_at);
  const shown = scoped.filter(j => !hidden.includes(j)).filter(j => {
    switch (filter) {
      case 'all': return true;
      case 'finished': return isFinished(j);
      case 'attention': return j.status === 'interrupted' || (j.local_ref ? nativeAttentionStatuses : ['failed', 'stalled']).includes(j.status);
      case 'active': return isLiveObservation(j);
      case 'failed': return ['failed', 'timeout', 'timed_out', 'truncated', 'interrupted'].includes(j.status);
      case 'cancelled': return j.status === 'cancelled';
      case 'complete': return ['completed', 'complete', 'done'].includes(j.status);
      // Lifecycle alone cannot establish a failed check or a trustworthy result.
      case 'untrustworthy': return quality(j) === 'degraded';
      default: return false;
    }
  }).sort((a, b) => {
    const left = createdAt.get(a.metadata_key ?? '') ?? null, right = createdAt.get(b.metadata_key ?? '') ?? null;
    if (left !== null && right !== null && left !== right) return sort === 'newest' ? right - left : left - right;
    if (left === null && right !== null) return 1;
    if (left !== null && right === null) return -1;
    return (a.metadata_key ?? '').localeCompare(b.metadata_key ?? '');
  });
  const trackerCount = [...shown, ...hidden].filter(job => !isNativeActivity(job)).length;
  const activeRows = shown.filter(j => isActive(j) && !isNativeActivity(j));
  const finishedRows = shown.filter(j => !isActive(j) && !isNativeActivity(j));
  const failedCount = finishedRows.filter(j => failedOutcomeStatuses.has(j.status)).length;
  const warningCount = finishedRows.filter(j => quality(j) === 'degraded').length;
  const cancelledCount = finishedRows.filter(j => j.status === 'cancelled').length;
  const failedRead = state.error || state.view.kind === 'view' && (state.view.view.availability === 'unavailable'
    || state.streams.some(s => s.state === 'unavailable' || s.state === 'cursor_expired')
    || state.local.state === 'unavailable' || state.local.state === 'expired');
  const anyRunning = shown.some(isLiveObservation);
  const runningCount = shown.filter(isLiveObservation).length;
  const completedCount = shown.filter(j => isFinished(j) && !isNativeActivity(j)).length;
  const hideFinished = () => {
    if (!finishedOpen) return;
    setPreferences(p => ({ ...p, dismissed: [...new Set([...p.dismissed, ...finishedRows.filter(j => j.read_status !== 'unavailable').flatMap(j => j.metadata_key ? [j.metadata_key] : [])])].slice(-200) }));
  };
  const popOutBoard = (job: Job) => {
    const deepLink = dashboardJobId(job);
    const repo = lastSelectedProjectRoot() || undefined;
    void api.dashboard(deepLink.startsWith('job_') ? deepLink : undefined, repo)
      .then((payload) => {
        const url = payload.url || payload.embed_url || '';
        if (!payload.ok || !url) {
          setNotice(dashboardLocateError(payload));
          return;
        }
        openAgentUrlExternal(url);
      })
      .catch((err: unknown) => { setNotice(dashboardUnavailableMessage(err)); });
  };
  const renderJob = (job: Job) => {
    const key = job.metadata_key ?? '', title = jobDisplayTitle(job), open = preferences.expanded.includes(key);
    const listedLocal = job.local_ref
      ? state.local.observations.find(o => localKey(o.row.local_ref) === localKey(job.local_ref!))?.row
        ?? (state.localDetail?.observation?.summary && localKey(state.localDetail.selection) === localKey(job.local_ref)
          ? state.localDetail.observation.summary : undefined)
      : undefined;
    const expertKey = expertLookupKey(key, listedLocal?.canonical, state.view.kind === 'view' ? state.view.context.repo : undefined);
    const expert = currentExpert(state, expertKey), model = (expert ? expertJobModel(expert) : null) ?? expertHeaderModel(currentHeader(state, expertKey));
    const routing = expert && expert.kind !== 'unavailable' && expert.coverage.tasks === 'complete' && expert.tasks.length === 0 && isLiveObservation(job) && !model;
    const header = currentHeader(state, expertKey);
    const workerCount = header?.selected_workers;
    const finishedWorkers = header?.completed_workers;
    const showWorkerProgress = workerCount !== undefined && workerCount > 0 && finishedWorkers !== undefined;
    const workerProgressFull = showWorkerProgress && finishedWorkers >= workerCount;
    const runningIcon = !job.local_ref && job.read_status !== 'unavailable' && job.status === 'running';
    const adapter = job.adapter || '';
    const rowHidden = !isActive(job) && !isNativeActivity(job) && !finishedOpen && !open;
    // Tailwind's preflight [hidden] rule loses to the later .flex utility, so
    // the display class must flip with the attribute or the row stays visible.
    return <div className={`relative shrink-0 ${rowHidden ? 'hidden' : 'flex'} flex-col border-b border-edge/25 last:border-b-0`} key={key} hidden={rowHidden} data-job-id={job.id} data-job-source={job.source} data-testid={`inspect-${job.source}-${job.id}`} data-quality={quality(job)}
      onFocus={() => { focusedRow.current = key; }} onBlur={event => { if (!event.currentTarget.contains(event.relatedTarget)) focusedRow.current = null; }}>
      <button ref={element => { if (element) rowButtons.current.set(key, element); else rowButtons.current.delete(key); }} className={`w-full flex items-center gap-2 py-1 px-1.5 hover:bg-panel2/25 text-left select-none cursor-pointer min-h-[1.625rem] focus-visible:outline focus-visible:outline-accent ${isFinished(job) && job.read_status !== 'unavailable' ? 'pr-8' : 'pr-5'}`} aria-label={`${title} · ${metadataOutcomeLabel(job.status)}`} aria-expanded={open} onClick={() => setPreferences(p => ({ ...p, expanded: open ? p.expanded.filter(k => k !== key) : [...p.expanded, key].slice(-8) }))}>
        <span className="shrink-0 text-faint">{open ? <ChevronDown size={12} /> : <ChevronRight size={12} />}</span>
        <span className="shrink-0">{runningIcon ? <MetadataActivityIndicator /> : isLiveObservation(job) ? <Loader2 size={12} className="animate-spin semantic-activity-spinner text-accent" /> : failedOutcomeStatuses.has(job.status) ? <XCircle size={12} className="text-risk" /> : quality(job) === 'degraded' ? <AlertTriangle size={12} className="text-warn" /> : ['completed', 'complete', 'done'].includes(job.status) ? <CheckCircle2 size={12} className="text-faint" /> : job.status === 'cancelled' ? <XCircle size={12} className="text-muted" /> : <Circle size={12} className="text-muted" />}</span>
        <span className="font-semibold text-[11px] text-txt truncate min-w-0 flex-1">{title}</span>
        {showWorkerProgress && <span className="inline-flex items-center gap-1 shrink-0">
          <span className="h-0.5 w-8 rounded-full bg-edge/50 overflow-hidden" aria-hidden>
            <span className={`block h-full rounded-full ${workerProgressFull ? (quality(job) === 'degraded' ? 'bg-warn/70 w-full' : 'bg-good/70 w-full') : 'bg-accent/70'}`} style={{ width: workerProgressFull ? '100%' : `${Math.max(8, Math.round((finishedWorkers / workerCount) * 100))}%` }} />
          </span>
          <span className={`text-[9px] tabular-nums font-mono ${quality(job) === 'degraded' ? 'text-warn/80' : 'text-muted'}`}>{finishedWorkers}/{workerCount}</span>
        </span>}
        {adapter && <span title={`Adapter: ${adapter}`} className="min-w-0 truncate text-[9px] text-faint">{adapter}</span>}
        {model && <span title={`Model: ${model}`} className="min-w-0 truncate text-faint"> · {model}</span>}
        {routing && <span className="shrink-0"> · routing…</span>}
        <span className={`text-[9px] font-medium tabular-nums shrink-0 ${job.status === 'cancelled' ? 'text-muted' : failedOutcomeStatuses.has(job.status) ? 'text-risk/80' : quality(job) === 'degraded' ? 'text-warn/80' : quality(job) === 'ok' && ['complete', 'completed', 'done'].includes(job.status) ? 'text-good' : 'text-accent/80'}`}>{quality(job) === 'degraded' ? <span className="text-warn">degraded</span> : quality(job) === 'ok' && ['complete', 'completed', 'done'].includes(job.status) ? <span className="text-good">done</span> : <MetadataOutcomeLabel status={job.status} />}</span>
      </button>
      <button type="button" className="absolute right-1 top-1 text-faint/50 hover:text-muted" aria-label="Open Puppetmaster board" title="Open Puppetmaster board" onClick={(event) => { event.stopPropagation(); popOutBoard(job); }}><ExternalLink size={11} /></button>
      {isFinished(job) && job.read_status !== 'unavailable' && <button type="button" className="absolute right-5 top-1 text-faint/50 hover:text-risk" aria-label={`Dismiss from Jobs: ${title}`} title="Dismiss from Jobs (stays in Puppetmaster history)" onClick={() => setPreferences(p => ({ ...p, dismissed: [...p.dismissed.filter(k => k !== key), key].slice(-200) }))}><X size={12} /></button>}
      {job.status === 'stalled' && <p className="px-2 text-xs text-muted">{job.local_ref ? 'May still be active; terminal state unconfirmed.' : 'Finished for liveness; recoverable.'}</p>}
      {open && <MetadataInspection compact job={job} onOpenDashboard={() => popOutBoard(job)} navigation={enabled && visible && pending && handled.current === pending && navigationMatches(pending, context, job) ? pending : undefined} />}
    </div>;
  };
  const jobList: ReactNode[] = [];
  for (const job of [...activeRows, ...finishedRows]) {
    if (job === activeRows[0]) {
      jobList.push(<div key="active-head" className="flex items-center px-1 pt-0.5">
        <span className="text-[10px] uppercase tracking-wider text-faint font-semibold">
          Active <span className="text-faint/60 normal-case tracking-normal">({activeRows.length})</span>
        </span>
      </div>);
    }
    if (job === finishedRows[0]) {
      jobList.push(<div key="finished-head" className="swarm-finished-head flex items-center justify-between px-1 pt-0.5">
        <button type="button" aria-expanded={finishedOpen} onClick={() => setFinishedOpen(open => !open)} className="flex items-center gap-1 min-w-0 text-[10px] uppercase tracking-wider text-faint font-semibold hover:text-muted focus:outline-none">
          {finishedOpen ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
          <span className="whitespace-nowrap">Finished <span className="text-faint/60 normal-case tracking-normal">({finishedRows.length})</span></span>
          <span className="swarm-finished-chips flex flex-wrap items-center min-w-0">
            {(failedCount > 0 || cancelledCount > 0) && <span className="whitespace-nowrap text-risk/70 normal-case tracking-normal">{[failedCount ? `${failedCount} failed` : '', cancelledCount ? `${cancelledCount} cancelled` : ''].filter(Boolean).join(' · ')}</span>}
            {warningCount > 0 && <span className="whitespace-nowrap text-warn/80 normal-case tracking-normal"> · {warningCount} untrustworthy</span>}
          </span>
        </button>
        <button type="button" aria-label="Hide finished" title="Hide all finished runs from Jobs (stays in Puppetmaster history)" onClick={hideFinished} className="shrink-0 whitespace-nowrap text-[9px] text-faint/70 hover:text-risk uppercase tracking-wider focus:outline-none">Clear</button>
      </div>);
    }
    jobList.push(renderJob(job));
  }
  return <section aria-label="Jobs" className="flex flex-col h-full overflow-hidden text-txt">
    <div className="shrink-0 flex items-center justify-between h-[var(--shell-rail-row-height)] px-2 border-b border-[var(--shell-panel-border)] select-none">
      <h2 className="flex items-center gap-1.5 text-[10px] uppercase tracking-normal text-faint font-medium">
        <span className="relative inline-flex">
          <Network size={11} className={anyRunning ? "text-accent" : "text-faint/70"} />
          {anyRunning ? <span className="absolute -top-0.5 -right-0.5 h-1.5 w-1.5 rounded-full bg-accent animate-pulse" title={`${runningCount} running`} aria-hidden /> : null}
        </span>
        <span>Swarm Tracker</span>
        {trackerCount > 0 && <span className="text-faint/60 normal-case tracking-normal">({trackerCount})</span>}
      </h2>
      <div className="flex items-center gap-2.5 text-[10px]">
        {anyRunning && <span className="flex items-center gap-1 text-accent"><Loader2 size={10} className="animate-spin semantic-activity-spinner" /> {runningCount} running</span>}
        {completedCount > 0 && <span className="flex items-center gap-1 text-good/80"><CheckCircle2 size={10} /> {completedCount}</span>}
      </div>
    </div>
    {state.error && <p role="alert" className="px-2 py-1 text-xs text-risk">Job updates unavailable ({state.error.replaceAll('_', ' ')}). Retained observations may be stale.</p>}
    <div className="sr-only">
      <MetadataStatus hidden />
      <SessionWorkerUsage />
      <p>Sorting and filters apply only to observed jobs; history coverage is incomplete. Known creation times are ordered within each lifecycle group; undated jobs remain last.</p>
    </div>
    {state.headerError && <p className="px-2 text-xs text-muted">Job headers could not be refreshed. Retry header refresh; list observations remain available.</p>}
    <div className="shrink-0 grid grid-cols-2 gap-1 px-2 py-1 border-b border-[var(--shell-panel-border)] bg-panel2/10">
      <select aria-label="Filter jobs" className={compactSelect} value={filter} onChange={e => { const next = e.target.value; setFilter(next); if (next !== 'all' && next !== 'active') setFinishedOpen(true); }}>
        <option value="all">All statuses</option>
        <option value="active">Active</option>
        <option value="attention">Needs attention</option>
        <option value="finished">Finished</option>
        <option value="failed">Failed</option>
        <option value="cancelled">Cancelled</option>
        <option value="complete">Completed lifecycle</option>
        <option value="untrustworthy">Untrustworthy</option>
      </select>
      <select aria-label="Sort jobs" className={compactSelect} value={sort} onChange={e => setSort(e.target.value === 'oldest' ? 'oldest' : 'newest')}>
        <option value="newest">Newest first</option>
        <option value="oldest">Oldest first</option>
      </select>
      <div className="col-span-2 flex h-6 overflow-hidden rounded border border-edge">
        {(["session", "repo", "all"] as const).map((scope) => (
          <button
            key={scope}
            type="button"
            aria-pressed={jobScope === scope}
            aria-label={scope === "session" ? "This session" : scope === "repo" ? "This repo" : "All projects"}
            onClick={() => { setJobScope(scope); saveJobScope(scope); }}
            className={`flex-1 text-[10px] ${jobScope === scope ? "bg-accent/15 text-txt" : "bg-panel2/40 text-muted hover:text-txt"}`}
          >
            {scope === "session" ? "Session" : scope === "repo" ? "Repo" : "All"}
          </button>
        ))}
      </div>
    </div>
    {finishedRows.length === 0 && <button type="button" className="sr-only" aria-label="Hide finished" onClick={hideFinished}>Hide finished</button>}
    {hidden.length > 0 && shown.length > 0 && <button type="button" className={`${button} px-2`} onClick={() => setPreferences(p => ({ ...p, dismissed: [] }))}>Show {hidden.length} hidden</button>}
    {filter !== 'all' && shown.length > 0 && <button type="button" className={`${button} px-2`} onClick={() => setFilter('all')}>Clear filter</button>}
    {filter === 'untrustworthy' && !shown.length && <button type="button" className={`${button} px-2`} onClick={() => setFilter('all')}>Clear filter</button>}
    {filter === 'untrustworthy' && <p role="status" className="px-2 text-xs text-muted">Showing recorded degraded quality. Failed lifecycle is separate from failed verification.</p>}
    {filter === 'complete' && <p className="px-2 text-xs text-muted">Completed lifecycle does not establish successful verification.</p>}
    {notice && <p role="status" className="px-2 text-sm text-muted">{notice}</p>}
    <div className="flex-1 min-h-0 overflow-y-auto px-2 py-1 flex flex-col gap-0.5">
    {!shown.length && filter !== 'untrustworthy' && <div className="flex flex-col items-center justify-center h-48 text-center px-6 gap-2">
      <Network size={20} className="text-faint/50" />
      <span className="text-[12px] text-muted font-medium">{failedRead
        ? jobsListEmptyTruth({ failedRead: true, viewReady: true, working: false, hiddenCount: 0, filter: 'all', hasJobs: false }).title
        : state.view.kind !== 'view' || !jobs.length && state.working
          ? <><span>Loading jobs...</span> Waiting for job metadata; no lifecycle result is known yet.</>
          : hidden.length && filter === 'all' ? 'Observed finished jobs are hidden. Show hidden jobs to restore them.'
            : !jobs.length ? 'No jobs yet'
              : 'No jobs match this filter'}</span>
      {failedRead ? <span className="text-[10.5px] text-faint">Retry updates; an empty view does not establish no work.</span>
        : filter !== 'all' && (jobs.length > 0 || hidden.length > 0) ? <button type="button" className="text-[10.5px] text-accent hover:underline focus:outline-none" onClick={() => setFilter('all')}>Clear filter</button>
        : hidden.length > 0 ? <button type="button" className="text-[10.5px] text-accent hover:underline focus:outline-none" onClick={() => setPreferences(p => ({ ...p, dismissed: [] }))}>Show {hidden.length} hidden</button>
        : !failedRead && state.view.kind === 'view' && !jobs.length && !state.working && (
        <span className="text-[10.5px] text-faint leading-relaxed">
          Every dispatched worker lands here -- run_implement, run_parallel,
          and run_swarm alike -- with its phase, router choice, live workers,
          and streamed findings. Inline tool calls stay in the chat.
        </span>
      )}
    </div>}
    {jobList}
    </div>
  </section>;
}
