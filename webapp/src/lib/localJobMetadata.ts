import { MetadataError } from './jobMetadata';
import type { MetadataContext, Traversal } from './jobMetadata';

export const nativeActiveStatuses = ['registered', 'queued', 'pending', 'running', 'in_progress', 'started', 'stitching'];
export const nativeAttentionStatuses = ['stalled', 'failed', 'timeout', 'timed_out', 'truncated', 'partial', 'unknown'];
export type LocalLane = 'active' | 'history';
type Priced = { spend_usd: number; source: 'financial_receipt' };
export type LocalEconomics = (
  | { kind: 'unavailable' }
  | (Priced & { kind: 'provider'; estimated: false; cost_provenance: 'provider' })
  | (Priced & { kind: 'measured'; estimated: false; cost_provenance: 'live' | 'static' })
  | (Priced & { kind: 'estimated'; estimated: true; cost_provenance: 'provider' | 'live' | 'static' | 'default' | 'unknown' })
) & { route_forecast_usd?: number; estimated_savings_usd?: number };
export type LocalRef = { job_id: string; incarnation: string };
export type LocalSummary = { local_ref: LocalRef; revision: number; deleted: false; session_id: string;
  lifecycle: string; kind: 'run_command' | 'run_command_batch' | 'parallel_wave' | 'provider';
  parent_ref: LocalRef | null; task_count: number | null; action_count: number | null;
  artifact_count: number | null; child_count: number | null; created_at: number | null; updated_at: number | null;
  receipts: { terminal: boolean; launch: boolean; recovery: boolean; child: boolean }; economics: LocalEconomics;
  display?: { label: string; model: string; adapter: string; truncated: boolean };
  usage?: { kind: 'unknown' } | { kind: 'reported'; tokens: number; source: 'local_job_tokens' };
  accounting?: { kind: 'declared' | 'excluded' | 'unresolved'; aggregation_authority: false } };
