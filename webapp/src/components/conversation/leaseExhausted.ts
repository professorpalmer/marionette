/**
 * Same copy as LeftRail.SESSION_LEASE_EXHAUSTED_MESSAGE — duplicated here to
 * avoid a Conversation ↔ LeftRail circular import (LeftRail imports Conversation).
 */
const SESSION_LEASE_EXHAUSTED_MESSAGE =
  "This session could not start — too many sessions are busy right now. Wait a moment or stop another turn, then try again.";

type LeaseExhaustedPayload = {
  code?: string;
  error?: string;
  message?: string;
  status?: number;
  max_concurrent?: number;
  active_count?: number;
  busy_session_ids?: string[];
  busy_session_titles?: string[];
};

/** True when WorkspaceChip open failed because session runner leases are full.
 * Mirrors LeftRail.isLeaseExhaustedError — duplicated here to avoid a
 * Conversation ↔ LeftRail circular import (LeftRail imports Conversation). */
export function isWorkspaceOpenLeaseExhausted(err: unknown): boolean {
  if (!err) return false;
  const e = err as LeaseExhaustedPayload;
  if (e.code === "lease_exhausted") return true;
  const msg = String(e.message || e.error || err || "");
  // Message-only fallbacks. Do NOT treat a bare "... -> 409" as lease exhaustion.
  if (/lease_exhausted/i.test(msg)) return true;
  if (/session runner lease exhausted/i.test(msg)) return true;
  return false;
}

/** Hermes-style copy when the 409 body includes capacity / busy titles. */
export function formatWorkspaceOpenLeaseExhaustedMessage(err: unknown): string {
  const e = (err || {}) as LeaseExhaustedPayload;
  const max = typeof e.max_concurrent === "number" ? e.max_concurrent : null;
  const active = typeof e.active_count === "number" ? e.active_count : null;
  const titles = (e.busy_session_titles || []).map((t) => String(t).trim()).filter(Boolean);
  const capacity =
    max != null
      ? active != null
        ? `${active}/${max}`
        : `${max}`
      : null;
  if (titles.length && capacity) {
    return `Too many sessions are busy (${capacity}). Stop one of: ${titles.map((t) => `"${t}"`).join(", ")} — then try again.`;
  }
  if (titles.length) {
    return `Too many sessions are busy. Stop one of: ${titles.map((t) => `"${t}"`).join(", ")} — then try again.`;
  }
  if (capacity) {
    return `This session could not start — session capacity is full (${capacity}). Wait a moment or stop another turn, then try again.`;
  }
  return SESSION_LEASE_EXHAUSTED_MESSAGE;
}
