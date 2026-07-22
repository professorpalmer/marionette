/** Action for an SSE close that did not already settle via kind:exit handling. */
export type TerminalBareOnDoneAction =
  | "noop"
  | "mark_exited"
  | "auto_recover"
  | "reattach";

/**
 * Bare SSE onDone (no kind:exit): never treat a transient stream drop as fatal.
 * Reattach the same session when we already saw ConPTY output; only the one-shot
 * empty-stream race still kill+recreates. Restart remains an explicit kill path.
 */
export function terminalBareOnDoneAction(opts: {
  disposed: boolean;
  sawExit: boolean;
  hasSession: boolean;
  sawOutput: boolean;
  autoRecovered: boolean;
}): TerminalBareOnDoneAction {
  if (opts.disposed) return "noop";
  if (opts.sawExit) return "mark_exited";
  if (!opts.hasSession) return "mark_exited";
  if (!opts.sawOutput) {
    return opts.autoRecovered ? "mark_exited" : "auto_recover";
  }
  return "reattach";
}
