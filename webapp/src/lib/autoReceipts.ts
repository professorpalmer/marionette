/**
 * Calm full-auto operator receipts — StatusBar / CostBreakdown quiet copy.
 *
 * These helpers never imply a command ran, a compaction occurred, or an
 * objective succeeded unless the halt reason itself says so.
 */

export type AutoBudgetSnapshot = {
  tokens_used?: number;
  max_tokens?: number;
  swarms_used?: number;
  max_swarms?: number;
  elapsed_s?: number;
  max_seconds?: number;
  idle_steps?: number;
  max_idle_steps?: number;
  halted?: string | null;
};

function formatTokens(num: number): string {
  if (!Number.isFinite(num) || num < 0) return "0";
  if (num >= 1_000_000) return (num / 1_000_000).toFixed(1).replace(/\.0$/, "") + "M";
  if (num >= 1000) return (num / 1000).toFixed(1).replace(/\.0$/, "") + "k";
  return String(Math.round(num));
}

/** Compact AutoBudget meters for a quiet chip (e.g. "2/20 swarms · 4.1k/50k tok · 45s"). */
export function formatAutoBudgetMeters(
  snapshot?: AutoBudgetSnapshot | null,
): string {
  if (!snapshot || typeof snapshot !== "object") return "";
  const parts: string[] = [];
  if (
    typeof snapshot.swarms_used === "number"
    && Number.isFinite(snapshot.swarms_used)
    && typeof snapshot.max_swarms === "number"
    && Number.isFinite(snapshot.max_swarms)
  ) {
    parts.push(`${snapshot.swarms_used}/${snapshot.max_swarms} swarms`);
  }
  if (typeof snapshot.tokens_used === "number" && Number.isFinite(snapshot.tokens_used)) {
    const used = formatTokens(snapshot.tokens_used);
    const max =
      typeof snapshot.max_tokens === "number" && Number.isFinite(snapshot.max_tokens)
        ? `/${formatTokens(snapshot.max_tokens)}`
        : "";
    parts.push(`${used}${max} tok`);
  }
  if (typeof snapshot.elapsed_s === "number" && Number.isFinite(snapshot.elapsed_s)) {
    parts.push(`${Math.max(0, Math.round(snapshot.elapsed_s))}s`);
  }
  return parts.join(" · ");
}

export function autoStatusPresentation(
  cycle: number,
  snapshot?: AutoBudgetSnapshot | null,
): { label: string; detail: string } {
  const n = Number.isFinite(cycle) ? Math.max(0, Math.round(cycle)) : 0;
  const meters = formatAutoBudgetMeters(snapshot);
  return {
    label: `Full-auto · cycle ${n}`,
    // Budget progress only — never "done", "compacted", or "executed".
    detail: meters,
  };
}

export function autoHaltPresentation(
  reason: string,
  snapshot?: AutoBudgetSnapshot | null,
): { label: string; detail: string; metObjective: boolean } {
  const raw = String(reason || "").trim();
  const metObjective = /objective met/i.test(raw);
  const cancelled = /\bcancel(?:led|ed)?\b/i.test(raw);
  const budgetTripped = /ceiling|stall|killswitch|token|swarm|idle|seconds/i.test(raw);

  let label = "Full-auto stopped";
  if (metObjective) label = "Full-auto finished";
  else if (cancelled) label = "Full-auto cancelled";
  else if (budgetTripped) label = "Full-auto halted";

  const detail = raw || "Budget or policy ended the run";
  const meters = formatAutoBudgetMeters(snapshot);
  return {
    label,
    detail: meters ? `${detail} · ${meters}` : detail,
    metObjective,
  };
}

export function commandBlockedPresentation(d: {
  reason?: string;
  category?: string;
}): { label: string; detail: string } {
  return {
    label: "Command not run",
    detail:
      (d.reason || "").trim()
      || (d.category || "").trim()
      || "Full-auto safety policy blocked this command",
  };
}

/** Truthful post-decision copy — never claims the shell command executed. */
export function commandApprovalStatusCopy(
  status: "pending" | "approving" | "approved" | "rejected" | "error",
): string {
  switch (status) {
    case "approving":
      return "Applying decision…";
    case "approved":
      return "Approved once — not run yet; retry queued.";
    case "rejected":
      return "Rejected — command was not run.";
    case "error":
      return "Decision failed — command remains blocked.";
    default:
      return "";
  }
}
