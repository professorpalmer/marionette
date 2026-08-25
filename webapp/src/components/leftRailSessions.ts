import { type Session } from "../lib/api";
import { repoPathsEqual } from "../lib/pathNormalize";
import { readSWRCache, writeSWRCache } from "../lib/useStaleWhileRevalidate";

/** User-facing copy when concurrent session runner leases are full. */
export const SESSION_LEASE_EXHAUSTED_MESSAGE =
  "This session could not start — too many sessions are busy right now. Wait a moment or stop another turn, then try again.";

export type LeaseExhaustedPayload = {
  code?: string;
  error?: string;
  message?: string;
  status?: number;
  max_concurrent?: number;
  active_count?: number;
  busy_session_ids?: string[];
  busy_session_titles?: string[];
};

/** True when switch/open/create failed because all session runner leases are busy. */
export function isLeaseExhaustedError(err: unknown): boolean {
  if (!err) return false;
  const e = err as LeaseExhaustedPayload;
  if (e.code === "lease_exhausted") return true;
  const msg = String(e.message || e.error || err || "");
  // Message-only fallbacks (older servers / bridge quirks). Do NOT treat a bare
  // "... -> 409" as lease exhaustion — other conflicts share that status.
  if (/lease_exhausted/i.test(msg)) return true;
  if (/session runner lease exhausted/i.test(msg)) return true;
  return false;
}

