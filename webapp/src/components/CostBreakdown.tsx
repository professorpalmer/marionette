// CostBreakdown -- the Economics pane body (process / this-app-run meters).
//
// It turns Marionette's per-task model routing into a visible value prop:
// "why this model / what it saved". It consumes ONLY fields already served by
// /api/usage (est_cost_usd, cache_savings_usd, price_in, price_out,
// tokens_used, tokens_cached) and degrades gracefully -- any field that is
// absent or zero simply renders nothing rather than "$0.000000" noise or NaN.

import { useState } from "react";
import { api, type UsageData } from "../lib/api";

export type CostBreakdownData = {
  tokens_used: number;
  est_cost_usd: number;
  cost_source?: "provider" | "estimated" | "mixed" | "plan_estimated";
  /** live | static | default | unknown — how display rates were resolved. */
  price_source?: "live" | "static" | "default" | "unknown";
  /** True when spend is not a full provider receipt. */
  estimated?: boolean;
  tokens_cached?: number;
  pilot_input_tokens?: number;
  pilot_cache_read_tokens?: number;
  pilot_cache_hit_ratio?: number | null;
  swarm_input_tokens?: number;
  swarm_cache_read_tokens?: number;
  swarm_cache_hit_ratio?: number | null;
  prompt_input_tokens?: number;
  prompt_cache_read_tokens?: number;
  prompt_cache_hit_ratio?: number | null;
  cache_savings_usd?: number;
  /** Uncapped catalog/list-price cache value (grows with cached tokens). */
  cache_savings_gross_usd?: number;
  /** catalog | capped | unknown — how reconciled cache savings were attributed. */
  cache_savings_basis?: "catalog" | "capped" | "unknown";
  routing_saved_usd?: number;
  /** actual_usage | estimated | unknown — how routing decision value was measured. */
  routing_savings_basis?: "actual_usage" | "estimated" | "unknown";
  routing_tokens_compared?: number;
  /** Model-selection value: worker usage at frontier baseline vs chosen model. */
  delegation_saved_usd?: number;
  /** actual_usage | unknown — how delegation value was measured. */
  delegation_savings_basis?: "actual_usage" | "estimated" | "unknown";
  delegation_tokens_compared?: number;
  cache_saved_usd_swarm?: number;
  swarm_cache_savings_basis?: "actual_usage" | "estimated" | "unknown";
  swarm_cache_unpriced_tokens?: number;
  tool_output_tokens_saved?: number;
  tool_output_savings_usd?: number;
  history_compactions?: number;
  history_tokens_saved?: number;
  history_cache_bust_tokens?: number;
  history_thrash_events?: number;
  /** Measured summarizer USD only — never inferred from tokens. */
  history_compaction_cost_usd?: number;
  spill_count?: number;
  spill_chars?: number;
  evals_recorded?: number;
  evals_failed?: number;
  memory_layers?: Record<string, { bytes?: number; entries?: number }>;
  compaction_advice?: {
    level?: string;
    hot_ratio?: number;
    l1_bytes?: number;
    l3_reclaimed_bytes?: number;
    reasons?: string[];
    needs_intervention?: boolean;
    warning_reason?: string;
  };
  history_compaction_ran?: boolean;
  /** estimated — standing floor / TTL (HARNESS_STANDING_ECONOMICS; flag-off omits). */
  standing_economics_basis?: "estimated";
  standing_system_tokens?: number;
  standing_tool_tokens?: number;
  standing_floor_tokens?: number;
  standing_floor_cost_usd?: number;
  standing_floor_cost_cached_usd?: number;
  prompt_cache_ttl_ms?: number;
  prompt_cache_age_ms?: number;
  prompt_cache_expires_in_ms?: number;
  prompt_cache_state?: "warm" | "expired";
  price_in?: number;
  price_out?: number;
  /** Locked cumulative spend per pilot that actually ran (picker-safe). */
  pilot_by_model?: Array<{
    model: string;
    est_cost_usd: number;
    tokens_used?: number;
    tokens_in?: number;
    tokens_out?: number;
    tokens_cached?: number;
  }>;
};

