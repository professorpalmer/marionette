/**
 * Pure helpers for session-switch hydrate / artifact gather.
 * Side-effectful wiring (API, EventSource detach) stays in Conversation.tsx.
 */

export type SessionArtifact = { type: string; headline: string };

/** Collect artifact rows from sessionTranscript display cards. */
export function collectDisplayArtifacts(display: unknown): SessionArtifact[] {
  const out: SessionArtifact[] = [];
  if (!Array.isArray(display) || display.length === 0) return out;
  for (const m of display as any[]) {
    if (m?.type === "card" && m.result && Array.isArray(m.result.artifacts)) {
      for (const art of m.result.artifacts) {
        if (art && art.type && art.headline) {
          out.push({ type: art.type, headline: art.headline });
        }
      }
    }
  }
  return out;
}

/** Deduplicate artifacts by type::headline, preserving first-seen order. */
export function mergeUniqueArtifacts(
  ...groups: SessionArtifact[][]
): SessionArtifact[] {
  const seen = new Set<string>();
  const unique: SessionArtifact[] = [];
  for (const group of groups) {
    for (const art of group) {
      const key = `${art.type}::${art.headline}`;
      if (!seen.has(key)) {
        seen.add(key);
        unique.push(art);
      }
    }
  }
  return unique;
}

/**
 * When activeSessionId clears mid-project-switch: keep prior rows dimmed
 * instead of flashing the first-run empty placeholder.
 */
export function emptySessionSwitchState(priorItemCount: number): {
  clearItems: boolean;
  stale: boolean;
} {
  if (priorItemCount === 0) {
    return { clearItems: true, stale: false };
  }
  return { clearItems: false, stale: true };
}

/**
 * Keep thinking/executing/streaming/awaiting_swarm chrome when runner is busy.
 * Matches preserveOrThinking / isAgentLoopOpen so Investigating stays armed.
 */
export function shouldPreserveBusyStatus(status: string): boolean {
  return (
    status === "thinking"
    || status === "executing"
    || status === "streaming"
    || status === "awaiting_swarm"
  );
}

export type RunnerBusySwitchDecision =
  | { kind: "noop" }
  | { kind: "awaiting" }
  | { kind: "busy" }
  | { kind: "idle" };

/**
 * Immediate runner chrome for the session we switched TO (warm cache + Stop),
 * before background transcript refresh. Mirrors Conversation applyRunnerBusy.
 *
 * Prefer awaiting_swarm over thinking when getSessionState reports pending
 * swarms / state===awaiting_swarm (even while runners=running).
 */
export function runnerBusySwitchDecision(opts: {
  runnerState: "running" | "idle" | "attaching" | "missing" | undefined;
  localStreamActive: boolean;
  switchedSession: boolean;
  /** Backend still has background jobs / pause-point latch. */
  pendingSwarms?: boolean;
  /** Session machine state from getSessionState. */
  sessionState?: string | null;
}): RunnerBusySwitchDecision {
  if (opts.localStreamActive) return { kind: "noop" };
  if (
    opts.sessionState === "awaiting_swarm"
    || opts.pendingSwarms
  ) {
    return { kind: "awaiting" };
  }
  if (opts.runnerState === "running") return { kind: "busy" };
  if (opts.switchedSession) return { kind: "idle" };
  return { kind: "noop" };
}

/** Short composer notice when session-state refresh fails on switch. */
export const SESSION_STATE_FAIL_NOTICE =
  "Couldn't refresh session status - showing idle until the next check.";

/**
 * On activeSessionId change: default idle/turnOpen=false until runners resolve
 * for the target session. Prevents busy A's Stop/thinking chrome sticking on B.
 * Conversation overlays sessionSwitchPending so running B never flashes Send.
 */
export function shouldResetBusyChromeOnSwitch(switchedSession: boolean): boolean {
  return switchedSession;
}

/**
 * After getSessionState failures on switch: stay idle (runners poll may re-arm)
 * and surface a short notice. Never leave prior-session chrome stuck silently.
 */
