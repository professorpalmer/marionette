/** Segment colors + display/validation helpers for the composer context-usage panel. */
import type { ContextCategory, ContextUsageResponse } from "../../lib/api";

export const CONTEXT_USAGE_COLORS = [
  "bg-blue-500", // System prompt
  "bg-emerald-500", // Tool definitions
  "bg-purple-500", // Rules
  "bg-amber-500", // Skills
  "bg-teal-500", // MCP
  "bg-rose-500", // Subagent
  "bg-pink-500", // Summarized conversation
  "bg-indigo-500", // Conversation
] as const;

export function contextUsagePercent(total: number, limit: number): number {
  if (!Number.isFinite(total) || !Number.isFinite(limit) || limit <= 0) return 0;
  return Math.max(0, Math.min(100, Math.round((total / limit) * 100)));
}

export function formatTokenK(tokens: number, digits = 1): string {
  if (!Number.isFinite(tokens)) return (0).toFixed(digits);
  return (tokens / 1000).toFixed(digits);
}

function isValidCategory(value: unknown): value is ContextCategory {
  if (!value || typeof value !== "object") return false;
  const { name, tokens } = value as { name?: unknown; tokens?: unknown };
  return (
    typeof name === "string"
    && name.trim().length > 0
    && typeof tokens === "number"
    && Number.isFinite(tokens)
    && tokens >= 0
  );
}

/**
 * Validate a raw /api/context/usage payload before it reaches state. A fresh
 * or misbehaving session can return partial/non-finite data (NaN totals,
 * missing categories) that used to render "NaN%" and crash the usage panel.
 * Returns null for anything that fails validation; valid values pass through
 * unchanged.
 */
export function normalizeContextUsage(raw: unknown): ContextUsageResponse | null {
  if (!raw || typeof raw !== "object") return null;
  const usage = raw as ContextUsageResponse;
  const totalIsValid = typeof usage.total === "number"
    && Number.isFinite(usage.total)
    && usage.total >= 0;
  const limitIsValid = typeof usage.limit === "number"
    && Number.isFinite(usage.limit)
    && usage.limit > 0;
  if (!totalIsValid || !limitIsValid) return null;
  if (!Array.isArray(usage.categories) || !usage.categories.every(isValidCategory)) {
    return null;
  }
  return usage;
}
