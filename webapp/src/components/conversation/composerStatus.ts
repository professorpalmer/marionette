/**
 * Composer chrome from runners poll (no local SSE).
 * running ≠ busy: a flying PM job / detached runner must not replace Send.
 * Local SSE still owns the mouth (return null). Used by tests; live chrome
 * uses isPilotMouthBusy(turnOpen, status).
 */
export function composerStatusFromRunner(
  activeSessionId: string | null,
  runners: Record<string, "running" | "idle" | "attaching" | "missing"> | undefined,
  localStreamActive: boolean,
): "thinking" | "idle" | null {
  if (localStreamActive || !activeSessionId) return null;
  void runners;
  return "idle";
}
