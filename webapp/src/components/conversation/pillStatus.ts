import { isAgentLoopOpen } from "./runnersBusy";

/**
 * Header StatusPill status from transcript/session chrome signals.
 * Prefer investigation / open-turn truth over brief runner idle flaps.
 * Emits human chrome keys (investigating / awaiting_swarm), never raw
 * machine flaps like executing/thinking for sticky busy.
 *
 * Idle/busy matches composerBusy / agentLoopOpen — never force idle while
 * Steer/Stop remain (answer-complete SSE lag must not split chrome).
 */

/** Pilot mid-turn — holdSwarmAwait must not seal Investigating chrome. */
export function isPilotBusy(turnOpen: boolean, status: string): boolean {
  return (
    turnOpen
    || status === "thinking"
    || status === "executing"
    || status === "streaming"
  );
}

/**
 * Pause-point chrome (matches TranscriptList.pausePoint):
 * awaiting_swarm, or holdSwarmAwait while the pilot is idle.
 * Bare holdSwarmAwait alone must NOT pause mid-turn — keep it for
 * agentLoopOpen / composerBusy / Stop-Steer only.
 */
export function isSwarmPausePoint(opts: {
  status: string;
  holdSwarmAwait: boolean;
  turnOpen: boolean;
}): boolean {
  return (
    opts.status === "awaiting_swarm"
    || (opts.holdSwarmAwait && !isPilotBusy(opts.turnOpen, opts.status))
  );
}

export function derivePillStatus(opts: {
  transcriptStale: boolean;
  /** When 0 on a cold miss, skip "switching…" chrome (that was part of the blink). */
  paintableCount?: number;
  /**
   * Legacy pure-chat "answer sealed" signal. Ignored while the agent-loop
   * latch is open so StatusPill cannot go idle ahead of composerBusy.
   */
  answerChromeIdle: boolean;
  liveInvestigation: boolean;
  turnOpen: boolean;
  status: string;
  /** Wave-3 lifecycle; interrupted must not paint Done. */
  turnLifecycle?: string;
  /**
   * Background pause-point (awaiting_swarm, or holdSwarmAwait && !pilotBusy).
   * Wins over liveInvestigation so StatusPill paints Still working….
   */
  awaitingSwarm?: boolean;
  /** Same latch as composerBusy; defaults from turnOpen + status. */
  agentLoopOpen?: boolean;
}): string {
  const {
    transcriptStale,
    answerChromeIdle,
    liveInvestigation,
    turnOpen,
    status,
    awaitingSwarm,
    agentLoopOpen,
    turnLifecycle,
  } = opts;
  // Empty cold-miss: stay off "switching…" so the header does not flash.
  // Still show it when stale rows are on screen (refresh honesty).
  if (transcriptStale && (opts.paintableCount ?? 1) > 0) return "switching…";
  // Pause-point wins over sticky liveInvestigation (hold-extended agentLoopOpen).
  if (awaitingSwarm) return "awaiting_swarm";
  const loopOpen = agentLoopOpen ?? isAgentLoopOpen(turnOpen, status);
  if (turnLifecycle === "interrupted" && !loopOpen) return "interrupted";
  // Only early-idle when composerBusy would also be false.
  if (answerChromeIdle && !loopOpen) return "idle";
  // Live tools / Investigation fold: always Investigating chrome — never flash
  // raw executing/thinking (or idle/done flaps remapped to those) in the header.
  if (liveInvestigation) return "investigating";
  if (turnOpen && (status === "idle" || status === "done")) return "thinking";
  if (status === "executing") return "investigating";
  return status;
}
