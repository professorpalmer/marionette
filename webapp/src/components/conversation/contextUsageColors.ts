/** Segment colors for the composer context-usage panel. */
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
  if (limit <= 0) return 0;
  return Math.min(100, Math.round((total / limit) * 100));
}

export function formatTokenK(tokens: number, digits = 1): string {
  return (tokens / 1000).toFixed(digits);
}
