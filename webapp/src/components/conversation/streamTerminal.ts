/**
 * Pure helpers for stream onDone / onError terminal chrome.
 */

export const STREAM_ABORT_MESSAGE =
  "[aborted] Connection closed before the turn finished. Send again to retry.";

export type StreamTerminalDecision =
  | { kind: "abort_error" }
  | { kind: "done" }
  | { kind: "preserve_error_or_done" }
  | { kind: "noop" };

/** Live SSE onDone after flush — abort if the turn never settled. */
export function streamOnDoneDecision(opts: {
  turnSettled: boolean;
  userStopped: boolean;
}): StreamTerminalDecision {
  if (!opts.turnSettled && !opts.userStopped) return { kind: "abort_error" };
  return { kind: "done" };
}

/** Live SSE onError after flush — ignore false errors after assistant_done. */
export function streamOnErrorDecision(opts: {
  turnSettled: boolean;
  userStopped: boolean;
}): StreamTerminalDecision {
  if (!opts.turnSettled && !opts.userStopped) return { kind: "abort_error" };
  if (!opts.userStopped) return { kind: "preserve_error_or_done" };
  return { kind: "noop" };
}
