import { createContext, useContext, useEffect, useState } from 'react';
import type { ReactNode } from 'react';
import { JobMetadataStore, useJobMetadata } from './useJobMetadata';
import type { JobMetadataState } from './useJobMetadata';
import type { Job } from './api';
import { canonicalExpertSelection, canonicalPMReplacesLocal, metadataSelectionKey, pmActiveStatuses } from './jobMetadata';
import type { LocalObservation } from './localJobMetadata';
import { localKey, nativeActiveStatuses } from './localJobMetadata';
import { isCommandJob, isTrackerHire, isWaveCoordinator } from './jobClassification';

/** Prefer the freshest local observation when the same job_id appears under multiple keys. */
function preferFresherLocal(a: LocalObservation, b: LocalObservation): LocalObservation {
  const winner = a.row.revision !== b.row.revision
    ? (a.row.revision > b.row.revision ? a : b)
    : a.freshness !== b.freshness
      ? (a.freshness === 'observed' ? a : b)
      : (a.observedAt >= b.observedAt ? a : b);
  const other = winner === a ? b : a;
  // localDetail summaries are inserted as stale/observedAt 0; keep list freshness when they win on revision.
  if (winner.freshness === 'stale' && winner.observedAt === 0 && other.freshness === 'observed'
    && other.row.local_ref.job_id === winner.row.local_ref.job_id) {
    return { ...winner, freshness: 'observed', observedAt: other.observedAt };
  }
  return winner;
}

function localGoalFallback(row: LocalObservation['row'], selectedContextRequest?: string): string {
  if (selectedContextRequest?.trim()) return selectedContextRequest.trim();
  const preview = row.display?.goal_preview?.trim();
  if (preview) return preview;
  if (row.display) return `${row.display.label}${row.display.model ? ` · ${row.display.model}` : ''}`;
  return row.kind.replaceAll('_', ' ');
}

const inactive = new JobMetadataStore();
export const JobMetadataContext = createContext(inactive);
export function JobMetadataOwner({ repo, sessionId, children }: { repo: string; sessionId: string | null; children: ReactNode }) {
  const [store] = useState(() => new JobMetadataStore());
  useJobMetadata(store);
  useEffect(() => {
    if (!repo || !sessionId) { store.invalidate(); return; }
    store.setTarget({ repo, session_id: sessionId, scope: 'all' });
    const tick = () => { if (!document.hidden) void store.ownerTick(); };
    store.startTicks(2000); tick();
    document.addEventListener('visibilitychange', tick);
    return () => { store.stopTicks(); document.removeEventListener('visibilitychange', tick); };
  }, [store, repo, sessionId]);
  // Effect cleanup must support React StrictMode's setup-cleanup-setup replay.
  useEffect(() => () => { store.stopTicks(); }, [store]);
  return <JobMetadataContext.Provider value={store}>{children}</JobMetadataContext.Provider>;
}
export function useSharedJobMetadata() {
  const store = useContext(JobMetadataContext);
  return { store, state: useJobMetadata(store) };
}
/** Leaf provider workers and command jobs stay off the Jobs list. */
export function isJobsListRow(job: Pick<Job, "job_kind" | "id" | "role" | "adapter">): boolean {
  return job.job_kind !== "provider" && !isCommandJob(job);
}
/** Live-dot / Jobs pulse: real swarm/implement hires only, never run_command. */
function isObservedTrackerHire(
  freshness: string,
  lifecycle: string | null | undefined,
  signals: { id?: string | null; job_kind?: string | null; role?: string | null; adapter?: string | null },
): boolean {
  const status = String(lifecycle || '').toLowerCase();
  return freshness === 'observed'
    && nativeActiveStatuses.includes(status)
    && isTrackerHire(signals)
    && !isWaveCoordinator(signals);
}

