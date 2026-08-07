/**
 * Header StatusPill status from transcript/session chrome signals.
 * Prefer investigation / open-turn truth over brief runner idle flaps.
 */
export function derivePillStatus(opts: {
  transcriptStale: boolean;
  answerChromeIdle: boolean;
  liveInvestigation: boolean;
  turnOpen: boolean;
  status: string;
  /** Background jobs still flying after the model turn closed. */
  awaitingSwarm?: boolean;
}): string {
  const {
    transcriptStale,
    answerChromeIdle,
    liveInvestigation,
    turnOpen,
    status,
    awaitingSwarm,
  } = opts;
  if (transcriptStale) return "switching…";
  if (awaitingSwarm) return "awaiting_swarm";
  if (answerChromeIdle) return "idle";
  if (liveInvestigation && (status === "idle" || status === "done")) return "executing";
  if (turnOpen && (status === "idle" || status === "done")) return "thinking";
  return status;
}
