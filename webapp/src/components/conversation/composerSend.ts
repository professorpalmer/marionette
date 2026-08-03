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
  const reason =
    err && typeof err === "object" && "reason" in err
      ? String((err as { reason?: unknown }).reason || "")
      : "";
  if (reason === "no_compactable_history") {
    return "System Note: Recent turn is already compact — nothing further to summarize.";
  }
  if (reason === "summary_rejected") {
    return "System Note: Compaction summary was rejected; history left unchanged. You can try again or continue.";
  }
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

/** Edit-notice chrome after rewind-edit send.

  Resubmit starts the new turn; the Revert/restore affordance is only offered
  while the composer is still in edit mode (Cancel). Lingering "Revert?" after
  send left a dead chrome that restored the old branch without starting a loop.
*/
export function editNoticeAfterSend(_canRevertEdit: boolean): string | null {
  return null;
}

/** Shown while auto stop+rewind runs after edit during an active turn. */
export const EDIT_BUSY_PROGRESS_NOTICE =
  "Sending will stop and revert to this message…";

export type EditOrdinalItem = {
  kind: string;
  msg?: { role: string };
};

/** User-message ordinal for rewind: UI-only rows before idx are skipped. */
export function userOrdinalBeforeIndex(
  items: EditOrdinalItem[],
  idx: number,
): number {
  return items
    .slice(0, idx)
    .filter((it) => it.kind === "msg" && it.msg?.role === "user").length;
}

/** Standalone editNotice with no edit/revert chrome needs an explicit dismiss. */
export function showStandaloneEditNoticeDismiss(opts: {
  editingIndex: number | null;
  canRevertEdit: boolean;
  editNotice: string | null;
}): boolean {
  return (
    opts.editNotice !== null
    && opts.editingIndex === null
    && !opts.canRevertEdit
  );
}

export type RewindSessionResponse = {
  ok: boolean;
  prefill?: string;
  notice?: string;
  error?: string;
};

export type EditMessageFlowResult =
  | { kind: "interrupt_failed"; notice: string }
  | { kind: "rewind_failed"; notice: string }
  | {
      kind: "success";
      truncateToIndex: number;
      prefill: string;
      notice: string;
    };

function editFlowErrorMessage(err: unknown, fallback: string): string {
  if (err && typeof err === "object" && "message" in err) {
    return String((err as { message?: unknown }).message || fallback);
  }
  return fallback;
}

/**
 * Idle edit: rewind only. Busy edit: stop local UI, await interrupt, then rewind.
 * Caller must guard duplicate clicks with editBusy and set EDIT_BUSY_PROGRESS_NOTICE
 * before awaiting when composerBusy.
 */
export async function runEditMessageFlow(opts: {
  composerBusy: boolean;
  idx: number;
  userOrdinal: number;
  originalText: string;
  stopLocal: () => void;
  interruptSession: () => Promise<{ ok: boolean }>;
  rewindSession: (userOrdinal: number) => Promise<RewindSessionResponse>;
}): Promise<EditMessageFlowResult> {
  if (opts.composerBusy) {
    opts.stopLocal();
    try {
      const interruptRes = await opts.interruptSession();
      if (!interruptRes?.ok) {
        return {
          kind: "interrupt_failed",
          notice: "Could not stop the current turn.",
        };
      }
    } catch (err) {
      return {
        kind: "interrupt_failed",
        notice: editFlowErrorMessage(err, "Could not stop the current turn."),
      };
    }
  }

  try {
    const res = await opts.rewindSession(opts.userOrdinal);
    if (!res?.ok) {
      return {
        kind: "rewind_failed",
        notice: res?.error || "Could not rewind transcript for edit.",
      };
    }
    return {
      kind: "success",
      truncateToIndex: opts.idx,
      prefill: res.prefill || opts.originalText,
      notice: res.notice || "Editing — resubmit, or Revert to restore.",
    };
  } catch (err) {
    return {
      kind: "rewind_failed",
      notice: editFlowErrorMessage(err, "Rewind failed."),
    };
  }
}

export type LocalSlashAction =
  | { kind: "none" }
  | { kind: "clear_or_new" }
  | { kind: "compact" }
  | { kind: "model" }
  | { kind: "help" }
  | { kind: "custom"; name: string; args: string };

/**
 * Classify a composer message that starts with `/` into a local slash action.
 * Built-in commands unknown here fall through as `none` (sent to the model).
 */
export function classifyLocalSlashCommand(opts: {
  message: string;
  isBuiltIn: (cmd: string) => boolean;
  customNames: string[];
}): LocalSlashAction {
  const msg = opts.message;
  if (!msg.startsWith("/")) return { kind: "none" };
  const parts = msg.split(/\s+/);
  const cmd = parts[0] || "";
  if (cmd === "/clear" || cmd === "/new") return { kind: "clear_or_new" };
  if (cmd === "/compact") return { kind: "compact" };
  if (cmd === "/model") return { kind: "model" };
  if (cmd === "/help") return { kind: "help" };
  if (!opts.isBuiltIn(cmd)) {
    const customCmdName = cmd.startsWith("/") ? cmd.slice(1) : cmd;
    if (opts.customNames.includes(customCmdName)) {
      return {
        kind: "custom",
        name: customCmdName,
        args: msg.substring(cmd.length).trim(),
      };
    }
  }
  return { kind: "none" };
}
