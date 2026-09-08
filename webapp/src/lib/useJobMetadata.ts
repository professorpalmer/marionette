import { historyCursors } from './selectedMetadataEvidence';
import type { HistoryLaneName } from './selectedMetadataEvidence';
import { localKey, nativeActiveStatuses } from './localJobMetadata';
import type { LocalRef, LocalDetail, LocalObservation, LocalLane } from './localJobMetadata';
import { useSyncExternalStore } from 'react';
import {
  JobMetadataClient, MetadataError, advanceMetadataStream, initialMetadataStream, mergeMetadataRows,
  metadataSelectionKey, metadataStreamKey, metadataStreams, sameMetadataContext, validateMetadataTarget, parseMetadataSelection,
} from './jobMetadata';
import type {
  DetailCursors, MetadataContext, MetadataDetail, MetadataErrorCode, MetadataObservation, MetadataPinResult,
  MetadataRemoval, MetadataSelection, MetadataStream, MetadataStreamState, MetadataTarget, MetadataView, Traversal,
} from './jobMetadata';

type ViewState =
  | { kind: 'idle' }
  | { kind: 'target'; target: MetadataTarget; reason: 'not_opened' | 'invalidated' | MetadataErrorCode }
  | { kind: 'view'; target: MetadataTarget; context: MetadataContext; view: MetadataView; refresh: 'idle' | 'pending' | 'ambiguous' };
type DetailState =
  | { kind: 'none' }
  | ({ kind: 'selected'; selection: MetadataSelection; cursors: DetailCursors; error: MetadataErrorCode | null } & (
    | { observation: null; freshness: 'stale' }
    | { observation: MetadataDetail; freshness: 'stale' | 'observed' }
  ));
