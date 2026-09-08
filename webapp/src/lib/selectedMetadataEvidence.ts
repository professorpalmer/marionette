import { MetadataError } from './jobMetadata';
import { isPublicJobRef, jobRefKey } from './publicJobRef';
import type { PublicJobRef } from './publicJobRef';

export type MetadataDisplay = { kind: 'unavailable'; reason?: string } | {
  kind: 'available'; goal_preview: string; goal_preview_truncated: boolean | null;
  delivery: 'pending' | 'blocked' | 'unverified' | 'unavailable'; quality: 'unverified' | 'unavailable';
};
export type SelectedMetric = { total: number | null; state: 'unknown' | 'partial' | 'measured' | 'estimated';
  known_selected: number | null; unknown_selected: number | null; estimated_selected: number | null; conflicting_selected: number | null };
export const selectedMetricNames = ['tokens_in', 'tokens_out', 'cache_read_tokens', 'cache_write_tokens', 'api_cost_usd', 'plan_marginal_cost_usd', 'api_equivalent_cost_usd'] as const;
export type SelectedEconomics = { kind: 'available' | 'unavailable'; job_ref: PublicJobRef; outcome: 'available' | 'unavailable';
  summary_revision: number | null; receipt_digest: string | null; source: 'terminal_receipt' | 'unavailable';
  coverage: 'selected_receipt' | 'unknown'; selected_count: number | null;
  totals: Record<typeof selectedMetricNames[number], SelectedMetric> | null; reason: string | null; retry_after_ms: number | null };
export type HistoryLaneName = 'attempts' | 'runs' | 'process_outcomes' | 'observations';
export type HistoryCursorName = 'attempt_cursor' | 'run_cursor' | 'process_outcome_cursor' | 'observation_cursor';
export const historyCursors = { attempts: 'attempt_cursor', runs: 'run_cursor', process_outcomes: 'process_outcome_cursor', observations: 'observation_cursor' } satisfies Record<HistoryLaneName, HistoryCursorName>;
export type HistoryRow = { job_ref: PublicJobRef; kind: string; sequence: number; facts: Record<string, string | number | boolean | null>;
  completion?: { job_ref: PublicJobRef; run_id: string; intent_digest: string | null; outcome: string } };
export type HistoryLane = { page: { outcome: 'complete' | 'partial' | 'unavailable' | 'cursor_expired'; next_cursor: string | null;
  scanned: number; captured_count: number | null; coverage: string; complete_invocation_history: false }; rows: HistoryRow[] };
