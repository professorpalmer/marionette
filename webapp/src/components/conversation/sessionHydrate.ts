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

/** Keep thinking/executing/streaming chrome when runner is already busy. */
export function shouldPreserveBusyStatus(status: string): boolean {
  return (
    status === "thinking"
    || status === "executing"
    || status === "streaming"
  );
}

export type RunnerBusySwitchDecision =
  | { kind: "noop" }
  | { kind: "busy" }
  | { kind: "idle" };

/**
 * Immediate runner chrome for the session we switched TO (warm cache + Stop),
 * before background transcript refresh. Mirrors Conversation applyRunnerBusy.
 */
export function runnerBusySwitchDecision(opts: {
  runnerState: "running" | "idle" | "attaching" | "missing" | undefined;
  localStreamActive: boolean;
  switchedSession: boolean;
}): RunnerBusySwitchDecision {
  if (opts.localStreamActive) return { kind: "noop" };
  if (opts.runnerState === "running") return { kind: "busy" };
  if (opts.switchedSession) return { kind: "idle" };
  return { kind: "noop" };
}
