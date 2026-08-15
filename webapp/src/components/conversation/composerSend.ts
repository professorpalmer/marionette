/**
 * Pure composer / send-path helpers. Conversation.tsx keeps the React wiring.
 */

import type { CommandPaletteActionId } from "../../lib/commandPalette";
import { isPilotMouthBusy } from "./runnersBusy";

/**
 * Enter busy latch — same truth as composerBusy / isPilotMouthBusy.
 * awaiting_swarm does not shut the mouth; Send starts a new turn.
 * Steer stays the piggyback inject until after Send stays Send.
 */
export function composerEnterBusy(opts: {
  turnOpen: boolean;
  status: string;
}): boolean {
  return isPilotMouthBusy(opts.turnOpen, opts.status);
}

/**
 * Enter while busy: Cmd/Ctrl+Enter queues; Alt+Enter interrupts then queues
 * the typed prompt; plain Enter steers/sends. Meta/ctrl wins over alt.
 * Empty composer while busy is a no-op — never invent a steer.
 */
export function composerEnterAction(opts: {
  busy: boolean;
  metaOrCtrl: boolean;
  altKey?: boolean;
  hasText?: boolean;
}): "queue" | "send" | "interrupt" | "noop" {
  if (opts.busy && opts.hasText === false) return "noop";
  if (opts.busy && opts.metaOrCtrl) return "queue";
  if (opts.busy && opts.altKey) return "interrupt";
  return "send";
}