/** Map GET /api/usage session meters into the Economics panel body. */
export function usageToCostBreakdownData(
  session: UsageData["session"],
): CostBreakdownData {
  return {
    tokens_used: session.tokens_used,
    est_cost_usd: session.est_cost_usd,
    cost_source: session.cost_source,
    price_source: session.price_source,
    estimated: session.estimated,
    tokens_cached: session.tokens_cached,
    pilot_input_tokens: session.pilot_input_tokens,
    pilot_cache_read_tokens: session.pilot_cache_read_tokens,
    pilot_cache_hit_ratio: session.pilot_cache_hit_ratio,
    swarm_input_tokens: session.swarm_input_tokens,
    swarm_cache_read_tokens: session.swarm_cache_read_tokens,
    swarm_cache_hit_ratio: session.swarm_cache_hit_ratio,
    prompt_input_tokens: session.prompt_input_tokens,
    prompt_cache_read_tokens: session.prompt_cache_read_tokens,
    prompt_cache_hit_ratio: session.prompt_cache_hit_ratio,
    cache_savings_usd: session.cache_savings_usd,
    cache_savings_gross_usd: session.cache_savings_gross_usd,
    cache_savings_basis: session.cache_savings_basis,
    routing_saved_usd: session.routing_saved_usd,
    routing_savings_basis: session.routing_savings_basis,
    routing_tokens_compared: session.routing_tokens_compared,
    delegation_saved_usd: session.delegation_saved_usd,
    delegation_savings_basis: session.delegation_savings_basis,
    delegation_tokens_compared: session.delegation_tokens_compared,
    cache_saved_usd_swarm: session.cache_saved_usd_swarm,
    swarm_cache_savings_basis: session.swarm_cache_savings_basis,
    swarm_cache_unpriced_tokens: session.swarm_cache_unpriced_tokens,
    tool_output_tokens_saved: session.tool_output_tokens_saved,
    tool_output_savings_usd: session.tool_output_savings_usd,
    history_compactions: session.history_compactions,
    history_tokens_saved: session.history_tokens_saved,
    history_cache_bust_tokens: session.history_cache_bust_tokens,
    history_thrash_events: session.history_thrash_events,
    history_compaction_cost_usd: session.history_compaction_cost_usd,
    spill_count: session.spill_count,
    spill_chars: session.spill_chars,
    evals_recorded: session.evals_recorded,
    evals_failed: session.evals_failed,
    memory_layers: session.memory_layers,
    compaction_advice: session.compaction_advice,
    history_compaction_ran: session.history_compaction_ran,
    standing_economics_basis: session.standing_economics_basis,
    standing_system_tokens: session.standing_system_tokens,
    standing_tool_tokens: session.standing_tool_tokens,
    standing_floor_tokens: session.standing_floor_tokens,
    standing_floor_cost_usd: session.standing_floor_cost_usd,
    standing_floor_cost_cached_usd: session.standing_floor_cost_cached_usd,
    prompt_cache_ttl_ms: session.prompt_cache_ttl_ms,
    prompt_cache_age_ms: session.prompt_cache_age_ms,
    prompt_cache_expires_in_ms: session.prompt_cache_expires_in_ms,
    prompt_cache_state: session.prompt_cache_state,
    price_in: session.price_in,
    price_out: session.price_out,
    pilot_by_model: session.pilot_by_model,
  };
}

/** Credit routing USD only when basis is actual or estimated — never unknown. */
export function routingSavingsCredited(
  basis: CostBreakdownData["routing_savings_basis"] | undefined,
  usd: unknown,
): number {
  const value =
    typeof usd === "number" && Number.isFinite(usd) && usd > 0 ? usd : 0;
  if (value <= 0) return 0;
  if (basis === "unknown") return 0;
  // Missing basis on older payloads: treat as estimated (backward compatible).
  if (basis === "actual_usage" || basis === "estimated" || basis == null) {
    return value;
  }
  return 0;
}

/** Credit delegation USD only for measured actual_usage — refuse unknown. */
export function delegationSavingsCredited(
  basis: CostBreakdownData["delegation_savings_basis"] | undefined,
  usd: unknown,
): number {
  const value =
    typeof usd === "number" && Number.isFinite(usd) && usd > 0 ? usd : 0;
  if (value <= 0) return 0;
  if (basis === "actual_usage") return value;
  // Missing basis with positive USD: legacy measured path.
  if (basis == null) return value;
  return 0;
}

/** Compact spend is estimated unless a full provider receipt backs it. */
export function spendIsEstimated(data: Pick<CostBreakdownData, "cost_source" | "estimated" | "price_source">): boolean {
  if (typeof data.estimated === "boolean") return data.estimated;
  if (data.cost_source === "provider") return false;
  if (data.price_source === "default" || data.price_source === "unknown") return true;
  return true;
}

/** Honest prompt-cache hit %: cache_read / input only.
 *
 * Null when unknown, non-finite, negative, or ``ratio > 1`` (invalid provider
 * skew — never clamp into 100%). Bare ``0`` stays null so a cold lane does not
 * paint a green "0% cache" chip; positive ratios only.
 */
export function formatCacheHitPercent(ratio: number | null | undefined): string | null {
  if (typeof ratio !== "number" || !Number.isFinite(ratio) || ratio <= 0 || ratio > 1) {
    return null;
  }
  const pct = ratio * 100;
  if (pct >= 10) return `${Math.round(pct)}%`;
  if (pct >= 1) return `${pct.toFixed(1)}%`;
  return "<1%";
}

export type CacheHitDisplay = {
  percent: string | null;
  label: string;
  title: string;
};

function positiveCacheReads(
  data: Pick<
    CostBreakdownData,
    | "prompt_cache_read_tokens"
    | "pilot_cache_read_tokens"
    | "swarm_cache_read_tokens"
    | "tokens_cached"
  >,
): number {
  const candidates = [
    data.prompt_cache_read_tokens,
    data.tokens_cached,
    (typeof data.pilot_cache_read_tokens === "number" ? data.pilot_cache_read_tokens : 0)
      + (typeof data.swarm_cache_read_tokens === "number" ? data.swarm_cache_read_tokens : 0),
  ];
  for (const value of candidates) {
    if (typeof value === "number" && Number.isFinite(value) && value > 0) return value;
  }
  return 0;
}

/**
 * Pick the best labeled cache-hit basis for StatusBar / CostBreakdown.
 * Prefers combined prompt ratio, then pilot, then swarm. Never invents a
 * percent from cache÷process-total (output + cold workers). Suppresses a
 * percent entirely when there are no cache reads (no green 0% chip).
 */
