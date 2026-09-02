// CostBreakdown -- the Economics pane body (process / this-app-run meters).
//
// It turns Marionette's per-task model routing into a visible value prop:
// "why this model / what it saved". It consumes ONLY fields already served by
// /api/usage (est_cost_usd, cache_savings_usd, price_in, price_out,
// tokens_used, tokens_cached) and degrades gracefully -- any field that is
// absent or zero simply renders nothing rather than "$0.000000" noise or NaN.

import type { UsageData } from "../lib/api";

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

// Local formatter so this subcomponent stays self-contained. Mirrors the
// StatusBar cost formatting (coarser as the number grows) but never emits a
// bare "$0.00" for a value that is meaningfully zero -- callers gate on that.
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

export default function CostBreakdown({
  data,
  hero = true,
}: {
  data: CostBreakdownData;
  hero?: boolean;
}) {
  const est = isFinite(data.est_cost_usd) ? data.est_cost_usd : 0;
  const estimated = spendIsEstimated(data);
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
  const promptCacheSaved =
    (pilotCacheGross > 0 ? pilotCacheGross : 0) + swarmCacheSaved;
  const compactSavings =
    typeof data.tool_output_savings_usd === "number" && isFinite(data.tool_output_savings_usd) && data.tool_output_savings_usd > 0
      ? data.tool_output_savings_usd
      : 0;
  const compactTokens =
    typeof data.tool_output_tokens_saved === "number" && isFinite(data.tool_output_tokens_saved) && data.tool_output_tokens_saved > 0
      ? data.tool_output_tokens_saved
      : 0;
  const valueTotal = listPriceValueTotal(data);
  const withoutSavings = est + valueTotal;
  const savingsPercent = withoutSavings > 0
    ? Math.max(0, Math.min(100, (valueTotal / withoutSavings) * 100))
    : null;
  const showWhySaved = promptCacheSaved > 0
    || modelSelectionSaved > 0
    || showRoutingDecision
    || compactSavings > 0;
  const showUnknownRouting = routingUnknown
    && typeof data.routing_saved_usd === "number"
    && data.routing_saved_usd > 0;
  if (!hero && !showWhySaved && !showUnknownRouting) return null;

  return (
    <div className="w-full min-h-0 px-3 py-3 text-[11px] text-txt">
      <p className="text-[10px] text-muted mb-2 leading-snug">
        Spend and savings since you opened Marionette.
      </p>
      {hero ? (
      <div className="mb-3 rounded-md border border-edge/50 bg-panel2/20 px-2.5 py-2.5">
        <div className="grid grid-cols-2 gap-x-3 gap-y-2.5">
          <div className="min-w-0">
            <div className="text-[10px] text-muted">Spend</div>
            <div className="mt-0.5 text-[15px] font-medium tabular-nums text-txt">{spendPrefix}{fmtCost(est)}</div>
          </div>
          <div className="min-w-0">
            <div className="text-[10px] text-muted">Without savings</div>
            <div className="mt-0.5 text-[15px] font-medium tabular-nums text-txt">~{fmtCost(withoutSavings)}</div>
          </div>
          <div className="min-w-0">
            <div className="text-[10px] text-muted">Estimated savings</div>
            <div className="mt-0.5 text-[15px] font-medium tabular-nums text-good/65">~{fmtCost(valueTotal)}</div>
          </div>
          <div className="min-w-0">
            <div className="text-[10px] text-muted">Less spent</div>
            <div className="mt-0.5 text-[15px] font-medium tabular-nums text-good/65">
              {savingsPercent === null ? "—" : `${savingsPercent.toFixed(1)}%`}
            </div>
          </div>
        </div>
      </div>
      ) : null}

      {showWhySaved ? (
      <div className={hero ? "mt-2 pt-2 border-t border-edge/50" : undefined}>
      <div className="text-[10px] text-faint mb-1">Why you saved</div>
      <p className="text-[10px] text-muted mb-1.5 leading-snug">
        Each saving is shown once, so the total stays honest.
      </p>
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
          <span className="tabular-nums text-good/65">~{fmtCost(promptCacheSaved)}</span>
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
          <span className="tabular-nums text-good/65">~{fmtCost(modelSelectionSaved)}</span>
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

      {compactSavings > 0 ? (
        <div className="flex items-center justify-between mb-1">
          <span className="text-muted">Compact tool outputs</span>
          <span className="tabular-nums text-good/65">
            {compactTokens > 0 ? `${fmtTokens(compactTokens)} tok · ` : ""}~{fmtCost(compactSavings)}
          </span>
        </div>
      ) : null}
      </div>
      ) : null}

      {showUnknownRouting ? (
        <div
          className="flex items-center justify-between mb-1 text-faint"
          title="Routing savings basis is unknown — refused as billed or measured value."
        >
          <span>Routing decision value</span>
          <span className="tabular-nums">unknown basis</span>
        </div>
      ) : null}
    </div>
  );
}
