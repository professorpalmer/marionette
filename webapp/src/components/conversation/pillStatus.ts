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
}): string {
  const { transcriptStale, answerChromeIdle, liveInvestigation, turnOpen, status } = opts;
  if (transcriptStale) return "switching…";
  if (answerChromeIdle) return "idle";
  if (liveInvestigation && (status === "idle" || status === "done")) return "executing";
  if (turnOpen && (status === "idle" || status === "done")) return "thinking";
  return status;
}