/** Mid-turn steer/interrupt requires typed text. Images-only is a new turn. */
export function shouldSteerWhileBusy(opts: { text: string }): boolean {
  return Boolean(opts.text.trim());
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
    + "\n\nLocal chrome (not sent to the model): `/swarm` `/terminal` `/settings` `/memory` `/mcp` `/files` `/state`."
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

/**
 * Apply /compact (or harness-compact-session) settle paints only while still
 * on the session that started the request — soft-fail so a mid-flight A→B
 * switch never paints A's thinking/receipt into B's transcript.
 */
export function shouldApplyCompactSettle(opts: {
  requestSessionId: string | null;
  activeSessionId: string | null;
}): boolean {
  return opts.requestSessionId === opts.activeSessionId;
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

export function formatInterruptErrorMessage(err: unknown): string {
  const message =
    err && typeof err === "object" && "message" in err
      ? String((err as { message?: unknown }).message || err)
      : String(err || "");
  return "[error] Interrupt failed: " + message;
}

/**
 * Cursor parity: clear the steer composer draft only after a successful
 * submit. Failed steers (4xx / network) keep the operator's text.
 */
export function shouldClearSteerDraftOnResult(ok: boolean): boolean {
  return ok;
}

/**
 * Chrome after POST /api/session/steer. Match the action the harness took —
 * a vision busy-Enter queues a follow-up and must not also paint `steer:`.
 */
export function steerResultChrome(opts: {
  action?: string;
  composerMode?: "send" | "queue" | "interrupt" | "noop";
}): "steer" | "queue" | "interrupt" {
  if (opts.composerMode === "interrupt" || opts.action === "interrupt_then_queue") {
    return "interrupt";
  }
  if (opts.action === "enqueue_prompt") return "queue";
  return "steer";
}

/** Transcript `steer:` / `interrupt:` row — never for a queued follow-up. */
export function steerTranscriptItem(opts: {
  text: string;
  chrome: "steer" | "queue" | "interrupt";
}): { kind: "steer"; text: string; mode?: "steer" | "interrupt" } | null {
  if (opts.chrome === "queue") return null;
  if (opts.chrome === "interrupt") {
    return { kind: "steer", text: opts.text, mode: "interrupt" };
  }
  return { kind: "steer", text: opts.text };
}

export type StopHonestyNotice = {
  message?: string;
  reason?: string;
  count?: number;
};

export type InterruptSessionResponse = {
  ok: boolean;
  notices?: StopHonestyNotice[];
};

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

/** Visible when Stop's backend interrupt fails (parity with edit-rewind honesty). */
export const STOP_INTERRUPT_FAILED_NOTICE =
  "Could not stop the current turn.";

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
  /** True when harness restored workspace files from a turn checkpoint. */
  workspace_restored?: boolean;
  checkpoint_id?: string | null;
  restored_files?: string[];
};

export type EditMessageFlowResult =
  | { kind: "interrupt_failed"; notice: string }
  | { kind: "rewind_failed"; notice: string }
  | {
      kind: "success";
      truncateToIndex: number;
      prefill: string;
      notice: string;
      workspace_restored: boolean;
    };

function editFlowErrorMessage(err: unknown, fallback: string): string {
  if (err && typeof err === "object" && "message" in err) {
    return String((err as { message?: unknown }).message || fallback);
  }
  return fallback;
}

export type StopFlowResult =
  | { kind: "ok"; notices: StopHonestyNotice[] }
  | { kind: "interrupt_failed"; notice: string };

/**
 * Stop: settle local UI chrome, then await backend interrupt.
 * Surfaces a visible notice on interrupt failure (parity with edit-rewind).
 * After a successful interrupt, optionally refresh the live transcript so
 * Stop honesty rows (owned-command orphan / steer drop) appear immediately.
 */
export async function runStopFlow(opts: {
  stopLocal: () => void;
  interruptSession: () => Promise<InterruptSessionResponse>;
  refreshTranscript?: () => Promise<void>;
}): Promise<StopFlowResult> {
  opts.stopLocal();
  try {
    const interruptRes = await opts.interruptSession();
    if (!interruptRes?.ok) {
      return {
        kind: "interrupt_failed",
        notice: STOP_INTERRUPT_FAILED_NOTICE,
      };
    }
    if (opts.refreshTranscript) {
      try {
        await opts.refreshTranscript();
      } catch {
        /* best-effort — notices may still arrive via SSE flush / interrupt body */
      }
    }
    return { kind: "ok", notices: interruptRes.notices || [] };
  } catch (err) {
    return {
      kind: "interrupt_failed",
      notice: editFlowErrorMessage(err, STOP_INTERRUPT_FAILED_NOTICE),
    };
  }
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
  interruptSession: () => Promise<InterruptSessionResponse>;
  rewindSession: (userOrdinal: number) => Promise<RewindSessionResponse>;
}): Promise<EditMessageFlowResult> {
  if (opts.composerBusy) {
    const stopResult = await runStopFlow({
      stopLocal: opts.stopLocal,
      interruptSession: opts.interruptSession,
    });
    if (stopResult.kind === "interrupt_failed") {
      return stopResult;
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
      workspace_restored: Boolean(res.workspace_restored),
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
  | { kind: "clear" }
  | { kind: "new" }
  | { kind: "compact" }
  | { kind: "model" }
  | { kind: "help" }
  | { kind: "swarm" }
  | { kind: "terminal" }
  | { kind: "settings" }
  | { kind: "memory" }
  | { kind: "mcp" }
  | { kind: "files" }
  | { kind: "state" }
  | { kind: "custom"; name: string; args: string };

/**
 * Session-chrome intent for /clear vs /new.
 * /clear resets the visible transcript in place; /new abandons to a new session.
 */
export function localSlashChromeAction(
  action: LocalSlashAction,
): "clear_visible" | "new_session" | null {
  if (action.kind === "clear") return "clear_visible";
  if (action.kind === "new") return "new_session";
  return null;
}

/**
 * Map navigation slash kinds onto Cmd-K palette action ids so Conversation
 * can reuse runCommandPaletteAction (same events as the palette).
 */
export function localSlashPaletteAction(
  action: LocalSlashAction,
): CommandPaletteActionId | null {
  switch (action.kind) {
    case "swarm":
      return "open-swarm";
    case "terminal":
      return "open-terminal";
    case "settings":
      return "open-settings";
    case "memory":
      return "open-memory";
    case "mcp":
      return "open-mcp";
    case "files":
      return "open-files";
    case "state":
      return "open-state";
    default:
      return null;
  }
}

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
  if (cmd === "/clear") return { kind: "clear" };
  if (cmd === "/new") return { kind: "new" };
  if (cmd === "/compact") return { kind: "compact" };
  if (cmd === "/model") return { kind: "model" };
  if (cmd === "/help") return { kind: "help" };
  if (cmd === "/swarm") return { kind: "swarm" };
  if (cmd === "/terminal") return { kind: "terminal" };
  if (cmd === "/settings") return { kind: "settings" };
  if (cmd === "/memory") return { kind: "memory" };
  if (cmd === "/mcp") return { kind: "mcp" };
  if (cmd === "/files") return { kind: "files" };
  if (cmd === "/state") return { kind: "state" };
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