export function metadataActivity(state: JobMetadataState): { count: number; label: string } {
  const count = state.observations.filter(o => isObservedTrackerHire(
    o.freshness,
    o.row.lifecycle,
    { id: o.row.selection.job_ref.job_id },
  )).length
    + state.local.observations.filter(o => isObservedTrackerHire(
      o.freshness,
      o.row.lifecycle,
      {
        id: o.row.local_ref.job_id,
        job_kind: o.row.kind,
        role: o.row.kind,
        adapter: o.row.display?.adapter,
      },
    ) && !canonicalPMReplacesLocal(o, state.observations)).length;
  return { count, label: count ? `At least ${count} active jobs; coverage incomplete` : 'Job activity unknown; coverage incomplete' };
}
/** Current selected facts are valid only for this exact source and revision. */
export function currentExpert(state: JobMetadataState, key: string) {
  const selected = state.detail.kind === 'selected' && metadataSelectionKey(state.detail.selection) === key ? state.detail : state.detailCache[key];
  if (!selected?.observation || selected.error) return undefined;
  const listed = state.observations.find(o => metadataSelectionKey(o.row.selection) === key);
  if (listed?.freshness === 'stale') return undefined;
  // An unrelated lane failure stamps every cached detail stale; a hydrated expert
  // whose own list row is still fresh stays visible.
  if (selected.freshness !== 'observed' && !selected.observation.expert) return undefined;
  // A live job's list row runs ahead of the last detail read between refreshes. The roster
  // stays visible from that read and the refresh cadence catches it up; the card takes its
  // lifecycle from the fresher list row, so a settled job never renders as running.
  return selected.observation.expert;
}
export function currentHeader(state: JobMetadataState, key: string) {
  const expert = currentExpert(state, key);
  const cached = state.headers[key];
  const stale = state.observations.some(o => metadataSelectionKey(o.row.selection) === key && o.freshness === 'stale');
  const header = !stale && cached?.observation.freshness === 'observed'
    ? cached.observation.row.header : undefined;
  const selected = expert?.kind !== 'unavailable' ? expert?.live_economics ?? expert?.header : undefined;
  if (selected) return selected.model_provenance === undefined && header?.model_provenance !== undefined
    ? { ...selected, model: header.model, model_provenance: header.model_provenance } : selected;
  return header;
}
/** Session id for ownership filters while the view is still opening. */
export function metadataViewSessionId(state: JobMetadataState): string {
  if (state.view.kind === 'view') return String(state.view.context.session_id || '').trim();
  if (state.view.kind === 'target') return String(state.view.target.session_id || '').trim();
  return '';
}
/** Presentation only: missing bodies/economics remain explicitly unavailable. No identity inference. */
export function metadataJobs(state: JobMetadataState): Job[] {
  if (state.view.kind === 'idle') return [];
  if (state.view.kind !== 'view' && !state.observations.length && !state.local.observations.length) return [];
  const sources = state.view.kind === 'view' ? state.view.view.sources : [];
  const observed = new Map(state.observations.map(o => [metadataSelectionKey(o.row.selection), o]));
  for (const pin of state.pins) {
    if (!pin.observation) continue;
    const key = metadataSelectionKey(pin.selection), current = observed.get(key);
    if (!current || pin.observation.row.revision > current.row.revision
      || (pin.observation.row.revision === current.row.revision && pin.observation.freshness === 'observed')) observed.set(key, pin.observation);
  }
  const pm: Job[] = [...observed.values()].filter(({ row }) => row.selection.source !== 'cli'
    || (row.stamp === 'known' && !!row.ownership.session_id?.trim())).map(({ row, freshness }) => ({
    id: row.selection.job_ref.job_id, job_ref: row.selection.job_ref, source: row.selection.source,
    metadata_key: metadataSelectionKey(row.selection), metadata_only: true,
    goal: row.display.kind === 'available' && row.display.goal_preview ? row.display.goal_preview : `${row.selection.source === 'cli' ? 'PM CLI job' : 'PM harness job'}`,
    status: row.lifecycle ?? 'unknown', session_id: row.ownership.session_id ?? undefined,
    cross_project: sources.find(s => s.state_id === row.selection.job_ref.state_id)?.cross_project,
    ...(freshness === 'stale' ? { read_status: 'unavailable' } : {}),
    unavailable_fields: ['artifacts', 'tasks'], artifacts_complete: false,
    ...(row.task_count === null ? {} : { task_count: row.task_count }),
  }));
  // Dedupe by job_id: local.observations and localDetail.summary can both contribute
  // the same alias under different localKeys (incarnation churn) and double the Active list.
  const nativeByJobId = new Map<string, LocalObservation>();
  for (const observation of state.local.observations) {
    const id = observation.row.local_ref.job_id;
    const existing = nativeByJobId.get(id);
    nativeByJobId.set(id, existing ? preferFresherLocal(existing, observation) : observation);
  }
  const selectedSummary = state.localDetail?.observation?.summary;
  if (selectedSummary) {
    const candidate: LocalObservation = { row: selectedSummary, freshness: 'stale', observedAt: 0 };
    const id = selectedSummary.local_ref.job_id;
    const existing = nativeByJobId.get(id);
    nativeByJobId.set(id, existing ? preferFresherLocal(existing, candidate) : candidate);
  }
  const selectedRequest = state.localDetail?.observation?.selected_context?.request?.text;
  const repo = state.view.kind === 'view' ? state.view.context.repo : '';
  const local: Job[] = [...nativeByJobId.values()]
    .filter(localObs => !canonicalPMReplacesLocal(localObs, [...observed.values()]))
    .map(({ row, freshness }) => {
      const canonical = row.canonical;
      const canonKey = canonical && repo ? metadataSelectionKey(canonicalExpertSelection(canonical, repo)) : '';
      const detailObservation = canonKey
        ? (state.detail.kind === 'selected' && metadataSelectionKey(state.detail.selection) === canonKey
          ? state.detail.observation : state.detailCache[canonKey]?.observation) ?? undefined
        : undefined;
      let goalFromPm: string | undefined;
      if (detailObservation?.display?.kind === 'available' && detailObservation.display.goal_preview) {
        goalFromPm = detailObservation.display.goal_preview;
      } else if (canonKey) {
        const listed = [...observed.values()].find(o => metadataSelectionKey(o.row.selection) === canonKey);
        if (listed?.row.display.kind === 'available' && listed.row.display.goal_preview) {
          goalFromPm = listed.row.display.goal_preview;
        }
      }
      const sameDetail = selectedSummary && localKey(selectedSummary.local_ref) === localKey(row.local_ref);
      // The canonical detail is re-read on a 4s cadence while the alias is live; once it
      // reports a terminal lifecycle the row is finished even if the list lanes lag.
      const detailLifecycle = detailObservation?.lifecycle ?? null;
      const settledByDetail = detailLifecycle !== null && !pmActiveStatuses.some(status => status === detailLifecycle);
      return {
        id: row.local_ref.job_id, local_ref: row.local_ref, source: 'local' as const, metadata_only: true as const,
        metadata_key: localKey(row.local_ref),
        goal: goalFromPm || localGoalFallback(row, sameDetail ? selectedRequest : undefined),
        status: settledByDetail && nativeActiveStatuses.some(status => status === row.lifecycle) ? detailLifecycle : row.lifecycle,
        session_id: row.session_id, job_kind: row.kind, role: row.kind,
        adapter: row.display?.adapter,
        ...(row.parent_ref ? { parent_ref: row.parent_ref } : {}),
        ...(freshness === 'stale' ? { read_status: 'unavailable' as const } : {}),
        updated_at: row.updated_at, unavailable_fields: ['artifacts', 'tasks'], artifacts_complete: false,
        ...(row.task_count === null ? {} : { task_count: row.task_count }),
      };
    });
  return [...pm, ...local];
}