export type LocalRow = LocalSummary | { local_ref: LocalRef; revision: number; deleted: true; session_id: string };
export type LocalPage = { outcome: 'complete' | 'partial' | 'expired' | 'unavailable'; revision: number; checkpoint: number; scanned: number; next_cursor: string | null };
export type LocalList = { context: MetadataContext; incarnation: string; rows: LocalRow[]; page: LocalPage; missing: string[] };
export type LocalObservation = { activeRemovalRevision?: number; row: LocalSummary; freshness: 'observed' | 'stale'; observedAt: number };
export type LocalAction = { action_id: string; worker_id: string; kind: string; goal: string; status: string; duration_ms: number | null; error: string; truncated: boolean };
export type LocalTask = { task_id: string | null; role: string; instruction: string; status: string; adapter: string; model: string; model_kind: 'assigned' | 'unavailable'; truncated: boolean };
export type LocalRoute = { ordinal: number; task_id: string | null; association: 'explicit' | 'legacy_owner_single_task' | 'unavailable'; model: string; model_kind: 'forecast' | 'realized'; role: string; policy: string; adapter: string; created_by: string; detail: string; est_cost_usd: number | null; truncated: boolean };
export type SelectedContext = { source: 'goal' | 'command_preview'; request: { text: string; truncated: boolean } | null; cwd: { text: string; truncated: boolean } | null; omission: 'none' | 'request_unavailable' | 'raw_command_not_retained' };
export type LocalDetail = { selected_context?: SelectedContext; summary?: LocalSummary; local_ref: LocalRef; page: LocalPage; missing: string[]; total: number | null } & (
  | { lane: 'tasks'; rows: LocalTask[] }
  | { lane: 'routing'; rows: LocalRoute[] }
  | { lane: 'actions'; rows: (LocalAction | { unavailable: true })[] }
  | { lane: 'children'; rows: ({ local_ref: LocalRef } | { unavailable: true })[] }
  | { lane: 'output'; rows: { offset: number; text: string }[]; output: { coverage: 'in_memory_only'; source_chars: number | null; spilled: boolean } | null }
);
export function localKey(ref: LocalRef): string { return JSON.stringify(['local', ref.incarnation, ref.job_id]); }
function fail(): never { throw new MetadataError('invalid_metadata'); }
function isRecord(v: unknown): v is Record<string, unknown> { return v !== null && typeof v === 'object' && !Array.isArray(v); }
function obj(v: unknown): Record<string, unknown> { if (!isRecord(v)) return fail(); return v; }
function str(v: unknown, max = 256): string { if (typeof v !== 'string' || Array.from(v).length > max) return fail(); return v; }
function num(v: unknown): number { if (typeof v !== 'number' || !Number.isFinite(v)) return fail(); return v; }
function count(v: unknown): number { const n = num(v); if (!Number.isSafeInteger(n) || n < 0) return fail(); return n; }
function nullable(v: unknown): number | null { return v === null ? null : num(v); }
function bool(v: unknown): boolean { if (typeof v !== 'boolean') return fail(); return v; }
function arr(v: unknown, max = 50): unknown[] { if (!Array.isArray(v) || v.length > max) return fail(); return v; }
function ref(v: unknown): LocalRef { const r = obj(v); const job_id = str(r.job_id), incarnation = str(r.incarnation); if (![job_id, incarnation].every(s => /^[a-zA-Z0-9_-]+$/.test(s))) return fail(); return { job_id, incarnation }; }
function missing(v: unknown): string[] { return arr(v, 64).map(v => str(v)); }
function page(v: unknown): LocalPage {
  const p = obj(v), outcome = p.outcome;
  if (outcome !== 'complete' && outcome !== 'partial' && outcome !== 'expired' && outcome !== 'unavailable') return fail();
  const next_cursor = p.next_cursor === null ? null : str(p.next_cursor, 4096);
  if ((outcome === 'partial') !== (next_cursor !== null) || (next_cursor !== null && !/^[A-Za-z0-9_-]+={0,2}$/.test(next_cursor))) return fail();
  const scanned = count(p.scanned); if (scanned > 51) return fail();
  return { outcome, next_cursor, scanned, revision: count(p.revision), checkpoint: count(p.checkpoint) };
}
function envelope(v: unknown, c: MetadataContext, lane: LocalLane = 'history') {
  const o = obj(v), x = obj(o.context);
  if (o.version !== 1 || x.session_id !== c.session_id || x.repo !== c.repo || x.scope !== c.scope || x.view_generation !== c.view_generation) return fail();
  const coverage = obj(o.coverage);
  if (coverage.membership !== (lane === 'active' ? 'retained_local_active' : 'retained_local_history') || (lane === 'active' && coverage.metadata !== 'live_during_traversal') || coverage.historical !== 'unavailable' || coverage.ordering !== 'id') return fail();
  return o;
}
function operatorFacts(r: Record<string, unknown>, kind: LocalSummary['kind']): Pick<LocalSummary, 'economics' | 'display' | 'usage' | 'accounting'> {
  const e = obj(r.economics);
  const forecast = e.route_forecast_usd === undefined ? {} : { route_forecast_usd: amount(e.route_forecast_usd) };
  const savings = e.estimated_savings_usd === undefined ? {} : { estimated_savings_usd: amount(e.estimated_savings_usd) };
  let economics: LocalEconomics;
  if (e.kind === 'unavailable') economics = { kind: 'unavailable', ...forecast, ...savings };
  else {
    const provenance = e.cost_provenance;
    if (e.source !== 'financial_receipt') return fail();
    const priced = { spend_usd: amount(e.spend_usd), source: 'financial_receipt', ...forecast, ...savings } satisfies Priced;
    if (e.kind === 'provider' && provenance === 'provider' && e.estimated === false)
      economics = { ...priced, kind: 'provider', estimated: false, cost_provenance: provenance };
    else if (e.kind === 'measured' && (provenance === 'live' || provenance === 'static') && e.estimated === false)
      economics = { ...priced, kind: 'measured', estimated: false, cost_provenance: provenance };
    else if (e.kind === 'estimated' && (provenance === 'provider' || provenance === 'live' || provenance === 'static' || provenance === 'default' || provenance === 'unknown') && e.estimated === true)
      economics = { ...priced, kind: 'estimated', estimated: true, cost_provenance: provenance };
    else return fail();
  }
  let display: LocalSummary['display'], usage: LocalSummary['usage'], accounting: LocalSummary['accounting'];
  if (r.display !== undefined) {
    const d = obj(r.display);
    const labels = { provider: 'Provider worker', run_command: 'Command', run_command_batch: 'Command batch', parallel_wave: 'Parallel wave' };
    if (d.label !== labels[kind]) return fail();
    display = { label: labels[kind], model: str(d.model, 160), adapter: str(d.adapter, 40), truncated: bool(d.truncated) };
  }
  if (r.usage !== undefined) {
    const u = obj(r.usage);
    if (u.kind === 'unknown') usage = { kind: 'unknown' };
    else if (u.kind === 'reported' && u.source === 'local_job_tokens') {
      const tokens = amount(u.tokens); if (!Number.isInteger(tokens)) return fail();
      usage = { kind: 'reported', tokens, source: 'local_job_tokens' };
    } else return fail();
  }
  if (r.accounting !== undefined) {
    const a = obj(r.accounting);
    if (a.aggregation_authority !== false || (a.kind !== 'declared' && a.kind !== 'excluded' && a.kind !== 'unresolved')) return fail();
    accounting = { kind: a.kind, aggregation_authority: false };
    if (a.kind === 'excluded' && (economics.kind !== 'unavailable' || usage?.kind === 'reported')) return fail();
  }
  return { economics, ...(display ? { display } : {}), ...(usage ? { usage } : {}), ...(accounting ? { accounting } : {}) };
}
function amount(v: unknown): number { const n = num(v); if (n < 0 || n >= 1e18) return fail(); return n; }
function localRow(v: unknown, c: MetadataContext, incarnation: string, lane: LocalLane, upper: number): LocalRow {
  const r = obj(v), local_ref = ref(r.local_ref), revision = count(r.revision), session_id = str(r.session_id);
  if (local_ref.incarnation !== incarnation || (lane === 'history' && revision > upper) || (c.scope === 'session' && session_id !== c.session_id)) return fail();
  if (r.deleted === true) return { local_ref, revision, session_id, deleted: true };
  if (r.deleted !== false) return fail();
  const kind = r.kind;
  if (kind !== 'run_command' && kind !== 'run_command_batch' && kind !== 'parallel_wave' && kind !== 'provider') return fail();
  const receipts = obj(r.receipts);
  const parent_ref = r.parent_ref === null ? null : ref(r.parent_ref);
  if (parent_ref && parent_ref.incarnation !== incarnation) return fail();
  return { local_ref, revision, session_id, deleted: false, lifecycle: str(r.lifecycle, 40), kind, parent_ref,
    task_count: r.task_count === null ? null : count(r.task_count), action_count: r.action_count === null ? null : count(r.action_count),
    artifact_count: r.artifact_count === null ? null : count(r.artifact_count), child_count: r.child_count === null ? null : count(r.child_count),
    created_at: nullable(r.created_at), updated_at: nullable(r.updated_at), ...operatorFacts(r, kind),
    receipts: { terminal: bool(receipts.terminal), launch: bool(receipts.launch), recovery: bool(receipts.recovery), child: bool(receipts.child) } };
}
export function parseLocalList(v: unknown, c: MetadataContext, incarnation: string, traversal: Traversal, lane: LocalLane = 'history'): LocalList {
  const o = envelope(v, c, lane); if (o.incarnation !== incarnation || (o.lane ?? 'history') !== lane) return fail();
  const p = page(o.page);
  const rows = arr(o.rows).map(v => localRow(v, c, incarnation, lane, p.revision));
  if (rows.length > p.scanned || ((p.outcome === 'expired' || p.outcome === 'unavailable') && rows.length)
    || (p.outcome === 'partial' && (p.checkpoint !== traversal.after_revision || p.next_cursor === traversal.cursor))
    || ((p.outcome === 'complete' || p.outcome === 'partial') && p.revision < traversal.after_revision)
    || ((p.outcome === 'expired' || p.outcome === 'unavailable') && p.checkpoint !== traversal.after_revision)
    || (p.outcome === 'complete' && p.checkpoint !== p.revision)) return fail();
  // Changes may contain several revisions of the same identity in one page.
  return { context: c, incarnation, rows, page: p, missing: missing(o.missing) };
}
function selectedContext(v: unknown, summary: LocalSummary): SelectedContext {
  const r = obj(v);
  if (Object.keys(r).sort().join(',') !== 'cwd,omission,request,source') return fail();
  const source = summary.kind === 'run_command' || summary.kind === 'run_command_batch' ? 'command_preview' : 'goal';
  if (r.source !== source) return fail();
  function bounded(v: unknown, max: number) {
    if (v === null) return null;
    const b = obj(v);
    if (Object.keys(b).sort().join(',') !== 'text,truncated') return fail();
    const text = str(b.text, max);
    if (new TextEncoder().encode(text).length > max) return fail();
    return { text, truncated: bool(b.truncated) };
  }
  const request = bounded(r.request, 2048), cwd = bounded(r.cwd, 512);
  const omission = request === null ? 'request_unavailable' : source === 'command_preview' ? 'raw_command_not_retained' : 'none';
  if (r.omission !== omission) return fail();
  return { source, request, cwd, omission };
}
export function parseLocalDetail(v: unknown, c: MetadataContext, selected: LocalRef, lane: LocalDetail['lane'], includeContext = false): LocalDetail {
  const o = envelope(v, c), local_ref = ref(o.local_ref), p = page(o.page);
  if (localKey(local_ref) !== localKey(selected) || o.lane !== lane || o.cancellation_authority !== false) return fail();
  let summary: LocalSummary | undefined;
  if (o.summary !== undefined) {
    const parsed = localRow(o.summary, c, selected.incarnation, 'active', p.revision);
    if (parsed.deleted || localKey(parsed.local_ref) !== localKey(selected)) return fail();
    summary = parsed;
  }
  if (o.selected_context !== undefined && (!includeContext || !summary || p.outcome === 'expired')) return fail();
  const selected_context = o.selected_context !== undefined && summary ? selectedContext(o.selected_context, summary) : undefined;
  const base = { ...(selected_context ? { selected_context } : {}), ...(summary ? { summary } : {}), local_ref, page: p, missing: missing(o.missing), total: o.total === undefined ? null : count(o.total) };
  const rows = arr(o.rows);
  if ((lane === 'tasks' || lane === 'routing') && summary && summary.revision !== p.revision) return fail();
  if (rows.length > p.scanned || ((p.outcome === 'expired' || p.outcome === 'unavailable') && rows.length)) return fail();
  function taskId(v: unknown): string | null {
    if (v === null) return null;
    const id = str(v); if (!/^[a-zA-Z0-9_-]+$/.test(id)) return fail(); return id;
  }
  switch (lane) {
    case 'tasks': return { ...base, lane, rows: rows.map(v => {
      const r = obj(v), model = str(r.model, 160), model_kind = r.model_kind;
      if ((model_kind !== 'assigned' && model_kind !== 'unavailable') || (model_kind === 'unavailable' && model !== '') || (model_kind === 'assigned' && !model)) return fail();
      return { task_id: taskId(r.task_id), role: str(r.role, 160), instruction: str(r.instruction, 1024), status: str(r.status, 40), adapter: str(r.adapter, 40), model, model_kind, truncated: bool(r.truncated) };
    }) };
    case 'routing': {
      let previousOrdinal = -1;
      return { ...base, lane, rows: rows.map(v => {
      const r = obj(v), association = r.association, model_kind = r.model_kind, task_id = taskId(r.task_id);
      if (association !== 'explicit' && association !== 'legacy_owner_single_task' && association !== 'unavailable') return fail();
      if (model_kind !== 'forecast' && model_kind !== 'realized') return fail();
      if ((association === 'unavailable') !== (task_id === null)) return fail();
      const ordinal = count(r.ordinal); if (ordinal <= previousOrdinal) return fail(); previousOrdinal = ordinal;
      return { ordinal, task_id, association, model: str(r.model, 160), model_kind, role: str(r.role, 160), policy: str(r.policy, 40), adapter: str(r.adapter, 40), created_by: str(r.created_by, 40), detail: str(r.detail, 512), est_cost_usd: r.est_cost_usd === null ? null : amount(r.est_cost_usd), truncated: bool(r.truncated) };
    }) }; }
    case 'actions': return { ...base, lane, rows: rows.map((v): LocalAction | { unavailable: true } => { const r = obj(v); if (r.unavailable === true) return { unavailable: true }; return { action_id: str(r.action_id, 128), worker_id: str(r.worker_id), kind: str(r.kind, 64), goal: str(r.goal, 240), status: str(r.status, 40), error: str(r.error, 240), duration_ms: nullable(r.duration_ms), truncated: bool(r.truncated) }; }) };
    case 'children': return { ...base, lane, rows: rows.map(v => { const r = obj(v); if (r.unavailable === true) return { unavailable: true }; const child = ref(r.local_ref); if (child.incarnation !== selected.incarnation) return fail(); return { local_ref: child }; }) };
    case 'output': { const output = o.output === undefined ? null : obj(o.output); if (output && output.coverage !== 'in_memory_only') return fail(); return { ...base, lane, rows: rows.map(v => { const r = obj(v); return { offset: count(r.offset), text: str(r.text, 2048) }; }), output: output ? { coverage: 'in_memory_only', source_chars: nullable(output.source_chars), spilled: bool(output.spilled) } : null }; }
  }
}
export function localPath(c: MetadataContext, t: Traversal, lane: LocalLane = 'history'): string {
  const p = new URLSearchParams(c); p.set('mode', t.mode); p.set('lane', lane);
  if (t.mode === 'changes') p.set('after_revision', String(t.after_revision));
  if (t.cursor !== null) p.set('cursor', t.cursor);
  return '/api/jobs/metadata/local?' + p;
}
