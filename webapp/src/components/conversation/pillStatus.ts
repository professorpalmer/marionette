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

export function derivePillStatus(opts: {
  transcriptStale: boolean;
  /**
   * Legacy pure-chat "answer sealed" signal. Ignored while the agent-loop
   * latch is open so StatusPill cannot go idle ahead of composerBusy.
   */
  answerChromeIdle: boolean;
  liveInvestigation: boolean;
  turnOpen: boolean;
  status: string;
  /** Background jobs still flying after the model turn closed. */
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
  } = opts;
  if (transcriptStale) return "switching…";
  if (awaitingSwarm) return "awaiting_swarm";
  const loopOpen = agentLoopOpen ?? isAgentLoopOpen(turnOpen, status);
  // Only early-idle when composerBusy would also be false.
  if (answerChromeIdle && !loopOpen) return "idle";
  // Live tools / Investigation fold: always Investigating chrome — never flash
  // raw executing/thinking (or idle/done flaps remapped to those) in the header.
  if (liveInvestigation) return "investigating";
  if (turnOpen && (status === "idle" || status === "done")) return "thinking";
  if (status === "executing") return "investigating";
  return status;
}
