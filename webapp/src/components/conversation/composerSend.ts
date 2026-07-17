/**
 * Pure composer / send-path helpers. Conversation.tsx keeps the React wiring.
 */

/** Enter while busy: Cmd/Ctrl+Enter queues; plain Enter steers/sends. */
export function composerEnterAction(opts: {
  busy: boolean;
  metaOrCtrl: boolean;
}): "queue" | "send" {
  if (opts.busy && opts.metaOrCtrl) return "queue";
  return "send";
}

/**
 * executeSend entry gates: stale transcript blocks real sends; Stop blocks
 * keep-alive resume turns.
 */
export function executeSendGate(opts: {
  transcriptStale: boolean;
  resume: boolean;
  userStopped: boolean;
}): "ok" | "stale" | "stopped_resume" {
  if (opts.transcriptStale && !opts.resume) return "stale";
  if (opts.resume && opts.userStopped) return "stopped_resume";
  return "ok";
}

/** Top-level send(): empty composer (no text and no images) is a no-op. */
export function shouldBlockEmptySend(opts: {
  transcriptStale: boolean;
  text: string;
  imageCount: number;
}): boolean {
  if (opts.transcriptStale) return true;
  if (!opts.text.trim() && opts.imageCount === 0) return true;
  return false;
}

export function formatHelpSlashReply(
  commands: { cmd: string; desc: string }[],
): string {
  return (
    "Available Slash Commands:\n\n"
    + commands.map((s) => `* \`${s.cmd}\` - ${s.desc}`).join("\n")
    + "\n\nType @ to list and mention files in your message context."
  );
}

export function formatCompactCompleteMessage(
  beforeTokens: number,
  afterTokens: number,
): string {
  return (
    "System Note: Manual context compaction complete ("
    + beforeTokens
    + " -> "
    + afterTokens
    + " tokens)."
  );
}

export function formatCompactErrorMessage(err: unknown): string {
  const message =
    err && typeof err === "object" && "message" in err
      ? String((err as { message?: unknown }).message || err)
      : String(err || "");
  return "[error] Compaction failed: " + message;
}

export function formatSteerErrorMessage(err: unknown): string {
  const message =
    err && typeof err === "object" && "message" in err
      ? String((err as { message?: unknown }).message || err)
      : String(err || "");
  return "[error] Steer failed: " + message;
}

export function formatRenderCommandErrorMessage(err: unknown): string {
  const message =
    err && typeof err === "object" && "message" in err
      ? String((err as { message?: unknown }).message || err)
      : String(err || "");
  return "[error] Render failed: " + message;
}

/** Edit-notice chrome after rewind-edit send (Hermes/Cursor pattern). */
export function editNoticeAfterSend(canRevertEdit: boolean): string | null {
  return canRevertEdit
    ? "Edited — Revert restores the previous turns."
    : null;
}