type RetainedMetadataObservation = MetadataObservation & { activeRemovalRevision?: number };
export type JobMetadataState = {
  startupStopped: boolean;
  activeSnapshotKeys: Record<string, string[]>;
  localActive: { initialized: boolean; snapshotRevisions: Record<string, number>; traversal: Traversal; state: 'ready' | 'partial' | 'complete' | 'expired' | 'unavailable'; keys: string[]; missing: string[]; observedAt: number | null };
  nextPrimaryActive: number;
  local: { observations: LocalObservation[]; traversal: Traversal; state: 'ready' | 'partial' | 'complete' | 'expired' | 'unavailable'; missing: string[]; observedAt: number | null };
  localDetail: { tasks?: Extract<LocalDetail, { lane: 'tasks' }>; routing?: Extract<LocalDetail, { lane: 'routing' }>; summaryFreshness: 'observed' | 'stale'; laneFreshness: 'observed' | 'stale'; selection: LocalRef; lane: LocalDetail['lane']; observation: LocalDetail | null; error: MetadataErrorCode | null } | null;
  followedLocal: LocalRef[]; followedCursors: Record<string, string | null>; nextFollowed: number; actionPage: { selection: LocalRef; observation: LocalDetail } | null;
  advanceNumber: number; observedAt: Record<string, number>;
  contextEpoch: number; epoch: number; view: ViewState; working: boolean; error: MetadataErrorCode | null;
  observations: RetainedMetadataObservation[]; removals: MetadataRemoval[]; streams: (MetadataStreamState & { initialized?: boolean; retryAt?: number; expiryFailures?: number })[]; nextStream: number; nextForeground: number;
  pins: { selection: MetadataSelection; observation: MetadataObservation | null; result: MetadataPinResult['result'] | null }[];
  detail: DetailState; detailCache: Record<string, Extract<DetailState, { kind: 'selected' }>>; displayLimited: boolean;
};
export type MetadataActionResult = 'applied' | 'skipped' | 'discarded' | 'failed';
function freeze<T>(value: T): T {
  if (value !== null && typeof value === 'object' && !Object.isFrozen(value)) {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}
function blank(epoch: number, view: ViewState): JobMetadataState {
  return { startupStopped: false, activeSnapshotKeys: {}, localActive: { initialized: false, snapshotRevisions: {}, traversal: { mode: 'snapshot', after_revision: 0, cursor: null }, state: 'ready', keys: [], missing: [], observedAt: null }, nextPrimaryActive: 0, contextEpoch: 0, followedLocal: [], followedCursors: {}, nextFollowed: 0, actionPage: null, local: { observations: [], traversal: { mode: 'snapshot', after_revision: 0, cursor: null }, state: 'ready', missing: [], observedAt: null }, localDetail: null, advanceNumber: 0, observedAt: {}, epoch, view, working: false, error: null, observations: [], removals: [], streams: [], nextStream: 0, nextForeground: 0, pins: [], detail: { kind: 'none' }, detailCache: {}, displayLimited: false };
}
function code(error: unknown): MetadataErrorCode { return error instanceof MetadataError ? error.code : 'outcome_unknown'; }
const contextEvents = ['harness-project-switching', 'harness-project-selected', 'harness-session-changed', 'harness-config-changed'];

/** One owner per workspace UI. Subscribers are passive; only the owner starts work. */
export class JobMetadataStore {
  private state = freeze(blank(0, { kind: 'idle' }));
  private readonly listeners = new Set<() => void>();
  private client: JobMetadataClient;
  private inFlight = false;
  // Host generations survive effect replay. A new authoritative host generation
  // retires the old one; retain only the last admitted capture, never a history.
  private sourceCapture: { key: string; error: MetadataErrorCode | null } | null = null;
  private reconciliationTurn = 0;
  private captureKey(view: MetadataView): string {
    return JSON.stringify([view.context.repo, view.context.session_id, view.context.view_generation, view.local?.incarnation]);
  }
  private captureError(): MetadataErrorCode | null {
    return this.state.view.kind === 'view' && this.sourceCapture?.key === this.captureKey(this.state.view.view) ? this.sourceCapture.error : null;
  }
  private timer: ReturnType<typeof setInterval> | null = null;
  private disposed = false;
  private scheduleGeneration = 0;
  constructor(client = new JobMetadataClient()) { this.client = client; }
  getSnapshot = (): JobMetadataState => this.state;
  private publish(state: JobMetadataState): void {
    const selected = state.detail;
    if (selected.kind === 'selected' && (selected.observation || selected.error)) {
      const key = metadataSelectionKey(selected.selection);
      const retained = Object.entries(state.detailCache).filter(([cached]) => cached !== key).slice(-7);
      state = { ...state, detailCache: Object.fromEntries([...retained, [key, selected]]) };
    }
    this.state = freeze(state);
    for (const listener of this.listeners) listener();
  }
  subscribe = (listener: () => void): (() => void) => {
    if (this.disposed) return () => {};
    this.listeners.add(listener);
    if (this.listeners.size === 1) for (const event of contextEvents) window.addEventListener(event, this.invalidate);
    return () => {
      this.listeners.delete(listener);
      if (!this.listeners.size) {
        for (const event of contextEvents) window.removeEventListener(event, this.invalidate);
        this.stopTicks();
        this.invalidate();
      }
    };
  };
  setTarget(target: MetadataTarget): void {
    if (this.disposed) return;
    // Every call is a new incarnation, even when the visible IDs are equal.
    this.stopTicks();
    this.publish({ ...blank(this.state.epoch + 1, { kind: 'target', target: validateMetadataTarget(target), reason: 'not_opened' }), contextEpoch: this.state.contextEpoch + 1, working: this.inFlight });
  }
  invalidate = (): void => {
    if (this.disposed) return;
    const old = this.state.view;
    this.publish({ ...blank(this.state.epoch + 1, old.kind === 'idle' ? old : { kind: 'target', target: old.target, reason: 'invalidated' }), contextEpoch: this.state.contextEpoch + 1, working: this.inFlight });
  };
  closeConnection(): void { this.client.close(); this.client = new JobMetadataClient(); }
  dispose(): void {
    this.stopTicks();
    this.invalidate();
    this.disposed = true;
    this.client.close();
    for (const event of contextEvents) window.removeEventListener(event, this.invalidate);
    this.listeners.clear();
  }
  private current(epoch: number): boolean { return !this.disposed && this.state.epoch === epoch; }
  private async run(work: (epoch: number) => Promise<void>, onError?: (error: MetadataErrorCode) => void, listScope?: { kind: 'pm'; stream: MetadataStream } | { kind: 'local'; lane: LocalLane }): Promise<MetadataActionResult> {
    if (this.disposed || this.inFlight) return 'skipped';
    const epoch = this.state.epoch;
    this.inFlight = true;
    this.publish({ ...this.state, working: true, error: this.captureError() });
    try {
      await work(epoch);
      return this.current(epoch) ? 'applied' : 'discarded';
    } catch (error) {
      if (!this.current(epoch)) return 'discarded';
      const errorCode = code(error);
      if (errorCode === 'busy') return 'skipped';
      const global = !listScope || errorCode === 'view_changed' || errorCode === 'endpoint_changed';
      const pmAffected = (o: MetadataObservation) => global || (listScope?.kind === 'pm' && this.pmAffected(o, listScope.stream));
      const localAffected = (o: LocalObservation) => global || (listScope?.kind === 'local' && (listScope.lane === 'active'
        ? nativeActiveStatuses.includes(o.row.lifecycle) : !this.state.localActive.keys.includes(localKey(o.row.local_ref))));
      this.publish({ ...this.state, error: errorCode, local: { ...this.state.local, observations: this.state.local.observations.map(o => localAffected(o) ? { ...o, freshness: 'stale' } : o) }, observations: this.state.observations.map(o => pmAffected(o) ? { ...o, freshness: 'stale' } : o),
        detailCache: Object.fromEntries(Object.entries(this.state.detailCache).map(([key, cached]) => [key, { ...cached, freshness: 'stale', error: errorCode }])),
        pins: this.state.pins.map(p => p.observation && pmAffected(p.observation) ? { ...p, observation: { ...p.observation, freshness: 'stale' } } : p),
        detail: this.state.detail.kind === 'none' ? this.state.detail : { ...this.state.detail, freshness: 'stale', error: errorCode } });
      if (errorCode === 'view_changed' || errorCode === 'endpoint_changed') {
        const view = this.state.view;
        if (errorCode === 'endpoint_changed') this.client = new JobMetadataClient();
        this.publish({ ...this.state, epoch: this.state.epoch + 1, contextEpoch: this.state.contextEpoch + 1,
          view: view.kind === 'idle' ? view : { kind: 'target', target: view.target, reason: errorCode } });
      } else onError?.(errorCode);
      return 'failed';
    } finally {
      this.inFlight = false;
      if (!this.disposed) this.publish({ ...this.state, working: false });
    }
  }
  private pmAffected(observation: MetadataObservation, stream: MetadataStream): boolean {
    const row = observation.row;
    if (row.selection.source !== stream.store.source || row.selection.job_ref.state_id !== stream.store.state_id) return false;
    if (stream.status !== null) return row.lifecycle === stream.status;
    const key = metadataSelectionKey(row.selection);
    return !Object.values(this.state.activeSnapshotKeys).some(keys => keys.includes(key));
  }
  private adopt(view: MetadataView, target: MetadataTarget): void {
    const context: MetadataContext = { ...view.context, scope: target.scope };
    const previous = this.state.view;
    const same = previous.kind === 'view' && sameMetadataContext(previous.context, context)
      && previous.view.local?.incarnation === view.local?.incarnation;
    const base = same ? this.state : { ...blank(this.state.epoch, { kind: 'idle' }), contextEpoch: this.state.contextEpoch + (previous.kind === 'view' ? 1 : 0) };
    this.publish({ ...base, error: this.sourceCapture?.key === this.captureKey(view) ? this.sourceCapture.error : null, working: true, view: { kind: 'view', target, context, view, refresh: 'idle' },
      streams: same ? base.streams : metadataStreams(view, target.scope).map(initialMetadataStream) });
  }
  /** GET only. Explicit reconciliation after ambiguous refresh; never discovers sources. */
  readView(visibleOnly = false): Promise<MetadataActionResult> {
    const old = this.state.view;
    if (old.kind === 'idle') return Promise.resolve('skipped');
    const target = { ...old.target };
    return this.run(async epoch => {
      await this.client.connect();
      if (!this.current(epoch) || (visibleOnly && document.hidden)) return;
      const view = await this.client.view(target);
      if (this.current(epoch)) this.adopt(view, target);
    });
  }
  refreshView(): Promise<MetadataActionResult> {
    const old = this.state.view;
    if (old.kind !== 'view' || old.refresh !== 'idle' || old.view.refreshing) return Promise.resolve('skipped');
    const captured = old.context;
    const key = this.captureKey(old.view);
    return this.run(async epoch => {
      const prior = this.sourceCapture;
      this.sourceCapture = { key, error: null };
      this.publish({ ...this.state, view: { ...old, refresh: 'pending' },
        observations: this.state.observations.map(o => ({ ...o, freshness: 'stale' })) });
      try {
        const view = await this.client.refresh(captured);
        if (this.current(epoch)) {
          this.sourceCapture = { key: this.captureKey(view), error: null };
          this.adopt(view, old.target);
        }
      } catch (error) {
        if (code(error) === 'busy') {
          this.sourceCapture = prior;
          if (this.current(epoch)) this.publish({ ...this.state, view: old });
        } else this.sourceCapture = { key, error: code(error) };
        throw error;
      }
    }, error => {
      const current = this.state.view;
      if (current.kind === 'view') this.publish({ ...this.state, view: { ...current,
        refresh: error === 'refresh_in_progress' ? 'idle' : 'ambiguous' } });
    });
  }
  /** One bounded page per turn; native active expiry restarts at its next reserved slot. */
  advance(initial = false): Promise<MetadataActionResult> {
    const view = this.state.view;
    if (view.kind !== 'view' || view.refresh !== 'idle' || view.view.refreshing) return Promise.resolve('skipped');
    if (this.inFlight) return Promise.resolve('skipped');
    const turn = this.state.advanceNumber;
    const nativeEligible = view.view.local?.available && !['expired', 'unavailable'].includes(this.state.local.state);
    const activeAvailable = view.view.local?.available && view.view.local.lanes?.includes('active');
    // Eight fixed slots reserve discovery independently of history and pending work.
    const slot = turn % 8;
    if (activeAvailable && (initial ? !this.state.localActive.initialized : slot === 0 || slot === 4)) return this.advanceLocal('active');
    if (!initial && turn % 16 === 6 && this.state.pins.length) return this.refreshPins(true);
    if (!initial && slot === 7 && this.state.followedLocal.length) return this.advanceFollowedLocal();
    if (!initial && slot === 2 && nativeEligible) return this.advanceLocal('history');
    const count = this.state.streams.length;
    const primary = new Set(view.view.sources.filter(source => !source.cross_project).map(source => source.state_id));
    const eligibleIndex = (lane: 'active' | 'history' | 'sibling'): number | null => {
      const start = lane === 'active' ? this.state.nextPrimaryActive : lane === 'history' ? this.state.nextForeground : this.state.nextStream;
      for (let offset = 0; offset < count; offset++) {
        const index = (start + offset) % count, candidate = this.state.streams[index];
        const isPrimary = primary.has(candidate.stream.store.state_id);
        if ((lane === 'sibling' ? !isPrimary : isPrimary && (lane === 'active' ? candidate.stream.status !== null : candidate.stream.status === null))
          && candidate.state !== 'unavailable' && (candidate.retryAt === undefined || candidate.retryAt <= Date.now())) return index;
      }
      return null;
    };
    const preferred = slot === 1 || slot === 5 ? 'active' : slot === 3 || slot === 7 ? 'sibling' : 'history';
    const initialIndex = this.state.streams.findIndex(s => primary.has(s.stream.store.state_id) && s.stream.status !== null && !s.initialized);
    const index = initial ? (initialIndex < 0 ? null : initialIndex)
      : eligibleIndex(preferred) ?? eligibleIndex('active') ?? eligibleIndex('history') ?? eligibleIndex('sibling');
    if (index === null && initial) return Promise.resolve('skipped');
    if (index === null) return activeAvailable ? this.advanceLocal('active') : nativeEligible ? this.advanceLocal('history') : Promise.resolve('skipped');
    const selectedStream = this.state.streams[index];
    const stream = selectedStream.state === 'cursor_expired' ? initialMetadataStream(selectedStream.stream) : selectedStream;
    const foreground = primary.has(stream.stream.store.state_id);
    const nextSchedule = {
      nextPrimaryActive: foreground && stream.stream.status !== null ? (index + 1) % count : this.state.nextPrimaryActive,
      nextForeground: foreground && stream.stream.status === null ? (index + 1) % count : this.state.nextForeground,
      nextStream: foreground ? this.state.nextStream : (index + 1) % count,
    };
    return this.run(async epoch => {
      this.publish({ ...this.state, streams: this.state.streams.map((s, i) => i === index ? { ...s, initialized: true } : s) });
      const response = await this.client.list(view.context, stream.stream, stream.traversal);
      if (!this.current(epoch)) return;
      const advanced = advanceMetadataStream(stream, response);
      const expiryFailures = Math.min((selectedStream.expiryFailures ?? 0) + 1, 6);
      const next = response.page.outcome === 'cursor_expired'
        ? { ...advanced, expiryFailures, retryAt: Date.now() + Math.min(120000, 5000 * 2 ** (expiryFailures - 1)) } : advanced;
      const key = metadataStreamKey(stream.stream);
      const activeKeys = new Set(stream.traversal.mode === 'snapshot' && !stream.traversal.cursor ? [] : this.state.activeSnapshotKeys[key]);
      const previous = new Map(this.state.observations.map(o => [metadataSelectionKey(o.row.selection), o]));
      const acceptedRows = response.rows.filter(row => {
        const old = previous.get(metadataSelectionKey(row.selection));
        return row.deleted || !old || old.activeRemovalRevision === undefined
          || row.revision > old.activeRemovalRevision || row.lifecycle !== old.row.lifecycle;
      });
      if (stream.stream.status !== null) for (const row of acceptedRows) {
        const rowKey = metadataSelectionKey(row.selection);
        if (row.deleted) activeKeys.delete(rowKey); else activeKeys.add(rowKey);
      }
      const merged = mergeMetadataRows(this.state.observations, acceptedRows, { status: stream.stream.status, removals: this.state.removals });
      const reconciled = merged.observations.map((o): RetainedMetadataObservation => {
        const rowKey = metadataSelectionKey(o.row.selection), old = previous.get(rowKey);
        const departed = acceptedRows.find(row => row.deleted && metadataSelectionKey(row.selection) === rowKey);
        const absent = stream.stream.status === o.row.lifecycle
          && o.row.selection.source === stream.stream.store.source && o.row.selection.job_ref.state_id === stream.stream.store.state_id
          && stream.traversal.mode === 'snapshot' && response.page.outcome === 'complete'
          && o.row.revision <= response.page.revision && !activeKeys.has(rowKey);
        const fence = departed && stream.stream.status === o.row.lifecycle ? departed.revision : absent ? response.page.revision : old?.activeRemovalRevision;
        return fence !== undefined && o.row.revision <= fence && (!old || old.row.lifecycle === o.row.lifecycle)
          ? { ...o, activeRemovalRevision: fence, freshness: 'stale' } : o;
      });
      const unavailable = response.page.outcome === 'unavailable' || response.page.outcome === 'cursor_expired';
      const observations = unavailable ? reconciled.map(o => this.pmAffected(o, stream.stream) ? { ...o, freshness: 'stale' } satisfies MetadataObservation : o) : reconciled;
      const touched = (selection: MetadataSelection) => (unavailable && selection.source === stream.stream.store.source
        && selection.job_ref.state_id === stream.stream.store.state_id)
        || response.rows.some(r => metadataSelectionKey(r.selection) === metadataSelectionKey(selection));
      const pins = this.state.pins.map(p => touched(p.selection) && p.observation
        ? { ...p, observation: { ...p.observation, freshness: 'stale' } satisfies MetadataObservation } : p);
      let detail = this.state.detail;
      if (detail.kind === 'selected') {
        // Summary changes cannot silently upgrade or overwrite an older detail observation.
        if (touched(detail.selection)) detail = { ...detail, freshness: 'stale' };
      }
      const room = 200 - this.state.local.observations.length;
      const boundedObservations = observations.slice(Math.max(0, observations.length - room));
      const boundedRemovals = merged.removals.slice(Math.max(0, merged.removals.length - (room - boundedObservations.length)));
      this.publish({ ...this.state, streams: this.state.streams.map((s, i) => i === index ? { ...next, initialized: true } : s), ...nextSchedule,
        advanceNumber: this.state.advanceNumber + 1, observedAt: { ...this.state.observedAt, [metadataStreamKey(stream.stream)]: Date.now() },
        activeSnapshotKeys: Object.fromEntries(Object.entries({ ...this.state.activeSnapshotKeys, ...(stream.stream.status !== null ? { [key]: [...activeKeys] } : {}) }).map(([k, keys]) => [k, keys.filter(rowKey => boundedObservations.some(o => metadataSelectionKey(o.row.selection) === rowKey))])),
        observations: boundedObservations, removals: boundedRemovals, pins, detail,
        detailCache: Object.fromEntries(Object.entries(this.state.detailCache).map(([key, cached]) => [key, touched(cached.selection) ? { ...cached, freshness: 'stale' } : cached])),
        displayLimited: this.state.displayLimited || merged.truncated || observations.length + merged.removals.length > room });
    }, error => {
      this.publish({ ...this.state, advanceNumber: this.state.advanceNumber + 1, streams: this.state.streams.map((s, i) => i === index ? { ...s, state: 'unavailable', missing: [error] } : s), ...nextSchedule });
    }, { kind: 'pm', stream: stream.stream });
  }
  restartTraversal(streamKey?: string): void {
    if (this.disposed || this.inFlight || this.state.view.kind !== 'view' || this.state.view.refresh !== 'idle') return;
    const affected = this.state.streams.filter(s => !streamKey || metadataStreamKey(s.stream) === streamKey);
    this.publish({ ...this.state,
      ...(streamKey ? {} : {
        localActive: { ...this.state.localActive, state: 'ready', keys: [], traversal: { mode: 'snapshot', after_revision: 0, cursor: null } },
        local: { ...this.state.local, observations: this.state.local.observations.map(o => ({ ...o, freshness: 'stale' })), state: 'ready', traversal: { mode: 'snapshot', after_revision: 0, cursor: null } },
      }),
      streams: this.state.streams.map(s => affected.includes(s) ? { ...initialMetadataStream(s.stream), initialized: s.initialized } : s),
      observations: this.state.observations.map(o => affected.some(s => this.pmAffected(o, s.stream)) ? { ...o, freshness: 'stale' } : o) });
  }
  /** Scheduling is opt-in at the shared owner, never inside useJobMetadata. Overlap skips. */
  startTicks(intervalMs = 5000): void {
    if (this.disposed || !Number.isSafeInteger(intervalMs) || intervalMs < 1000 || intervalMs > 2147483647) throw new MetadataError('invalid_request');
    this.stopTicks();
    this.timer = setInterval(() => { if (!document.hidden) void this.ownerTick(); }, intervalMs);
  }
  private hasInitialWork(): boolean {
    if (this.state.startupStopped) return false;
    const view = this.state.view;
    if (view.kind === 'target') return true;
    if (view.kind !== 'view') return false;
    if (view.view.local?.available && view.view.local.lanes?.includes('active') && !this.state.localActive.initialized) return true;
    if (view.view.refreshing || view.refresh !== 'idle') return false;
    if (view.view.missing.includes('sources_not_refreshed') && this.sourceCapture?.key !== this.captureKey(view.view)) return true;
    const primary = new Set(view.view.sources.filter(s => !s.cross_project).map(s => s.state_id));
    return this.state.streams.some(s => primary.has(s.stream.store.state_id) && s.stream.status !== null && !s.initialized);
  }
  /** Owner-only startup: one page per known active stream, never continuation pages.
   * Two primary sources x six statuses + native active + view/capture <= 15 turns.
   * Admissions are serial; skipped/failed work never queues a continuation.
   */
  async ownerTick(): Promise<void> {
    const generation = this.scheduleGeneration, epoch = this.state.epoch;
    for (let remaining = 15; remaining > 0; remaining--) {
      if (this.disposed || document.hidden || generation !== this.scheduleGeneration || epoch !== this.state.epoch) return;
      const result = await this.tick(this.hasInitialWork());
      if (generation !== this.scheduleGeneration || epoch !== this.state.epoch || this.disposed) return;
      if (result === 'failed' || this.state.streams.some(s => s.initialized && (s.state === 'cursor_expired' || s.state === 'unavailable'))
        || (this.state.localActive.initialized && ['expired', 'unavailable'].includes(this.state.localActive.state))) {
        this.publish({ ...this.state, startupStopped: true });
        return;
      }
      if (result !== 'applied' || !this.hasInitialWork()) return;
    }
  }
  tick(initial = false): Promise<MetadataActionResult> {
    if (document.hidden) return Promise.resolve('skipped');
    const view = this.state.view;
    if (view.kind === 'target') return this.readView(true);
    if (view.kind !== 'view' || this.inFlight) return Promise.resolve('skipped');
    if (view.view.refreshing || view.refresh === 'ambiguous') {
      if (initial && view.view.local?.available && view.view.local.lanes?.includes('active') && !this.state.localActive.initialized) return this.advanceLocal('active');
      // Reconcile on the existing cadence while reserving native observation turns.
      const turn = this.reconciliationTurn++;
      if (view.view.local?.available && turn % 2 === 1)
        return this.advanceLocal(turn % 4 === 1 && view.view.local.lanes?.includes('active') ? 'active' : 'history');
      return this.readView(true);
    }
    if (view.view.missing.includes('sources_not_refreshed') && this.sourceCapture?.key !== this.captureKey(view.view))
      return this.refreshView();
    return this.advance(initial);
  }
  private advanceLocal(lane: LocalLane = 'history'): Promise<MetadataActionResult> {
    const v = this.state.view;
    if (v.kind !== 'view' || !v.view.local?.available) return Promise.resolve('skipped');
    const incarnation = v.view.local.incarnation;
    const before = lane === 'active' ? this.state.localActive : this.state.local;
    const traversalBefore: Traversal = lane === 'active' && before.state === 'expired' ? { mode: 'snapshot', after_revision: 0, cursor: null } : before.traversal;
    return this.run(async epoch => {
      if (lane === 'active') this.publish({ ...this.state, localActive: { ...this.state.localActive, initialized: true } });
      const response = await this.client.localList(v.context, incarnation, traversalBefore, lane);
      if (!this.current(epoch)) return;
      const now = Date.now(), rows = new Map(this.state.local.observations.map(o => [localKey(o.row.local_ref), o]));
      const snapshotRevisions = lane === 'active' && traversalBefore.mode === 'snapshot' && !traversalBefore.cursor
        ? Object.fromEntries(this.state.local.observations.map(o => [localKey(o.row.local_ref), o.row.revision])) : this.state.localActive.snapshotRevisions;
      const activeKeys = new Set(lane === 'active' && traversalBefore.mode === 'snapshot' && !traversalBefore.cursor ? [] : this.state.localActive.keys);
      for (const row of response.rows) {
        const key = localKey(row.local_ref), old = rows.get(key);
        if (old && old.row.revision > row.revision) continue;
        if (lane === 'active') { if (row.deleted) activeKeys.delete(key); else activeKeys.add(key); }
        if (lane === 'active' && row.deleted) {
          if (old && nativeActiveStatuses.includes(old.row.lifecycle)) rows.set(key, { ...old, activeRemovalRevision: row.revision, freshness: 'stale' });
          continue;
        }
        // Active removal and its terminal history row share the native revision.
        if (old?.activeRemovalRevision !== undefined && !row.deleted
          && (row.revision < old.activeRemovalRevision
            || row.revision === old.activeRemovalRevision && nativeActiveStatuses.includes(row.lifecycle))) continue;
        rows.delete(key);
        if (!row.deleted) rows.set(key, { row, freshness: 'observed', observedAt: now });
      }
      const unavailable = response.page.outcome === 'expired' || response.page.outcome === 'unavailable';
      if (lane === 'active' && traversalBefore.mode === 'snapshot' && response.page.outcome === 'complete') {
        for (const [key, observation] of rows) {
          if (!activeKeys.has(key) && snapshotRevisions[key] !== undefined && observation.row.revision <= snapshotRevisions[key] && nativeActiveStatuses.includes(observation.row.lifecycle))
            rows.set(key, { ...observation, activeRemovalRevision: observation.row.revision, freshness: 'stale' });
        }
      }
      const all = [...rows.values()].sort((a, b) => Number(activeKeys.has(localKey(a.row.local_ref))) - Number(activeKeys.has(localKey(b.row.local_ref))));
      const observations = all.slice(-100).map(o => unavailable && (lane === 'active' ? nativeActiveStatuses.includes(o.row.lifecycle) : !activeKeys.has(localKey(o.row.local_ref))) ? { ...o, freshness: 'stale' } satisfies LocalObservation : o);
      const traversal: Traversal = response.page.outcome === 'complete'
        ? { mode: 'changes', after_revision: response.page.checkpoint, cursor: null }
        : response.page.outcome === 'partial' ? { ...traversalBefore, cursor: response.page.next_cursor } : traversalBefore;
      const room = 200 - observations.length;
      const pmObservations = this.state.observations.slice(Math.max(0, this.state.observations.length - room));
      const pmRemovals = this.state.removals.slice(Math.max(0, this.state.removals.length - (room - pmObservations.length)));
      this.publish({ ...this.state, advanceNumber: this.state.advanceNumber + 1, removals: pmRemovals,
        observations: pmObservations,
        displayLimited: this.state.displayLimited || all.length > 100 || this.state.observations.length + observations.length > 200,
        localActive: lane === 'active' ? { initialized: true, snapshotRevisions: Object.fromEntries(Object.entries(snapshotRevisions).filter(([key]) => observations.some(o => localKey(o.row.local_ref) === key))), keys: [...activeKeys].filter(key => observations.some(o => localKey(o.row.local_ref) === key)), state: response.page.outcome, missing: response.missing, traversal, observedAt: now } : this.state.localActive,
        local: lane === 'history' ? { observations, state: response.page.outcome, missing: response.missing, traversal, observedAt: now } : { ...this.state.local, observations } });
    }, error => this.publish({ ...this.state, advanceNumber: this.state.advanceNumber + 1,
      ...(lane === 'active' ? { localActive: { ...this.state.localActive, state: 'unavailable', missing: [error] } } : { local: { ...this.state.local, state: 'unavailable', missing: [error] } }) }), { kind: 'local', lane });
  }
  setPendingSelections(pm: MetadataSelection[], native: LocalRef[]): void {
    const view = this.state.view;
    if (view.kind !== 'view' || pm.length + native.length > 8) return;
    const selections = pm.map(s => this.captureSelection(s));
    const followed = native.filter(s => s.incarnation === view.view.local?.incarnation).map(s => ({ ...s }));
    if (JSON.stringify(selections) === JSON.stringify(this.state.pins.map(p => p.selection))
      && JSON.stringify(followed) === JSON.stringify(this.state.followedLocal)) return;
    const old = new Map(this.state.pins.map(p => [metadataSelectionKey(p.selection), p]));
    this.publish({ ...this.state, epoch: this.state.epoch + 1,
      pins: selections.map(selection => old.get(metadataSelectionKey(selection)) ?? { selection, observation: null, result: null }),
      followedLocal: followed, followedCursors: Object.fromEntries(followed.map(r => [localKey(r), this.state.followedCursors[localKey(r)] ?? null])), nextFollowed: 0, actionPage: null });
  }
  followLocal(selections: LocalRef[]): void {
    const view = this.state.view;
    const captured = selections.slice(0, 8 - this.state.pins.length).filter(s => view.kind === 'view' && s.incarnation === view.view.local?.incarnation).map(s => ({ ...s }));
    if (JSON.stringify(captured) === JSON.stringify(this.state.followedLocal)) return;
    this.publish({ ...this.state, epoch: this.state.epoch + 1, followedLocal: captured, followedCursors: Object.fromEntries(captured.map(r => [localKey(r), this.state.followedCursors[localKey(r)] ?? null])), nextFollowed: 0, actionPage: null });
  }
  private advanceFollowedLocal(): Promise<MetadataActionResult> {
    const view = this.state.view, selected = this.state.followedLocal[this.state.nextFollowed % this.state.followedLocal.length];
    if (view.kind !== 'view' || !selected) return Promise.resolve('skipped');
    return this.run(async epoch => {
      const observation = await this.client.localDetail(view.context, selected, 'actions', this.state.followedCursors[localKey(selected)] ?? null);
      if (this.current(epoch)) this.publish({ ...this.state, advanceNumber: this.state.advanceNumber + 1,
        followedCursors: { ...this.state.followedCursors, [localKey(selected)]: observation.page.outcome === 'partial' ? observation.page.next_cursor : null },
        nextFollowed: (this.state.nextFollowed + 1) % this.state.followedLocal.length, actionPage: { selection: selected, observation } });
    }, () => this.publish({ ...this.state, advanceNumber: this.state.advanceNumber + 1,
      nextFollowed: (this.state.nextFollowed + 1) % this.state.followedLocal.length }));
  }
  selectLocal(selection: LocalRef | null, lane: LocalDetail['lane'] = 'actions'): void {
    const view = this.state.view;
    if (selection && (view.kind !== 'view' || selection.incarnation !== view.view.local?.incarnation)) throw new MetadataError('invalid_request');
    const previous = this.state.localDetail;
    const retained = selection && previous && localKey(selection) === localKey(previous.selection) ? previous : null;
    this.publish({ ...this.state, epoch: this.state.epoch + 1, detail: { kind: 'none' }, localDetail: selection ? {
      tasks: retained?.tasks, routing: retained?.routing, selection: { ...selection }, lane, observation: retained?.observation ?? null, error: null,
      summaryFreshness: retained?.summaryFreshness ?? 'stale', laneFreshness: 'stale',
    } : null });
  }
  readLocalDetail(next = false): Promise<MetadataActionResult> {
    const view = this.state.view, detail = this.state.localDetail;
    if (view.kind !== 'view' || view.refresh !== 'idle' || !detail) return Promise.resolve('skipped');
    const cursor = next ? detail.observation?.page.next_cursor : null;
    if (next && (!cursor || detail.laneFreshness !== 'observed')) return Promise.resolve('skipped');
    return this.run(async epoch => {
      const observation = await this.client.localDetail(view.context, detail.selection, detail.lane, cursor ?? null, true);
      if (!this.current(epoch)) return;
      const listed = this.state.local.observations.find(o => localKey(o.row.local_ref) === localKey(detail.selection));
      const revision = Math.max(detail.observation?.summary?.revision ?? 0, listed?.row.revision ?? 0);
      const summaryFresh = observation.page.outcome !== 'expired' && observation.summary !== undefined
        && observation.summary.revision >= revision;
      const laneFresh = summaryFresh && (observation.page.outcome === 'complete' || observation.page.outcome === 'partial');
      const retained = detail.observation;
      let routing = detail.routing;
      if (laneFresh && observation.lane === 'routing') {
        const previous = next && routing?.page.revision === observation.page.revision ? routing : null;
        const rows = [...(previous?.rows ?? []), ...observation.rows];
        const limited = rows.length > 200 || previous?.missing.includes('frontend_routing_limit');
        routing = { ...observation, rows: rows.slice(-200), missing: [...observation.missing, ...(limited ? ['frontend_routing_limit'] : [])] };
      }
      this.publish({ ...this.state, localDetail: { ...detail, routing, tasks: laneFresh && observation.lane === 'tasks' ? observation : detail.tasks,
        observation: !summaryFresh ? retained : !laneFresh && retained && (retained.page.outcome === 'complete' || retained.page.outcome === 'partial')
          ? { ...retained, summary: observation.summary, selected_context: observation.selected_context }
          : observation,
        summaryFreshness: summaryFresh ? 'observed' : 'stale',
        laneFreshness: laneFresh ? 'observed' : 'stale',
        error: laneFresh ? null : 'unavailable' } });
    }, error => this.publish({ ...this.state, localDetail: { ...detail, error, summaryFreshness: 'stale', laneFreshness: 'stale' } }));
  }

  stopTicks(): void { this.scheduleGeneration++; if (this.timer !== null) clearInterval(this.timer); this.timer = null; }
  private captureSelection(s: MetadataSelection): MetadataSelection {
    const v = this.state.view;
    if (v.kind !== 'view') throw new MetadataError('invalid_request');
    const selected = parseMetadataSelection(s, v.context);
    if (!v.view.sources.some(source => source.source === selected.source && source.state_id === selected.job_ref.state_id
      && (!source.cross_project || v.context.scope !== 'repo'))) throw new MetadataError('invalid_request');
    return selected;
  }
  setPins(selections: MetadataSelection[]): void {
    if (this.disposed) return;
    if (selections.length + this.state.followedLocal.length > 8) throw new MetadataError('invalid_request');
    const captured = selections.map(s => this.captureSelection(s));
    if (new Set(captured.map(metadataSelectionKey)).size !== captured.length) throw new MetadataError('invalid_request');
    const old = new Map(this.state.pins.map(p => [metadataSelectionKey(p.selection), p]));
    this.publish({ ...this.state, pins: captured.map(s => old.get(metadataSelectionKey(s)) ?? { selection: s, observation: null, result: null }) });
    // Captured pin set is compared at response time, including set ABA via epoch.
    this.publish({ ...this.state, epoch: this.state.epoch + 1 });
  }
  refreshPins(scheduled = false): Promise<MetadataActionResult> {
    const view = this.state.view;
    if (view.kind !== 'view' || view.refresh !== 'idle' || !this.state.pins.length) return Promise.resolve('skipped');
    const selected = this.state.pins.map(p => p.selection);
    return this.run(async epoch => {
      const response = await this.client.pins(view.context, selected);
      if (!this.current(epoch)) return;
      const results = new Map(response.results.map(r => [metadataSelectionKey(r.selection), r.result]));
      this.publish({ ...this.state, advanceNumber: this.state.advanceNumber + (scheduled ? 1 : 0), pins: this.state.pins.map(p => {
        const result = results.get(metadataSelectionKey(p.selection));
        if (!result) return { ...p, observation: p.observation && { ...p.observation, freshness: 'stale' } };
        if (result.kind === 'present' && (!p.observation || result.row.revision >= p.observation.row.revision))
          return { ...p, result, observation: { row: result.row, freshness: 'observed' } };
        return { ...p, result, observation: p.observation && { ...p.observation, freshness: 'stale' } };
      }) });
    }, () => { if (scheduled) this.publish({ ...this.state, advanceNumber: this.state.advanceNumber + 1 }); });
  }
  select(selection: MetadataSelection | null): void {
    if (this.disposed) return;
    const captured = selection === null ? null : this.captureSelection(selection);
    // A-B-A selection changes invalidate all in-flight work, not just string-key comparisons.
    this.publish({ ...this.state, epoch: this.state.epoch + 1, localDetail: null, detail: captured === null ? { kind: 'none' }
      : this.state.detailCache[metadataSelectionKey(captured)] ?? { kind: 'selected', selection: captured, observation: null, freshness: 'stale',
        cursors: { task_cursor: null, artifact_cursor: null }, error: null } });
  }
  /** Refresh starts both lanes; advance carries the other lane's independent cursor unchanged. */
  readDetail(advance: 'refresh' | 'tasks' | 'artifacts' | HistoryLaneName = 'refresh'): Promise<MetadataActionResult> {
    const view = this.state.view, detail = this.state.detail;
    if (view.kind !== 'view' || view.refresh !== 'idle' || detail.kind !== 'selected') return Promise.resolve('skipped');
    let cursors = detail.cursors;
    if (advance === 'refresh') cursors = { task_cursor: null, artifact_cursor: null };
    else {
      const lane = advance === 'tasks' || advance === 'artifacts' ? detail.observation?.[advance]
        : detail.observation && detail.observation.history.kind !== 'unavailable' ? detail.observation.history[advance] : null;
      if (!lane || lane.page.outcome !== 'partial' || detail.error !== null || detail.freshness !== 'observed') return Promise.resolve('skipped');
      cursors = advance === 'tasks' ? { ...cursors, task_cursor: lane.page.next_cursor }
        : advance === 'artifacts' ? { ...cursors, artifact_cursor: lane.page.next_cursor }
          : { ...cursors, [historyCursors[advance]]: lane.page.next_cursor };
    }
    const captured = cursors;
    return this.run(async epoch => {
      const response = await this.client.detail(view.context, detail.selection, captured);
      if (!this.current(epoch)) return;
      const selectedHistoryUnavailable = advance !== 'refresh' && advance !== 'tasks' && advance !== 'artifacts'
        && (response.history.kind === 'unavailable' || !['complete', 'partial'].includes(response.history[advance].page.outcome));
      const incomplete = selectedHistoryUnavailable || [response.tasks.page.outcome, response.artifacts.page.outcome].some(o => o === 'unavailable' || o === 'cursor_expired');
      // An unavailable selected lookup retains the last observation visibly stale.
      const observation = incomplete && detail.observation ? detail.observation : response;
      this.publish({ ...this.state, detail: { ...detail, observation, cursors: incomplete ? detail.cursors : captured,
        freshness: incomplete ? 'stale' : 'observed', error: incomplete ? 'unavailable' : null } });
    });
  }
}

/** Pass the same store to every consumer. Subscription performs no network work. */
export function useJobMetadata(store: JobMetadataStore): JobMetadataState {
  return useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);
}