export function cacheHitDisplay(
  data: Pick<
    CostBreakdownData,
    | "prompt_cache_hit_ratio"
    | "pilot_cache_hit_ratio"
    | "swarm_cache_hit_ratio"
    | "prompt_cache_read_tokens"
    | "pilot_cache_read_tokens"
    | "swarm_cache_read_tokens"
    | "tokens_cached"
  >,
): CacheHitDisplay {
  const tip =
    "Prompt-cache hit rate is cache-read ÷ prompt-input tokens for that lane. " +
    "Cold first turns and independent workers start uncached; this is not " +
    "cache÷process-total (which includes output). Affinity is only what the provider reported.";
  const reads = positiveCacheReads(data);
  if (reads <= 0) {
    return {
      percent: null,
      label: "prompt cache",
      title: `Cache hit % unavailable — no prompt-cache reads yet. ${tip}`,
    };
  }
  const combined = formatCacheHitPercent(data.prompt_cache_hit_ratio);
  if (combined != null) {
    return { percent: combined, label: "prompt cache", title: `Combined pilot+swarm ${tip}` };
  }
  const pilot = formatCacheHitPercent(data.pilot_cache_hit_ratio);
  if (pilot != null) {
    return { percent: pilot, label: "pilot cache", title: `Pilot lane ${tip}` };
  }
  const swarm = formatCacheHitPercent(data.swarm_cache_hit_ratio);
  if (swarm != null) {
    return { percent: swarm, label: "swarm cache", title: `Swarm lane ${tip}` };
  }
  return {
    percent: null,
    label: "prompt cache",
    title: `Cache hit % unavailable — prompt-input denominator unknown. ${tip}`,
  };
}

/** Additive list-price value shown in both footer and receipt.

 * Routing / delegation / provider-cache remain separate mechanisms. Unknown
 * basis is refused. History-compaction USD and standing-floor estimates are
 * never folded in (no second cost plane).
 */
export function listPriceValueTotal(
  data: Pick<
    CostBreakdownData,
    | "cache_savings_gross_usd"
    | "cache_savings_usd"
    | "cache_saved_usd_swarm"
    | "delegation_saved_usd"
    | "delegation_savings_basis"
    | "routing_saved_usd"
    | "routing_savings_basis"
    | "tool_output_savings_usd"
  >,
): number {
  const positive = (value: unknown) =>
    typeof value === "number" && Number.isFinite(value) && value > 0 ? value : 0;
  const pilotCache =
    typeof data.cache_savings_gross_usd === "number" &&
    Number.isFinite(data.cache_savings_gross_usd)
      ? positive(data.cache_savings_gross_usd)
      : positive(data.cache_savings_usd);
  const delegationMeasured = data.delegation_savings_basis === "actual_usage";
  const delegation = delegationSavingsCredited(
    data.delegation_savings_basis,
    data.delegation_saved_usd,
  );
  const routing = routingSavingsCredited(
    data.routing_savings_basis,
    data.routing_saved_usd,
  );
  // Measured zero delegation must not be replaced by a routing estimate.
  // Otherwise prefer credited delegation, else credited routing — never both.
  const modelSelection = delegationMeasured
    ? delegation
    : (delegation > 0 ? delegation : routing);
  return (
    pilotCache
    + positive(data.cache_saved_usd_swarm)
    + modelSelection
    + positive(data.tool_output_savings_usd)
  );
}

export type ListPriceEvidenceBasis = "measured" | "estimated" | "partial" | "unknown";

const EVIDENCE_RANK: Record<ListPriceEvidenceBasis, number> = {
  measured: 0,
  estimated: 1,
  partial: 2,
  unknown: 3,
};

function weakerEvidence(
  current: ListPriceEvidenceBasis | null,
  next: ListPriceEvidenceBasis,
): ListPriceEvidenceBasis {
  if (!current) return next;
  return EVIDENCE_RANK[next] > EVIDENCE_RANK[current] ? next : current;
}

/** Weakest evidence basis among lines credited into listPriceValueTotal.
 *
 * Unknown / partial / estimated beat measured. Does not change the dollar math.
 */
