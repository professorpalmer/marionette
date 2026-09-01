/** Deterministic decisions for terminal stream lifecycle frames. */
export type TerminalBareOnDoneAction = "noop" | "mark_exited" | "auto_recover" | "reattach";
export type TerminalStreamEvent =
  | { kind: "data"; b64: string; offset?: number }
  | { kind: "process_exit"; offset?: number; error?: string }
  | { kind: "missing_session"; offset?: number; error?: string }
  | { kind: "stream_error"; offset?: number; error?: string }
  | { kind: "legacy_exit"; offset?: number; error?: string }
  | { kind: "unknown"; offset?: number };

const MAX_NOTICE = 240;

/** Decode both the old kind:exit frame and the new explicit PTY lifecycle frames. */
export function decodeTerminalStreamEvent(value: unknown): TerminalStreamEvent {
  if (!value || typeof value !== "object") return { kind: "unknown" };
  const raw = value as Record<string, unknown>;
  const kind = typeof raw.kind === "string" ? raw.kind : "";
  const offset = typeof raw.offset === "number" && Number.isFinite(raw.offset) ? raw.offset : undefined;
  const error = typeof raw.error === "string" ? raw.error : undefined;
  if (kind === "data" && typeof raw.b64 === "string") return { kind, b64: raw.b64, offset };
  if (kind === "process_exit" || kind === "exit") return { kind: kind === "exit" ? "legacy_exit" : kind, offset, error };
  if (kind === "missing_session") return { kind, offset, error };
  if (kind === "stream_error") return { kind, offset, error };
  return { kind: "unknown", offset };
}

/** Only backend-redacted text is displayed, and never without a hard bound. */
export function terminalNotice(error?: string): string {
  if (!error) return "";
  return error.slice(0, MAX_NOTICE);
}

export function terminalMissingSessionAction(alreadyRecovered: boolean): "auto_recover" | "mark_exited" {
  return alreadyRecovered ? "mark_exited" : "auto_recover";
}

export function terminalBareOnDoneAction(opts: {
  disposed: boolean; sawExit: boolean; hasSession: boolean; sawOutput: boolean; autoRecovered: boolean;
}): TerminalBareOnDoneAction {
  if (opts.disposed) return "noop";
  if (opts.sawExit) return "mark_exited";
  if (!opts.hasSession) return "mark_exited";
  if (!opts.sawOutput) return opts.autoRecovered ? "mark_exited" : "auto_recover";
  return "reattach";
}
