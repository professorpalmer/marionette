// CostBreakdown -- a compact, presentational cost popover for the StatusBar.
//
// It turns Marionette's per-task model routing into a visible value prop:
// "why this model / what it saved". It consumes ONLY fields already served by
// /api/usage (est_cost_usd, cache_savings_usd, price_in, price_out,
// tokens_used, tokens_cached) and degrades gracefully -- any field that is
// absent or zero simply renders nothing rather than "$0.000000" noise or NaN.

export type CostBreakdownData = {
  tokens_used: number;
  est_cost_usd: number;
  tokens_cached?: number;
  cache_savings_usd?: number;
  tool_output_tokens_saved?: number;
  tool_output_savings_usd?: number;
  history_compactions?: number;
  history_tokens_saved?: number;
  spill_count?: number;
  spill_chars?: number;
  evals_recorded?: number;
  evals_failed?: number;
  memory_layers?: Record<string, { bytes?: number; entries?: number }>;
  price_in?: number;
  price_out?: number;
};

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
  const est = isFinite(data.est_cost_usd) ? data.est_cost_usd : 0;
  const cacheSavings =
    typeof data.cache_savings_usd === "number" && isFinite(data.cache_savings_usd) && data.cache_savings_usd > 0
      ? data.cache_savings_usd
      : 0;
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

  const layerLabel = (id: string) => {
    const layer = data.memory_layers?.[id];
    const bytes = typeof layer?.bytes === "number" && isFinite(layer.bytes) ? layer.bytes : 0;
    return `${id} ${fmtBytes(bytes)}`;
  };

  return (
    <div className="w-[260px] rounded-md border border-edge bg-panel shadow-lg p-3 text-[11px] text-txt">
      <div className="text-[10px] uppercase tracking-wide text-faint mb-2">Session cost</div>

      {/* (a) Session estimated cost. Always shown when a positive figure exists. */}
      {est > 0 ? (
        <div className="flex items-center justify-between mb-1">
          <span className="text-muted">Estimated spend</span>
          <span className="text-good font-medium tabular-nums">~{fmtCost(est)}</span>
        </div>
      ) : null}

      {/* (b) Cache savings -- only when there is a real, positive dollar figure. */}
      {cacheSavings > 0 ? (
        <div className="flex items-center justify-between mb-1">
          <span className="text-muted">Prompt-cache saved</span>
          <span className="text-accent font-medium tabular-nums">~{fmtCost(cacheSavings)}</span>
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

      {/* (c) The routing value proposition. Cache savings are concrete dollars
          the router-plus-cache path already banked; the framing line explains
          the mechanism that keeps spend low even absent a flat-frontier
          baseline. Kept to one short line. */}
      <div className="mt-2 pt-2 border-t border-edge/60 text-[10px] leading-snug text-muted/90">
        {cacheSavings > 0 || compactSavings > 0 ? (
          <span>
            Routed per-step to the cheapest capable model
            {cacheSavings > 0 ? (
              <>, with <span className="text-accent">~{fmtCost(cacheSavings)}</span> saved via prompt caching</>
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