/** Hermes-style toast: name busy sessions and show capacity when the 409 body has them. */
export function formatLeaseExhaustedMessage(err: unknown): string {
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

/**
 * Stable PROJECTS rail order: pin durable Home first (when provided), keep
 * remaining recents as-is, append currentRepo only when it is not already
 * present (slash/case-insensitive). Never force the active path to index 0 —
 * Home is the only synthetic pin.
 */
export function buildProjectsList(
  currentRepo: string,
  rawRecents: string[],
  home?: string,
): string[] {
  const seen: string[] = [];
  const out: string[] = [];
  if (home) {
    seen.push(home);
    out.push(home);
  }
  for (const p of rawRecents) {
    if (!p || seen.some((s) => repoPathsEqual(s, p))) continue;
    seen.push(p);
    out.push(p);
  }
  if (currentRepo && !seen.some((s) => repoPathsEqual(s, currentRepo))) {
    out.push(currentRepo);
  }
  return out;
}

/** Drop a path (and slash/case siblings) from a recents list. */
export function filterForgottenRecent(recents: string[], path: string): string[] {
  return (recents || []).filter((r) => !repoPathsEqual(r, path));
}

/**
 * After removing the active project, land on Home (when pinned) or the first
 * remaining recent. Never leave a stale cwd selected once the row is gone.
 */
export function pickFallbackProjectAfterForget(
  recents: string[],
  forgottenPath: string,
  home?: string,
): string {
  const remaining = filterForgottenRecent(recents, forgottenPath);
  return buildProjectsList("", remaining, home)[0] || "";
}

/**
 * Settle/Unsettle only work for sessions under the active workspace
 * (POST /api/sessions/settle 403s foreign roots). Hide affordances elsewhere.
 */
export function canSettleSessionsForProject(
  projectPath: string,
  activeRepo: string | undefined | null,
): boolean {
  return !!(activeRepo && projectPath && repoPathsEqual(projectPath, activeRepo));
}

/**
 * Split project-scoped sessions into open (inbox) vs settled.
 * `settled` and `archived` are independent durable flags. Archived rows are
 * excluded here — they live in the global Archived section, not the project tree.
 * Rootless orphans only appear under the active workspace row.
 */
export function partitionProjectSessions(
  rows: Session[],
  projectPath: string,
  isActiveRow: boolean,
): { open: Session[]; settled: Session[] } {
  const scoped = (rows || []).filter((s) => {
    const root = (s.workspace_root || s.repo || "").trim();
    if (!root) return isActiveRow;
    return repoPathsEqual(root, projectPath);
  });
  const open: Session[] = [];
  const settled: Session[] = [];
  for (const s of scoped) {
    if (s.archived) continue;
    if (s.settled) settled.push(s);
    else open.push(s);
  }
  return { open, settled };
}

/** Read current settled flag for a session id from per-root caches (first hit). */
export function readSessionSettledFromCaches(
  roots: string[],
  sessionId: string,
  read: (key: string) => Session[] | undefined = readSWRCache,
): boolean | undefined {
  for (const root of roots) {
    if (!root) continue;
    const cached = read(`sessions:${root}`);
    const hit = cached?.find((s) => s.id === sessionId);
    if (hit) return !!hit.settled;
  }
  return undefined;
}

/** Optimistically flip durable `settled` on every per-root sessions cache that holds the id. */
export function patchSessionSettledInCaches(
  roots: string[],
  sessionId: string,
  settled: boolean,
  read: (key: string) => Session[] | undefined = readSWRCache,
  write: (key: string, data: Session[]) => void = writeSWRCache,
): number {
  let touched = 0;
  for (const root of roots) {
    if (!root) continue;
    const key = `sessions:${root}`;
    const cached = read(key);
    if (!cached || !cached.some((s) => s.id === sessionId)) continue;
    write(
      key,
      cached.map((s) => (s.id === sessionId ? { ...s, settled } : s)),
    );
    touched += 1;
  }
  return touched;
}

/** Optimistically set the display title on every per-root sessions cache that holds the id. */
export function patchSessionTitleInCaches(
  roots: string[],
  sessionId: string,
  title: string,
  read: (key: string) => Session[] | undefined = readSWRCache,
  write: (key: string, data: Session[]) => void = writeSWRCache,
): number {
  let touched = 0;
  for (const root of roots) {
    if (!root) continue;
    const key = `sessions:${root}`;
    const cached = read(key);
    if (!cached || !cached.some((s) => s.id === sessionId)) continue;
    write(
      key,
      cached.map((s) => (s.id === sessionId ? { ...s, title } : s)),
    );
    touched += 1;
  }
  return touched;
}

/** Optimistically flip durable `archived` on every per-root sessions cache that holds the id. */
export function patchSessionArchivedInCaches(
  roots: string[],
  sessionId: string,
  archived: boolean,
  read: (key: string) => Session[] | undefined = readSWRCache,
  write: (key: string, data: Session[]) => void = writeSWRCache,
): number {
  let touched = 0;
  for (const root of roots) {
    if (!root) continue;
    const key = `sessions:${root}`;
    const cached = read(key);
    if (!cached || !cached.some((s) => s.id === sessionId)) continue;
    write(
      key,
      cached.map((s) => (s.id === sessionId ? { ...s, archived } : s)),
    );
    touched += 1;
  }
  return touched;
}

/** Drop a session id from every per-root sessions SWR cache. Returns how many
 *  caches were rewritten. Used on delete so inactive projects do not keep
 *  phantom titles (the "merged dir" ghost). */
export function purgeSessionFromRootCaches(
  roots: string[],
  sessionId: string,
  read: (key: string) => Session[] | undefined = readSWRCache,
  write: (key: string, data: Session[]) => void = writeSWRCache,
): number {
  let touched = 0;
  for (const root of roots) {
    if (!root) continue;
    const key = `sessions:${root}`;
    const cached = read(key);
    if (!cached) continue;
    const next = cached.filter((s) => s.id !== sessionId);
    if (next.length === cached.length) continue;
    write(key, next);
    touched += 1;
  }
  return touched;
}

/** SWR cache key for the Branches list -- keyed by repo so project switches
 *  do not flash another project's branches, and revisits stay warm. */
export function workspacesCacheKey(repo: string): string {
  return `workspaces:${repo || "__none__"}`;
}

/** SWR cache key for jobs scoped to both the selected project and active session. */
export function jobsCacheKey(projectPath: string, activeSessionId?: string): string {
  return `jobs:${projectPath || "__none__"}:${activeSessionId || "__none__"}`;
}

/** True when LeftRail should offer Stop without forcing a view attach. */
export function shouldOfferBackgroundStop(
  status: "running" | "idle" | "attaching" | "missing" | undefined,
  isActive: boolean,
): boolean {
  return status === "running" && !isActive;
}

export type RunnerStatus = "running" | "idle" | "attaching" | "missing";

/** Sessions that finished a background turn while the user looked elsewhere. */
export function collectUnreadFinishedSessionIds(
  prev: Record<string, RunnerStatus>,
  next: Record<string, RunnerStatus>,
  activeSessionId: string | undefined,
): string[] {
  const out: string[] = [];
  for (const [id, status] of Object.entries(next)) {
    if (id === activeSessionId) continue;
    if (status === "idle" && prev[id] === "running") out.push(id);
  }
  return out;
}

/**
 * Rail-wide dim / project-switching signal. Browse-selecting an already-listed
 * project must not trip this — jobs SWR key changes on select and used to flash
 * the whole PROJECTS tree. Only real open/switch/session activation dims the rail.
 */
export function isRailWideSwitching(flags: {
  opening: boolean;
  switchingSessionId: string | null;
  workspaceTransitioning: boolean;
  sessionsTransitioning: boolean;
}): boolean {
  return (
    flags.opening
    || !!flags.switchingSessionId
    || flags.workspaceTransitioning
    || flags.sessionsTransitioning
  );
}

/**
 * Per-project empty-state: spinner only until that root's sessions resolve.
 * Never gate on rail-wide / jobs transitioning (that hid the New session CTA
 * and blinked "Loading sessions..." on first expand of a listed project).
 */
export function projectSessionsEmptyState(
  sessionsReady: boolean,
  showRowLoading: boolean,
): "loading" | "pending" | "empty" {
  if (sessionsReady) return "empty";
  return showRowLoading ? "loading" : "pending";
}