export type SelectedHistory = { kind: 'unavailable'; reason: string } | ({ kind: 'available' | 'partial'; missing?: string[]; counts: {
  captured_attempts: number | null; captured_runs: number | null; captured_process_outcomes: number | null; captured_observations: number | null;
  outcome: 'available' | 'unavailable'; coverage: 'captured' | 'partial' | 'unknown'; complete_invocation_history: false;
} } & Record<HistoryLaneName, HistoryLane>);
function fail(): never { throw new MetadataError('invalid_metadata'); }
function object(v: unknown): Record<string, unknown> { if (!v || typeof v !== 'object' || Array.isArray(v)) return fail(); return v as Record<string, unknown>; }
function string(v: unknown, max = 2048): string { if (typeof v !== 'string' || new TextEncoder().encode(v).length > max) return fail(); return v; }
function count(v: unknown): number { if (typeof v !== 'number' || !Number.isSafeInteger(v) || v < 0) return fail(); return v; }
function nullableCount(v: unknown): number | null { return v === null ? null : count(v); }
function nullableString(v: unknown): string | null { return v === null ? null : string(v); }
function choice<T extends string>(v: unknown, choices: readonly T[]): T { for (const c of choices) if (v === c) return c; return fail(); }
function ref(v: unknown, expected: PublicJobRef): PublicJobRef { if (!isPublicJobRef(v) || jobRefKey(v) !== jobRefKey(expected)) return fail(); return { ...v }; }
export function parseMetadataDisplay(value: unknown): MetadataDisplay {
  const v = object(value);
  if (v.kind === 'unavailable') return { kind: 'unavailable', ...(v.reason === undefined ? {} : { reason: string(v.reason) }) };
  if (v.kind !== 'available' || (v.goal_preview_truncated !== null && typeof v.goal_preview_truncated !== 'boolean')) return fail();
  return { kind: 'available', goal_preview: string(v.goal_preview), goal_preview_truncated: v.goal_preview_truncated,
    delivery: choice(v.delivery, ['pending', 'blocked', 'unverified', 'unavailable']), quality: choice(v.quality, ['unverified', 'unavailable']) };
}
function metric(value: unknown): SelectedMetric {
  const v = object(value), state = choice(v.state, ['unknown', 'partial', 'measured', 'estimated']);
  if (v.total !== null && (typeof v.total !== 'number' || !Number.isFinite(v.total) || v.total < 0)) return fail();
  if (state === 'unknown' && v.total !== null) return fail();
  return { total: v.total, state, known_selected: nullableCount(v.known_selected), unknown_selected: nullableCount(v.unknown_selected),
    estimated_selected: nullableCount(v.estimated_selected), conflicting_selected: nullableCount(v.conflicting_selected) };
}
export function parseSelectedEconomics(value: unknown, expected: PublicJobRef): SelectedEconomics {
  const v = object(value), totals = v.totals === null ? null : object(v.totals);
  const kind = choice(v.kind, ['available', 'unavailable']);
  if (v.outcome !== kind) return fail();
  const source = choice(v.source, ['terminal_receipt', 'unavailable']), coverage = choice(v.coverage, ['selected_receipt', 'unknown']);
  if (totals !== null && (source !== 'terminal_receipt' || coverage !== 'selected_receipt')) return fail();
  return { kind, job_ref: ref(v.job_ref, expected), outcome: kind,
    summary_revision: nullableCount(v.summary_revision), receipt_digest: nullableString(v.receipt_digest), source, coverage,
    selected_count: nullableCount(v.selected_count), reason: nullableString(v.reason), retry_after_ms: nullableCount(v.retry_after_ms),
    totals: totals === null ? null : { tokens_in: metric(totals.tokens_in), tokens_out: metric(totals.tokens_out),
      cache_read_tokens: metric(totals.cache_read_tokens), cache_write_tokens: metric(totals.cache_write_tokens),
      api_cost_usd: metric(totals.api_cost_usd), plan_marginal_cost_usd: metric(totals.plan_marginal_cost_usd), api_equivalent_cost_usd: metric(totals.api_equivalent_cost_usd) } };
}
function historyLane(value: unknown, expected: PublicJobRef, previous?: string | null): HistoryLane {
  const v = object(value), p = object(v.page);
  const outcome = choice(p.outcome, ['complete', 'partial', 'unavailable', 'cursor_expired']);
  const next = p.next_cursor === null ? null : string(p.next_cursor, 8192);
  if (p.complete_invocation_history !== false || (outcome === 'partial' ? !next || next === previous || next.length < 44 || next.length % 4 !== 0 || !/^[A-Za-z0-9_-]+={0,2}$/.test(next) : next !== null)) return fail();
  if (!Array.isArray(v.rows) || v.rows.length > 20) return fail();
  const rows = v.rows.map(value => {
    const row = object(value), raw = object(row.facts), facts: HistoryRow['facts'] = {};
    if (Object.keys(raw).length > 64) return fail();
    for (const [key, value] of Object.entries(raw)) {
      string(key, 128);
      if (value !== null && typeof value !== 'string' && typeof value !== 'number' && typeof value !== 'boolean') return fail();
      if (typeof value === 'string') string(value, 8192);
      if (typeof value === 'number' && !Number.isFinite(value)) return fail();
      facts[key] = value;
    }
    const result: HistoryRow = { job_ref: ref(row.job_ref, expected), kind: string(row.kind, 64), sequence: count(row.sequence), facts };
    if (row.completion !== undefined) {
      const c = object(row.completion);
      if (facts.id !== c.run_id) return fail();
      result.completion = { job_ref: ref(c.job_ref, expected), run_id: string(c.run_id), intent_digest: nullableString(c.intent_digest),
        outcome: choice(c.outcome, ['pending_publication', 'published', 'stale_lease', 'invalidated', 'legacy_unknown', 'unavailable']) };
    }
    return result;
  });
  const scanned = count(p.scanned);
  if (scanned > 21 || rows.length > scanned || new Set(rows.map(r => r.sequence)).size !== rows.length
    || ((outcome === 'unavailable' || outcome === 'cursor_expired') && rows.length)) return fail();
  return { page: { outcome, next_cursor: next, scanned, captured_count: nullableCount(p.captured_count),
    coverage: choice(p.coverage, ['captured', 'partial', 'unknown']), complete_invocation_history: false }, rows };
}
export function parseSelectedHistory(value: unknown, expected: PublicJobRef, cursors: Partial<Record<HistoryCursorName, string | null>>): SelectedHistory {
  const v = object(value);
  if (v.kind === 'unavailable' && !('counts' in v)) return { kind: 'unavailable', reason: string(v.reason) };
  if (v.kind !== 'available' && v.kind !== 'unavailable') return fail();
  const missing = v.missing ?? [];
  if (!Array.isArray(missing) || missing.length > 64) return fail();
  const c = object(v.counts);
  if (c.complete_invocation_history !== false) return fail();
  return { kind: v.kind === 'available' ? 'available' : 'partial', missing: missing.map(value => string(value, 256)), counts: { captured_attempts: nullableCount(c.captured_attempts), captured_runs: nullableCount(c.captured_runs),
    captured_process_outcomes: nullableCount(c.captured_process_outcomes), captured_observations: nullableCount(c.captured_observations),
    outcome: choice(c.outcome, ['available', 'unavailable']), coverage: choice(c.coverage, ['captured', 'partial', 'unknown']), complete_invocation_history: false },
    attempts: historyLane(v.attempts, expected, cursors.attempt_cursor), runs: historyLane(v.runs, expected, cursors.run_cursor),
    process_outcomes: historyLane(v.process_outcomes, expected, cursors.process_outcome_cursor), observations: historyLane(v.observations, expected, cursors.observation_cursor) };
}
