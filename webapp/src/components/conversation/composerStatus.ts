/**
 * Composer chrome from runners poll (no local SSE).
 * When the active session's runner is "running", show Stop/Steer (thinking);
 * otherwise allow Send (idle). Used by Conversation and mirrored in tests.
 */
export function composerStatusFromRunner(
  activeSessionId: string | null,
  runners: Record<string, "running" | "idle" | "attaching" | "missing"> | undefined,
  localStreamActive: boolean,
): "thinking" | "idle" | null {
  if (localStreamActive || !activeSessionId) return null;
  // "attaching" = deferred cold pilot build — not a user turn (no thinking chrome).
  if (runners?.[activeSessionId] === "running") return "thinking";
  return "idle";
}
