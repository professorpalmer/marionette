/**
 * Pure helpers for stream onDone / onError terminal chrome.
 */

export const STREAM_ABORT_MESSAGE =
  "[aborted] Connection closed before the turn finished. Send again to retry.";

/**
 * Sanitized stream error shape from the Electron bridge (stream-bridge.cjs):
 * `{ status, code, message }` with no token/body. The web transport instead
 * throws `Error("stream /api/chat -> 403")`; both are handled here.
 */
export type StreamErrorLike =
  | { status?: number | null; code?: string; message?: string }
  | Error
  | string
  | null
  | undefined;

function streamErrorHttpStatus(err: StreamErrorLike): number | null {
  const structured = (err as { status?: number | null } | null)?.status;
  if (typeof structured === "number" && Number.isFinite(structured)) return structured;
  const text = String((err as { message?: string } | null)?.message ?? err ?? "");
  const m = /(?:->|HTTP)\s*(\d{3})\b/.exec(text);
  return m ? Number(m[1]) : null;
}

/**
 * Meaningful terminal text for a stream error. Deliberately never echoes the
 * raw error payload (it crossed a process boundary and must stay secret-free);
 * only the HTTP status / connection class picks the message.
 */
export function streamErrorText(err: StreamErrorLike): string {
  const status = streamErrorHttpStatus(err);
  if (status === 401 || status === 403) {
    return (
      `[error] The backend rejected this request (HTTP ${status}: authentication failed). ` +
      "The app and backend are likely out of sync after an update — fully quit and " +
      "relaunch Marionette, or install the latest version."
    );
  }
  if (status != null) {
    return `[error] The backend request failed (HTTP ${status}). Send again to retry.`;
  }
  const text = String(
    (err as { code?: string } | null)?.code
      ?? (err as { message?: string } | null)?.message
      ?? err
      ?? "",
  );
  if (/ECONNREFUSED|ECONNRESET|ETIMEDOUT|EPIPE|socket hang up|conn_error/i.test(text)) {
    return (
      "[error] The backend is not reachable (connection failed). " +
      "It may be restarting — wait a moment and send again."
    );
  }
  return STREAM_ABORT_MESSAGE;
}

export type StreamTerminalDecision =
  | { kind: "abort_error" }
  | { kind: "done" }
  | { kind: "preserve_error_or_done" }
  | { kind: "noop" };

/**
 * Live SSE onDone after flush — abort if the turn never settled.
 * When the transcript already shows a sealed answer (SSE lag / missing
 * assistant_done), settle silently instead of painting a false abort bubble.
 */
export function streamOnDoneDecision(opts: {
  turnSettled: boolean;
  userStopped: boolean;
  /** Pure-chat answer already sealed on screen (see turnLooksAnswerComplete). */
  answerComplete?: boolean;
}): StreamTerminalDecision {
  if (opts.turnSettled || opts.userStopped) return { kind: "done" };
  if (opts.answerComplete) return { kind: "done" };
  return { kind: "abort_error" };
}

/** Cleared on session switch so settled A cannot suppress busy chrome for B. */
export function resetTurnSettledOnSessionSwitch(
  turnSettledRef: { current: boolean },
): void {
  turnSettledRef.current = false;
}

/**
 * Cleared on switchedSession so Stop / pilot_resume / approved-command retry
 * latched on A cannot force idle, skip reattach, or fire into mid-turn B.
 */
export function resetCrossSessionLatchesOnSwitch(opts: {
  userStoppedRef: { current: boolean };
  resumeQueuedRef: { current: boolean };
  approvedCommandRetryRef: { current: string | null };
}): void {
  opts.userStoppedRef.current = false;
  opts.resumeQueuedRef.current = false;
  opts.approvedCommandRetryRef.current = null;
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

/**
 * Whether a mid-turn stream event may refresh busy chrome (thinking /
 * executing / streaming). After an authoritative terminal (assistant_done /
 * error / auto_halt / Stop), late command/batch receipts must update cards
 * without reopening Stop/Steer or a permanent thinking pill.
 */
export function shouldRefreshBusyChrome(opts: {
  turnSettled: boolean;
  userStopped?: boolean;
}): boolean {
  if (opts.turnSettled) return false;
  if (opts.userStopped) return false;
  return true;
}
