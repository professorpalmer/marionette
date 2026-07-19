/**
 * Shared nested-action bounds / status normalization for live merge and
 * durable hydrate. Keep in sync with harness.job_actions.
 */

export const MAX_JOB_ACTIONS = 80;
export const MAX_ACTION_KIND_CHARS = 64;
export const MAX_ACTION_GOAL_CHARS = 240;
export const MAX_ACTION_ERROR_CHARS = 240;
export const MAX_ACTION_ID_CHARS = 128;

const TERMINAL_JOB_STATUSES = new Set([
  "completed",
  "failed",
  "cancelled",
  "canceled",
  "done",
]);

export function isTerminalJobStatus(status: string | undefined | null): boolean {
  return TERMINAL_JOB_STATUSES.has(String(status || "").trim().toLowerCase());
}

/** Bound a live/hydrate action string the same way the harness sanitizer does. */
export function boundActionField(value: unknown, limit: number): string {
  const text = String(value ?? "").trim();
  if (text.length <= limit) return text;
  if (limit <= 1) return text.slice(0, limit);
  return `${text.slice(0, Math.max(0, limit - 1))}…`;
}

/** Normalize live/hydrate nested status (aligned across poll + reload). */
export function normalizeNestedActionStatus(
  raw: unknown,
  error?: unknown,
): "running" | "complete" | "failed" {
  const statusRaw = String(raw || "").toLowerCase().trim();
  if (statusRaw === "complete" || statusRaw === "failed" || statusRaw === "running") {
    return statusRaw;
  }
  if (
    statusRaw === "completed"
    || statusRaw === "done"
    || statusRaw === "success"
    || statusRaw === "ok"
  ) {
    return "complete";
  }
  if (
    statusRaw === "error"
    || statusRaw === "cancelled"
    || statusRaw === "canceled"
    || statusRaw === "interrupted"
  ) {
    return "failed";
  }
  // Unknown: match transcript hydrate — error → failed, else complete (not running).
  return error ? "failed" : "complete";
}