export function sessionStateFailureSwitchDecision(): {
  kind: "idle_with_notice";
  notice: string;
} {
  return { kind: "idle_with_notice", notice: SESSION_STATE_FAIL_NOTICE };
}

/**
 * Composer notice when sessionTranscript refresh fails or returns an empty
 * feed that would wipe warm cache rows (disk/attach flake honesty).
 */
export const SESSION_TRANSCRIPT_FAIL_NOTICE =
  "Couldn't refresh this session's messages — showing what we have until the next check.";

/**
 * Drop sticky SESSION_* fail banners after successful transcript hydrate or
 * getSessionState / runners recovery. Leaves unrelated edit/rewind notices alone.
 */
export function clearRecoveredSessionFailNotice(
  notice: string | null,
): string | null {
  if (
    notice === SESSION_STATE_FAIL_NOTICE
    || notice === SESSION_TRANSCRIPT_FAIL_NOTICE
  ) {
    return null;
  }
  return notice;
}

/**
 * Empty transcript on a cold boot OR non-empty cache-hit can be a disk/attach
 * race. Retry before accepting blank — a warm cache with rows must not be
 * hard-replaced with [] on the first empty response.
 *
 * Only an explicit New Session seed (`seededEmpty`) skips retry. A plain
 * zero-row cache entry (e.g. after /clear, or ambiguous eviction) still
 * retries so a flaky empty response cannot silently blank a real session.
 */
export function shouldRetryEmptyTranscript(opts: {
  loadedCount: number;
  attempt: number;
  maxAttempts: number;
  /** Warm-cache length when present; omit on cache miss. */
  cachedCount?: number;
  /** True only for New Session's intentional `[]` seed. */
  seededEmpty?: boolean;
}): boolean {
  if (opts.loadedCount !== 0) return false;
  if (opts.seededEmpty) return false;
  return opts.attempt < opts.maxAttempts - 1;
}

/**
 * Cache-hit with warm rows received an empty transcript after retries: keep
 * those rows, mark stale, and surface a notice. Never hard-replace with [].
 */
export function cacheHitEmptyTranscriptDecision(): {
  kind: "keep_warm_with_notice";
  stale: true;
  notice: string;
} {
  return {
    kind: "keep_warm_with_notice",
    stale: true,
    notice: SESSION_TRANSCRIPT_FAIL_NOTICE,
  };
}

/**
 * After retries, empty remote transcript + warm cache.
 * Non-empty warm rows: keep + notice. Explicit New Session seed or cleared
 * empty cache after retries: accept blank.
 */
export function emptyTranscriptAfterRetryDecision(opts: {
  cachedCount: number;
  seededEmpty?: boolean;
}): { kind: "accept_empty" } | ReturnType<typeof cacheHitEmptyTranscriptDecision> {
  if (opts.cachedCount > 0) {
    return cacheHitEmptyTranscriptDecision();
  }
  return { kind: "accept_empty" };
}

/**
 * Transcript refresh exception path. Cache hit: keep rows + stale + notice.
 * Cache miss: clear relics but mark stale (Loading…) + notice — never look
 * like a legitimate first-run empty session with no honesty signal.
 */
export function transcriptRefreshFailureDecision(hadCache: boolean): {
  kind: "keep_warm_with_notice" | "clear_stale_with_notice";
  clearItems: boolean;
  stale: true;
  notice: string;
} {
  if (hadCache) {
    return {
      kind: "keep_warm_with_notice",
      clearItems: false,
      stale: true,
      notice: SESSION_TRANSCRIPT_FAIL_NOTICE,
    };
  }
  return {
    kind: "clear_stale_with_notice",
    clearItems: true,
    stale: true,
    notice: SESSION_TRANSCRIPT_FAIL_NOTICE,
  };
}

/**
 * Mid-turn reattach when getSessionState fails: retry, then optimistic busy so
 * Ready chrome cannot lie while a turn continues. Runners poll clears idle targets.
 */
export function reattachSessionStateFailureDecision(opts: {
  attempt: number;
  maxAttempts: number;
}): "retry" | "optimistic_busy" {
  if (opts.attempt < opts.maxAttempts) return "retry";
  return "optimistic_busy";
}