export function listPriceValueWeakestBasis(
  data: Pick<
    CostBreakdownData,
    | "cache_savings_gross_usd"
    | "cache_savings_usd"
    | "cache_savings_basis"
    | "cache_saved_usd_swarm"
    | "swarm_cache_savings_basis"
    | "swarm_cache_unpriced_tokens"
    | "delegation_saved_usd"
    | "delegation_savings_basis"
    | "routing_saved_usd"
    | "routing_savings_basis"
    | "tool_output_savings_usd"
  >,
): ListPriceEvidenceBasis | null {
  const positive = (value: unknown) =>
    typeof value === "number" && Number.isFinite(value) && value > 0 ? value : 0;
  const pilotCache =
    typeof data.cache_savings_gross_usd === "number" &&
    Number.isFinite(data.cache_savings_gross_usd)
      ? positive(data.cache_savings_gross_usd)
      : positive(data.cache_savings_usd);
  const swarm = positive(data.cache_saved_usd_swarm);
  const delegationMeasured = data.delegation_savings_basis === "actual_usage";
  const delegation = delegationSavingsCredited(
    data.delegation_savings_basis,
    data.delegation_saved_usd,
  );
  const routing = routingSavingsCredited(
    data.routing_savings_basis,
    data.routing_saved_usd,
  );
  const modelSelection = delegationMeasured
    ? delegation
    : (delegation > 0 ? delegation : routing);
  const compact = positive(data.tool_output_savings_usd);

  let weakest: ListPriceEvidenceBasis | null = null;
  if (pilotCache > 0) {
    weakest = weakerEvidence(
      weakest,
      data.cache_savings_basis === "unknown"
        ? "unknown"
        : data.cache_savings_basis === "capped"
          ? "estimated"
          : "measured",
    );
  }
  if (swarm > 0) {
    const unpriced =
      typeof data.swarm_cache_unpriced_tokens === "number"
      && Number.isFinite(data.swarm_cache_unpriced_tokens)
      && data.swarm_cache_unpriced_tokens > 0;
    weakest = weakerEvidence(
      weakest,
      data.swarm_cache_savings_basis === "unknown" || unpriced
        ? "partial"
        : data.swarm_cache_savings_basis === "estimated"
          ? "estimated"
          : "measured",
    );
  }
  if (modelSelection > 0) {
    const modelBasis: ListPriceEvidenceBasis =
      delegationMeasured || (delegation > 0 && data.delegation_savings_basis !== "estimated")
        ? "measured"
        : data.routing_savings_basis === "estimated" || data.routing_savings_basis == null
          ? "estimated"
          : "measured";
    weakest = weakerEvidence(weakest, modelBasis);
  }
  if (compact > 0) weakest = weakerEvidence(weakest, "estimated");
  return weakest;
}

export function listPriceValueHeading(
  basis: ListPriceEvidenceBasis | null,
): string {
  if (basis === "unknown") return "List-price value (unknown basis)";
  if (basis === "partial") return "List-price value (partial)";
  if (basis === "estimated") return "List-price value (est.)";
  return "List-price value";
}

function fmtDurationMs(ms: number): string {
  if (ms > 0 && ms < 60_000) return "<1m";
  const mins = Math.max(0, Math.round(ms / 60_000));
  if (mins < 60) return `${mins}m`;
  const h = Math.floor(mins / 60);
  const rem = mins % 60;
  return rem ? `${h}h ${rem}m` : `${h}h`;
}

/** Calm user-facing copy for compaction advice. Machine reasons stay in title. */
export function compactionAdvicePresentation(
  level: string | undefined,
): { label: string; message: string; showCompactAction: boolean } {
  if (level === "soon") {
    return {
      label: "Long session",
      message:
        "This conversation is getting long. Older history can be tidied to keep responses fast and costs down.",
      showCompactAction: true,
    };
  }
  return {
    label: "Needs attention",
    message:
      "This conversation is very long. Compact it now or start a fresh session for best results.",
    showCompactAction: true,
  };
}

// Local formatter so this subcomponent stays self-contained. Mirrors the
// StatusBar cost formatting (coarser as the number grows) but never emits a
// bare "$0.00" for a value that is meaningfully zero -- callers gate on that.
/** Last path segment of a provider:model spec for the cost rows. */
export function shortPilotModel(spec: string): string {
  const text = (spec || "").trim();
  if (!text) return "unknown";
  const afterColon = text.includes(":") ? text.slice(text.lastIndexOf(":") + 1) : text;
  const slash = afterColon.lastIndexOf("/");
  return slash >= 0 ? afterColon.slice(slash + 1) : afterColon;
}

function fmtCost(num: number): string {
  if (!isFinite(num) || num <= 0) return "$0.00";
  if (num < 0.001) return `$${num.toFixed(4)}`;
  if (num < 0.01) return `$${num.toFixed(3)}`;
  return `$${num.toFixed(2)}`;
}

function fmtTokens(num: number): string {
  if (!isFinite(num) || num <= 0) return "0";
  if (num >= 1000000) return (num / 1000000).toFixed(1).replace(/\.0$/, "") + "M";
  if (num >= 1000) return (num / 1000).toFixed(1).replace(/\.0$/, "") + "k";
  return String(num);
}

function fmtBytes(num: number): string {
  if (!isFinite(num) || num <= 0) return "0 B";
  if (num >= 1024 * 1024) return (num / (1024 * 1024)).toFixed(1).replace(/\.0$/, "") + " MB";
  if (num >= 1024) return (num / 1024).toFixed(1).replace(/\.0$/, "") + " KB";
  return `${num} B`;
}

function compactFailureReason(err: unknown): string {
  if (err && typeof err === "object" && "reason" in err) {
    return String((err as { reason?: unknown }).reason || "");
  }
  return "";
}

