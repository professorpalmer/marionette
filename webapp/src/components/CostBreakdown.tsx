// CostBreakdown -- a compact, presentational cost popover for the StatusBar.
//
// It turns Marionette's per-task model routing into a visible value prop:
// "why this model / what it saved". It consumes ONLY fields already served by
// /api/usage (est_cost_usd, cache_savings_usd, price_in, price_out,
// tokens_used, tokens_cached) and degrades gracefully -- any field that is
// absent or zero simply renders nothing rather than "$0.000000" noise or NaN.

import { useState } from "react";
import { api } from "../lib/api";

export type CostBreakdownData = {
  tokens_used: number;
  est_cost_usd: number;
  cost_source?: "provider" | "estimated" | "mixed" | "plan_estimated";
  /** live | static | default — how display rates were resolved. */
  price_source?: "live" | "static" | "default";
  /** True when spend is not a full provider receipt. */
  estimated?: boolean;
  tokens_cached?: number;
  cache_savings_usd?: number;
  /** catalog | capped | unknown — how cache savings were attributed. */
  cache_savings_basis?: "catalog" | "capped" | "unknown";
  routing_saved_usd?: number;
  cache_saved_usd_swarm?: number;
  tool_output_tokens_saved?: number;
  tool_output_savings_usd?: number;
  history_compactions?: number;
  history_tokens_saved?: number;
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
  price_in?: number;
  price_out?: number;
};

/** Compact spend is estimated unless a full provider receipt backs it. */
export function spendIsEstimated(data: Pick<CostBreakdownData, "cost_source" | "estimated" | "price_source">): boolean {
  if (typeof data.estimated === "boolean") return data.estimated;
  if (data.cost_source === "provider") return false;
  if (data.price_source === "default") return true;
  return true;
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

export default function CostBreakdown({ data }: { data: CostBreakdownData }) {
  const [compactState, setCompactState] = useState<"idle" | "working" | "done" | "error">("idle");
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
          : "Estimated spend";
  const spendPrefix = estimated ? "~" : "";
  const cacheUnknown = data.cache_savings_basis === "unknown";
  const cacheSavings =
    !cacheUnknown
    && typeof data.cache_savings_usd === "number"
    && isFinite(data.cache_savings_usd)
    && data.cache_savings_usd > 0
      ? data.cache_savings_usd
      : 0;
  const routingSaved =
    typeof data.routing_saved_usd === "number" && isFinite(data.routing_saved_usd) && data.routing_saved_usd > 0
      ? data.routing_saved_usd
      : 0;
  const swarmCacheSaved =
    typeof data.cache_saved_usd_swarm === "number" && isFinite(data.cache_saved_usd_swarm) && data.cache_saved_usd_swarm > 0
      ? data.cache_saved_usd_swarm
      : 0;
  // One Prompt-cache saved row: pilot-priced cache + authoritative swarm store.
  const promptCacheSaved = cacheSavings + swarmCacheSaved;
  const compactSavings =
    typeof data.tool_output_savings_usd === "number" && isFinite(data.tool_output_savings_usd) && data.tool_output_savings_usd > 0
      ? data.tool_output_savings_usd
      : 0;
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
        // delta. Anything else is a no-op the user should be able to retry.
        const trulyReduced =
          res?.ok === true &&
          (res.compacted === true ||
            (res.compacted === undefined &&
              isFinite(res.before_tokens) &&
              isFinite(res.after_tokens) &&
              res.after_tokens < res.before_tokens));
        if (!trulyReduced) {
          setCompactState("error");
          return;
        }
        setCompactState("done");
        window.dispatchEvent(new Event("harness-usage-refresh"));
      })
      .catch(() => setCompactState("error"));
  };

  return (
    <div className="w-[260px] rounded-md border border-edge bg-panel shadow-lg p-3 text-[11px] text-txt">
      <div className="text-[10px] uppercase tracking-wide text-faint mb-2">Session cost</div>

      {/* (a) Session spend. Provider-billed when OpenRouter (etc.) returned usage.cost. */}
      {est > 0 ? (
        <div className="flex items-center justify-between mb-1">
          <span className="text-muted">{spendLabel}</span>
          <span className="text-good font-medium tabular-nums">{spendPrefix}{fmtCost(est)}</span>
        </div>
      ) : null}

      {/* (b) Cache savings -- pilot + swarm store, one row. */}
      {promptCacheSaved > 0 ? (
        <div className="flex items-center justify-between mb-1">
          <span className="text-muted">
            Prompt-cache saved
            {data.cache_savings_basis === "capped" ? " (capped)" : ""}
          </span>
          <span className="text-accent font-medium tabular-nums">~{fmtCost(promptCacheSaved)}</span>
        </div>
      ) : null}
      {cacheUnknown ? (
        <div className="flex items-center justify-between mb-1 text-faint">
          <span>Prompt-cache saved</span>
          <span className="tabular-nums">unknown (net provider)</span>
        </div>
      ) : null}

      {routingSaved > 0 ? (
        <div className="flex items-center justify-between mb-1">
          <span className="text-muted">Routing saved</span>
          <span className="text-accent font-medium tabular-nums">~{fmtCost(routingSaved)}</span>
        </div>
      ) : null}

      {cached > 0 ? (
        <div className="flex items-center justify-between mb-1 text-faint">
          <span>Tokens from cache</span>
          <span className="tabular-nums">{fmtTokens(cached)}</span>
        </div>
      ) : null}

      {compactSavings > 0 ? (
        <div className="flex items-center justify-between mb-1">
          <span className="text-muted">Compact tool outputs saved</span>
          <span className="text-accent font-medium tabular-nums">~{fmtCost(compactSavings)}</span>
        </div>
      ) : null}

      {compactTokens > 0 ? (
        <div className="flex items-center justify-between mb-1 text-faint">
          <span>Tool-output tokens avoided</span>
          <span className="tabular-nums">{fmtTokens(compactTokens)}</span>
        </div>
      ) : null}

      {historyCompactions > 0 ? (
        <div className="flex items-center justify-between mb-1 text-faint">
          <span>History compaction</span>
          <span className="tabular-nums">{fmtTokens(historyTokensSaved)} saved ({historyCompactions} event{historyCompactions === 1 ? "" : "s"})</span>
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
            <span className="font-medium">{adviceCopy.label}</span>
            {adviceCopy.showCompactAction ? (
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
          <p className="leading-snug text-amber-100/80 m-0">{adviceCopy.message}</p>
        </div>
      ) : null}

      {/* (c) The routing value proposition. Cache savings are concrete dollars
          the router-plus-cache path already banked; the framing line explains
          the mechanism that keeps spend low even absent a flat-frontier
          baseline. Kept to one short line. */}
      <div className="mt-2 pt-2 border-t border-edge/60 text-[10px] leading-snug text-muted/90">
        {promptCacheSaved > 0 || compactSavings > 0 || routingSaved > 0 ? (
          <span>
            Routed per-step to the cheapest capable model
            {routingSaved > 0 ? (
              <>, saving <span className="text-accent">~{fmtCost(routingSaved)}</span> vs a flat-frontier baseline</>
            ) : null}
            {promptCacheSaved > 0 ? (
              <>, with <span className="text-accent">~{fmtCost(promptCacheSaved)}</span> saved via prompt caching</>
            ) : null}
            {compactSavings > 0 ? (
              <>, and <span className="text-accent">~{fmtCost(compactSavings)}</span> avoided by compact tool outputs</>
            ) : null}
            .
          </span>
        ) : (
          <span>Each task step is routed to the cheapest capable model instead of a single flat-frontier model.</span>
        )}
      </div>
    </div>
  );
}