export default function CostBreakdown({ data }: { data: CostBreakdownData }) {
  const [compactState, setCompactState] = useState<
    "idle" | "working" | "done" | "error" | "noop"
  >("idle");
  const est = isFinite(data.est_cost_usd) ? data.est_cost_usd : 0;
  const estimated = spendIsEstimated(data);
  const billed = data.cost_source === "provider" && !estimated;
  const spendLabel = billed
    ? "Billed spend"
    : data.cost_source === "mixed"
      ? "Spend (mixed)"
      : data.cost_source === "plan_estimated"
        ? "Plan spend (est.)"
        : data.price_source === "default"
          ? "Estimated spend (default rates)"
          : data.price_source === "unknown"
            ? "Spend (rates unavailable)"
            : "Estimated spend";
  const spendPrefix = estimated ? "~" : "";
  const pilotCacheGross =
    typeof data.cache_savings_gross_usd === "number" && isFinite(data.cache_savings_gross_usd)
      ? data.cache_savings_gross_usd
      : typeof data.cache_savings_usd === "number" && isFinite(data.cache_savings_usd)
        ? data.cache_savings_usd
        : 0;
  const routingSaved = routingSavingsCredited(
    data.routing_savings_basis,
    data.routing_saved_usd,
  );
  const routingEstimated = data.routing_savings_basis === "estimated";
  const routingUnknown = data.routing_savings_basis === "unknown";
  const delegationSaved = delegationSavingsCredited(
    data.delegation_savings_basis,
    data.delegation_saved_usd,
  );
  const delegationMeasured = data.delegation_savings_basis === "actual_usage";
  // Keep routing and delegation separate: measured delegation (even $0)
  // owns the model-selection row; routing estimate must not replace it.
  const modelSelectionSaved = delegationMeasured
    ? delegationSaved
    : (delegationSaved > 0 ? delegationSaved : routingSaved);
  const showRoutingDecision =
    routingSaved > 0
    && (delegationMeasured || delegationSaved > 0)
    && Math.abs(routingSaved - delegationSaved) > 0.0001;
  const swarmCacheSaved =
    typeof data.cache_saved_usd_swarm === "number" && isFinite(data.cache_saved_usd_swarm) && data.cache_saved_usd_swarm > 0
      ? data.cache_saved_usd_swarm
      : 0;
  const swarmCacheUnpricedTokens =
    typeof data.swarm_cache_unpriced_tokens === "number"
    && isFinite(data.swarm_cache_unpriced_tokens)
    && data.swarm_cache_unpriced_tokens > 0
      ? data.swarm_cache_unpriced_tokens
      : 0;
  const swarmCachePartial =
    swarmCacheSaved > 0
    && (
      data.swarm_cache_savings_basis === "unknown"
      || swarmCacheUnpricedTokens > 0
    );
  // One Prompt-cache value row: uncapped pilot gross + store-job cache.
  const promptCacheSaved =
    (pilotCacheGross > 0 ? pilotCacheGross : 0) + swarmCacheSaved;
  const compactSavings =
    typeof data.tool_output_savings_usd === "number" && isFinite(data.tool_output_savings_usd) && data.tool_output_savings_usd > 0
      ? data.tool_output_savings_usd
      : 0;
  const valueTotal = listPriceValueTotal(data);
  const valueBasis = listPriceValueWeakestBasis(data);
  const valueHeading = listPriceValueHeading(valueBasis);
  const compactTokens =
    typeof data.tool_output_tokens_saved === "number" && isFinite(data.tool_output_tokens_saved) && data.tool_output_tokens_saved > 0
      ? data.tool_output_tokens_saved
      : 0;
  const historyCompactions =
    typeof data.history_compactions === "number" && isFinite(data.history_compactions) && data.history_compactions > 0
      ? data.history_compactions
      : 0;
  const historyTokensSaved =
    typeof data.history_tokens_saved === "number" && isFinite(data.history_tokens_saved) && data.history_tokens_saved > 0
      ? data.history_tokens_saved
      : 0;
  const historyCacheBust =
    typeof data.history_cache_bust_tokens === "number" && isFinite(data.history_cache_bust_tokens) && data.history_cache_bust_tokens > 0
      ? data.history_cache_bust_tokens
      : 0;
  const historyThrash =
    typeof data.history_thrash_events === "number" && isFinite(data.history_thrash_events) && data.history_thrash_events > 0
      ? data.history_thrash_events
      : 0;
  // USD only when the journal measured it — never infer from tokens.
  const historyCostUsd =
    typeof data.history_compaction_cost_usd === "number" && isFinite(data.history_compaction_cost_usd) && data.history_compaction_cost_usd > 0
      ? data.history_compaction_cost_usd
      : null;
  const standingFloorCost =
    data.standing_economics_basis === "estimated"
    && typeof data.standing_floor_cost_usd === "number"
    && isFinite(data.standing_floor_cost_usd)
    && data.standing_floor_cost_usd > 0
      ? data.standing_floor_cost_usd
      : 0;
  const standingFloorCached =
    data.standing_economics_basis === "estimated"
    && data.prompt_cache_state !== "expired"
    && typeof data.standing_floor_cost_cached_usd === "number"
    && isFinite(data.standing_floor_cost_cached_usd)
    && data.standing_floor_cost_cached_usd > 0
      ? data.standing_floor_cost_cached_usd
      : 0;
  const standingFloorTokens =
    typeof data.standing_floor_tokens === "number" && isFinite(data.standing_floor_tokens) && data.standing_floor_tokens > 0
      ? data.standing_floor_tokens
      : 0;
  const cacheTtlMs =
    typeof data.prompt_cache_ttl_ms === "number" && isFinite(data.prompt_cache_ttl_ms) && data.prompt_cache_ttl_ms > 0
      ? data.prompt_cache_ttl_ms
      : 0;
  const cacheExpiresInMs =
    typeof data.prompt_cache_expires_in_ms === "number" && isFinite(data.prompt_cache_expires_in_ms)
      ? data.prompt_cache_expires_in_ms
      : null;
  const cacheState = data.prompt_cache_state;
  const spillCount =
    typeof data.spill_count === "number" && isFinite(data.spill_count) && data.spill_count > 0
      ? data.spill_count
      : 0;
  const evalsRecorded =
    typeof data.evals_recorded === "number" && isFinite(data.evals_recorded) && data.evals_recorded > 0
      ? data.evals_recorded
      : 0;
  const evalsFailed =
    typeof data.evals_failed === "number" && isFinite(data.evals_failed) && data.evals_failed > 0
      ? data.evals_failed
      : 0;
  const spillChars =
    typeof data.spill_chars === "number" && isFinite(data.spill_chars) && data.spill_chars > 0
      ? data.spill_chars
      : 0;
  const cached =
    typeof data.tokens_cached === "number" && isFinite(data.tokens_cached) && data.tokens_cached > 0
      ? data.tokens_cached
      : 0;
  const hit = cacheHitDisplay(data);
  const pilotCached =
    typeof data.pilot_cache_read_tokens === "number" && isFinite(data.pilot_cache_read_tokens)
      ? data.pilot_cache_read_tokens
      : 0;
  const swarmCached =
    typeof data.swarm_cache_read_tokens === "number" && isFinite(data.swarm_cache_read_tokens)
      ? data.swarm_cache_read_tokens
      : 0;
  const l1Bytes =
    typeof data.memory_layers?.L1?.bytes === "number" && isFinite(data.memory_layers.L1.bytes)
      ? data.memory_layers.L1.bytes
      : 0;
  const compactionAdviceLevel = data.compaction_advice?.level;
  const needsIntervention =
    data.compaction_advice?.needs_intervention === true ||
    compactionAdviceLevel === "soon" ||
    compactionAdviceLevel === "now";
  const showCompactionAdvice = needsIntervention;
  const compactionAdviceReason =
    showCompactionAdvice
      ? (data.compaction_advice?.warning_reason ||
          (Array.isArray(data.compaction_advice?.reasons) && data.compaction_advice.reasons.length > 0
            ? data.compaction_advice.reasons[0]
            : "") ||
          (data.history_compaction_ran ? "history compaction ran under context pressure" : ""))
      : "";
  const adviceCopy = compactionAdvicePresentation(compactionAdviceLevel);
  const showContextHealth =
    historyCompactions > 0
    || standingFloorCost > 0
    || (cacheTtlMs > 0 && Boolean(cacheState))
    || spillCount > 0
    || evalsRecorded > 0
    || l1Bytes > 0
    || showCompactionAdvice;

  const layerLabel = (id: string) => {
    const layer = data.memory_layers?.[id];
    const bytes = typeof layer?.bytes === "number" && isFinite(layer.bytes) ? layer.bytes : 0;
    return `${id} ${fmtBytes(bytes)}`;
  };

  const onCompactNow = () => {
    if (compactState === "working") return;
    setCompactState("working");
    api
      .compactSession()
      .then((res) => {
        // Only celebrate a REAL reduction: the backend sets compacted=true
        // when a compaction event fired; older backends are checked by token
        // delta. Structured no-ops get calm copy; other failures stay retryable.
        const trulyReduced =
          res?.ok === true &&
          (res.compacted === true ||
            (res.compacted === undefined &&
              isFinite(res.before_tokens) &&
              isFinite(res.after_tokens) &&
              res.after_tokens < res.before_tokens));
        if (!trulyReduced) {
          if (res?.reason === "no_compactable_history") {
            setCompactState("noop");
            // Refresh meters so Needs attention clears after the manual ack latch.
            window.dispatchEvent(new Event("harness-usage-refresh"));
            return;
          }
          setCompactState("error");
          return;
        }
        setCompactState("done");
        window.dispatchEvent(new Event("harness-usage-refresh"));
      })
      .catch((err) => {
        if (compactFailureReason(err) === "no_compactable_history") {
          setCompactState("noop");
          window.dispatchEvent(new Event("harness-usage-refresh"));
          return;
        }
        setCompactState("error");
      });
  };

  return (
    <div className="w-full min-h-0 overflow-auto px-3 py-3 text-[11px] text-txt">
      <div className="text-[10px] uppercase tracking-wide text-faint">This app run</div>
      <p className="text-[10px] text-muted mb-2 leading-snug">
        Process spend since launch. Resets on full quit — not Swarm pane repo-session spend, not conversation lifetime.
      </p>

      {/* (a) App-run spend. Provider-billed when OpenRouter (etc.) returned usage.cost. */}
      {est > 0 ? (
        <div className="flex items-center justify-between mb-1">
          <span className="text-muted">{spendLabel}</span>
          <span className="text-good font-medium tabular-nums">{spendPrefix}{fmtCost(est)}</span>
        </div>
      ) : null}

      {Array.isArray(data.pilot_by_model)
        ? data.pilot_by_model
            .filter((row) => typeof row?.est_cost_usd === "number" && isFinite(row.est_cost_usd) && row.est_cost_usd > 0)
            .map((row) => (
              <div
                key={row.model}
                className="flex items-center justify-between mb-1 pl-2 text-faint"
                title={`Locked to ${row.model} — swapping the pilot does not reprice this row.`}
              >
                <span className="truncate pr-2">{shortPilotModel(row.model)}</span>
                <span className="tabular-nums shrink-0">
                  {spendPrefix}{fmtCost(row.est_cost_usd)}
                </span>
              </div>
            ))
        : null}

      {/* (b) Prompt-cache value -- uncapped pilot gross + swarm store. */}
      {promptCacheSaved > 0 ? (
        <div
          className="flex items-center justify-between mb-1"
          title={
            swarmCachePartial
              ? swarmCacheUnpricedTokens > 0
                ? `Partial avoided full-price input value; ${fmtTokens(swarmCacheUnpricedTokens)} swarm cache tokens could not be priced. Not a cash refund.`
                : "Partial avoided full-price input value; not every swarm cache hit has a trustworthy list rate. Not a cash refund."
              : "Gross avoided full-price input value from prompt-cache hits (catalog/list rate). Continues growing with cached tokens; not a cash refund and not capped to provider spend."
          }
        >
          <span className="text-muted">
            Prompt-cache value{swarmCachePartial ? " (partial)" : ""}
          </span>
          <span className="text-accent font-medium tabular-nums">~{fmtCost(promptCacheSaved)}</span>
        </div>
      ) : null}

      {modelSelectionSaved > 0 ? (
        <div
          className="flex items-center justify-between mb-1"
          title={
            delegationSaved > 0
              ? "Full list-price value of choosing cheaper worker models vs a frontier-equivalent baseline on the same actual tokens (ignores prompt-cache discounts). Not a cash refund."
              : routingEstimated
                ? "Running estimate vs frontier-equivalent list price (preflight). Not a cash refund or billed spend."
                : "List-price value vs a frontier-equivalent baseline on the same actual tokens. Not a cash refund."
          }
        >
          <span className="text-muted">
            Model selection value{routingEstimated && delegationSaved <= 0 ? " (est.)" : ""}
          </span>
          <span className="text-accent font-medium tabular-nums">~{fmtCost(modelSelectionSaved)}</span>
        </div>
      ) : null}

      {showRoutingDecision ? (
        <div
          className="flex items-center justify-between mb-1 text-faint"
          title="Narrow router-decision delta (balanced/cheap policies only; includes prompt-cache discount in counterfactual). Shown separately from model-selection value. Not billed spend."
        >
          <span>Routing decision value{routingEstimated ? " (est.)" : ""}</span>
          <span className="tabular-nums">~{fmtCost(routingSaved)}</span>
        </div>
      ) : null}

      {routingUnknown && typeof data.routing_saved_usd === "number" && data.routing_saved_usd > 0 ? (
        <div
          className="flex items-center justify-between mb-1 text-faint"
          title="Routing savings basis is unknown — refused as billed or measured value."
        >
          <span>Routing decision value</span>
          <span className="tabular-nums">unknown basis</span>
        </div>
      ) : null}

      {cached > 0 || hit.percent != null ? (
        <div
          className="flex items-center justify-between mb-1 text-faint"
          title={hit.title}
        >
          <span>
            {hit.percent != null ? `${hit.label} hit` : "Tokens from cache"}
          </span>
          <span className="tabular-nums text-right">
            {hit.percent != null ? hit.percent : ""}
            {hit.percent != null && cached > 0 ? " · " : ""}
            {cached > 0 ? `${fmtTokens(cached)} read` : ""}
            {pilotCached > 0 && swarmCached > 0
              ? ` (pilot ${fmtTokens(pilotCached)} · swarm ${fmtTokens(swarmCached)})`
              : ""}
          </span>
        </div>
      ) : null}

      {compactSavings > 0 ? (
        <div className="flex items-center justify-between mb-1">
          <span className="text-muted">Compact tool outputs saved</span>
          <span className="text-accent font-medium tabular-nums">~{fmtCost(compactSavings)}</span>
        </div>
      ) : null}

      {valueTotal > 0 || valueBasis === "unknown" ? (
        <div
          className="mt-1.5 pt-1.5 border-t border-edge/50 flex items-center justify-between font-medium"
          title="Prompt-cache value + model-selection value + compact-output value. Additive list-price value, not a cash refund or billed-spend subtraction. Label follows the weakest included evidence basis."
        >
          <span className="text-txt">{valueHeading}</span>
          <span className="text-good tabular-nums">
            {valueTotal > 0 ? `~${fmtCost(valueTotal)}` : (valueBasis === "unknown" ? "unknown basis" : "—")}
          </span>
        </div>
      ) : null}

      {compactTokens > 0 ? (
        <div className="flex items-center justify-between mb-1 text-faint">
          <span>Tool-output tokens avoided</span>
          <span className="tabular-nums">{fmtTokens(compactTokens)}</span>
        </div>
      ) : null}

      {showContextHealth ? (
      <div className="mt-3 pt-2 border-t border-edge/60">
        <div className="text-[10px] uppercase tracking-wide text-faint mb-2">Context health</div>
      {historyCompactions > 0 ? (
        <div
          className="flex items-center justify-between mb-1 text-faint"
          title="Tokens avoided by history compaction. USD shown only when the summarizer cost was measured — never inferred from tokens."
        >
          <span>History compaction</span>
          <span className="tabular-nums text-right">
            {fmtTokens(historyTokensSaved)} saved ({historyCompactions} event{historyCompactions === 1 ? "" : "s"})
            {historyCostUsd != null ? ` · ${fmtCost(historyCostUsd)} measured` : ""}
            {historyCacheBust > 0 ? ` · ${fmtTokens(historyCacheBust)} cache bust` : ""}
            {historyThrash > 0 ? ` · ${historyThrash} thrash` : ""}
          </span>
        </div>
      ) : null}

      {standingFloorCost > 0 ? (
        <div
          className="flex items-center justify-between mb-1 text-faint"
          title="Estimated per-turn cost of the standing system+tools prefix at current list rates. Not billed spend and not part of list-price value."
        >
          <span>Standing context floor (est.)</span>
          <span className="tabular-nums">
            ~{fmtCost(standingFloorCost)}
            {standingFloorTokens > 0 ? ` · ${fmtTokens(standingFloorTokens)} tok` : ""}
            {standingFloorCached > 0 ? ` · ~${fmtCost(standingFloorCached)} cached` : ""}
          </span>
        </div>
      ) : null}

      {cacheTtlMs > 0 && cacheState ? (
        <div
          className="flex items-center justify-between mb-1 text-faint"
          title={
            cacheState === "expired"
              ? "Prompt-cache TTL elapsed — no cache value claimed after expiry."
              : "Estimated prompt-cache TTL remaining based on last activity (not a guarantee)."
          }
        >
          <span>Prompt-cache TTL (est.)</span>
          <span className="tabular-nums">
            {cacheState === "expired"
              ? "expired"
              : cacheExpiresInMs != null
                ? `warm · ~${fmtDurationMs(cacheExpiresInMs)} left`
                : "warm"}
          </span>
        </div>
      ) : null}

      {spillCount > 0 ? (
        <div className="flex items-center justify-between mb-1 text-faint">
          <span>Offloaded outputs</span>
          <span className="tabular-nums">{fmtTokens(spillChars)} chars ({spillCount} spill{spillCount === 1 ? "" : "s"})</span>
        </div>
      ) : null}

      {evalsRecorded > 0 ? (
        <div className="flex items-center justify-between mb-1 text-faint">
          <span>Checks recorded</span>
          <span className="tabular-nums">{evalsRecorded} ({evalsFailed} failed)</span>
        </div>
      ) : null}

      {l1Bytes > 0 ? (
        <div className="flex items-center justify-between mb-1 text-faint">
          <span>Memory layers</span>
          <span className="tabular-nums text-right">
            {layerLabel("L0")} | {layerLabel("L1")} | {layerLabel("L2")} | {layerLabel("L3")}
          </span>
        </div>
      ) : null}

      {showCompactionAdvice ? (
        <div
          className="mb-1 rounded px-1.5 py-1.5 -mx-0.5 bg-amber-500/10 border border-amber-500/25 text-amber-200/90"
          role="status"
          title={compactionAdviceReason || "Context pressure needs attention"}
        >
          <div className="flex items-center justify-between gap-2 mb-1">
            <span className="font-medium">
              {compactState === "noop" ? "Already compact" : adviceCopy.label}
            </span>
            {adviceCopy.showCompactAction && compactState !== "noop" ? (
              <button
                type="button"
                onClick={onCompactNow}
                disabled={compactState === "working"}
                className="shrink-0 rounded border border-amber-500/40 bg-amber-500/15 px-1.5 py-0.5 text-[10px] font-medium text-amber-100 hover:bg-amber-500/25 disabled:opacity-60"
              >
                {compactState === "working"
                  ? "Compacting..."
                  : compactState === "done"
                    ? "Compacted"
                    : compactState === "error"
                      ? "Retry compact"
                      : "Compact now"}
              </button>
            ) : null}
          </div>
          <p className="leading-snug text-amber-100/80 m-0">
            {compactState === "noop"
              ? "Recent turn is already compact."
              : adviceCopy.message}
          </p>
        </div>
      ) : null}
      </div>
      ) : null}

      {/* (c) Additive value framing — routing list-price + cache + compact
          are separate mechanisms (not overlapping cash refunds). */}
      <div className="mt-2 pt-2 border-t border-edge/60 text-[10px] leading-snug text-muted/90">
        {promptCacheSaved > 0 || compactSavings > 0 || modelSelectionSaved > 0 ? (
          <span>
            Routed per-step to the cheapest capable model
            {modelSelectionSaved > 0 ? (
              <>
                , with{" "}
                <span className="text-accent">~{fmtCost(modelSelectionSaved)}</span> model
                selection value vs frontier-equivalent list price
                {routingEstimated && delegationSaved <= 0 ? " (estimate)" : ""}
              </>
            ) : null}
            {promptCacheSaved > 0 ? (
              <>
                , plus <span className="text-accent">~{fmtCost(promptCacheSaved)}</span>{" "}
                prompt-cache value
              </>
            ) : null}
            {compactSavings > 0 ? (
              <>
                , and <span className="text-accent">~{fmtCost(compactSavings)}</span>{" "}
                avoided by compact tool outputs
              </>
            ) : null}
            .
          </span>
        ) : (
          <span>
            Each task step is routed to the cheapest capable model instead of a
            single frontier-equivalent list-price baseline.
          </span>
        )}
      </div>
    </div>
  );
}
