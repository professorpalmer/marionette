/**
 * Authoritative frontend turn lifecycle + terminal-cause vocabulary.
 *
 * Transport signals (framing kind=done, HTTP/SSE EOF, Electron end, ring
 * replay exhaustion, transcript shape) never prove a completed turn.
 * Only assistant_done with stop_cause=natural, or an honest user Stop,
 * may become settled_complete.
 */

export const TURN_RUNNING = "running";
export const TURN_AWAITING_SWARM = "awaiting_swarm";
export const TURN_SETTLED_COMPLETE = "settled_complete";
export const TURN_SETTLED_INCOMPLETE = "settled_incomplete";
export const TURN_ABORTED = "aborted";
export const TURN_ERROR = "error";
export const TURN_INTERRUPTED = "interrupted";

export type TurnLifecycle =
  | typeof TURN_RUNNING
  | typeof TURN_AWAITING_SWARM
  | typeof TURN_SETTLED_COMPLETE
  | typeof TURN_SETTLED_INCOMPLETE
  | typeof TURN_ABORTED
  | typeof TURN_ERROR
  | typeof TURN_INTERRUPTED;

export const CAUSE_NATURAL = "natural";
export const CAUSE_LENGTH = "length";
export const CAUSE_INCOMPLETE = "incomplete";
export const CAUSE_PROVIDER_EOF = "provider_eof";
export const CAUSE_TRANSPORT_ERROR = "transport_error";
export const CAUSE_TURN_BUDGET = "turn_budget";
export const CAUSE_STEP_CAP = "step_cap";
export const CAUSE_STAGNATION = "stagnation";
export const CAUSE_INVALID_TOOL = "invalid_tool";
export const CAUSE_INTERRUPTED = "interrupted";
export const CAUSE_CANCELLED = "cancelled";
export const CAUSE_CONTENT_FILTER = "content_filter";
export const CAUSE_EMPTY_LOOP = "empty_loop";
export const CAUSE_DRIVER_SWAP = "driver_swap";
export const CAUSE_CONTEXT_OVERFLOW = "context_overflow";
export const CAUSE_UNSPECIFIED = "unspecified";
export const CAUSE_TOOL_CALLS = "tool_calls";

export type TerminalCause =
  | typeof CAUSE_NATURAL
  | typeof CAUSE_LENGTH
  | typeof CAUSE_INCOMPLETE
  | typeof CAUSE_PROVIDER_EOF
  | typeof CAUSE_TRANSPORT_ERROR
  | typeof CAUSE_TURN_BUDGET
  | typeof CAUSE_STEP_CAP
  | typeof CAUSE_STAGNATION
  | typeof CAUSE_INVALID_TOOL
  | typeof CAUSE_INTERRUPTED
  | typeof CAUSE_CANCELLED
  | typeof CAUSE_CONTENT_FILTER
  | typeof CAUSE_EMPTY_LOOP
  | typeof CAUSE_DRIVER_SWAP
  | typeof CAUSE_CONTEXT_OVERFLOW
  | typeof CAUSE_UNSPECIFIED
  | typeof CAUSE_TOOL_CALLS;

export type BusyChromeStatus =
  | "idle"
  | "thinking"
  | "executing"
  | "done"
  | "error"
  | "streaming"
  | "awaiting_swarm";

export type TurnSettle = {
  kind: "settle";
  lifecycle: TurnLifecycle;
  cause: TerminalCause;
  status: BusyChromeStatus;
  turnOpen: boolean;
  /** Operator-facing chip / row. Null when nothing extra should paint. */
  explanation: string | null;
};

export type TurnSettleResult = TurnSettle | { kind: "already_settled" };

const CAUSE_ALIASES: Record<string, TerminalCause> = {
  natural: CAUSE_NATURAL,
  length: CAUSE_LENGTH,
  incomplete: CAUSE_INCOMPLETE,
  provider_eof: CAUSE_PROVIDER_EOF,
  eof: CAUSE_PROVIDER_EOF,
  transport_error: CAUSE_TRANSPORT_ERROR,
  transport: CAUSE_TRANSPORT_ERROR,
  error: CAUSE_TRANSPORT_ERROR,
  turn_budget: CAUSE_TURN_BUDGET,
  step_cap: CAUSE_STEP_CAP,
  stagnation: CAUSE_STAGNATION,
  invalid_tool: CAUSE_INVALID_TOOL,
  "invalid-tool": CAUSE_INVALID_TOOL,
  auto_halt: CAUSE_INCOMPLETE,
  interrupted: CAUSE_INTERRUPTED,
  cancelled: CAUSE_CANCELLED,
  canceled: CAUSE_CANCELLED,
  user_stop: CAUSE_CANCELLED,
  content_filter: CAUSE_CONTENT_FILTER,
  empty_loop: CAUSE_EMPTY_LOOP,
  driver_swap: CAUSE_DRIVER_SWAP,
  context_overflow: CAUSE_CONTEXT_OVERFLOW,
  unspecified: CAUSE_UNSPECIFIED,
  tool_calls: CAUSE_TOOL_CALLS,
  intermediate: CAUSE_TOOL_CALLS,
};

const KNOWN_CAUSES = new Set<string>(Object.values(CAUSE_ALIASES));

/** Map a backend / alias label onto the frontend vocabulary. */
export function canonicalizeTerminalCause(raw: unknown): TerminalCause {
  const text = String(raw || "").trim();
  if (!text) return CAUSE_UNSPECIFIED;
  if (KNOWN_CAUSES.has(text) && text in CAUSE_ALIASES) {
    return CAUSE_ALIASES[text] || (text as TerminalCause);
  }
  const aliased = CAUSE_ALIASES[text] || CAUSE_ALIASES[text.toLowerCase()];
  if (aliased) return aliased;
  return CAUSE_UNSPECIFIED;
}

/**
 * Truthful chip copy. Never mentions context % unless the backend named
 * context overflow as the cause.
 */
export const DIRTY_FINISH_BANNER = "Turn ended without a clean finish.";

export function terminalCauseCopy(cause: TerminalCause): string {
  switch (cause) {
    case CAUSE_NATURAL:
      return "";
    case CAUSE_LENGTH:
      return "Stopped: output length limit.";
    case CAUSE_INCOMPLETE:
      return "Reply incomplete.";
    case CAUSE_PROVIDER_EOF:
      return "Provider stream ended before a clean finish.";
    case CAUSE_TRANSPORT_ERROR:
      return "Connection lost before the turn finished.";
    case CAUSE_TURN_BUDGET:
      return "Reached the output token budget for this turn.";
    case CAUSE_STEP_CAP:
      return "Reached the investigation step limit.";
    case CAUSE_STAGNATION:
      return "Stopped: no new progress.";
    case CAUSE_INVALID_TOOL:
      return "Stopped: invalid tool call.";
    case CAUSE_INTERRUPTED:
      return "Interrupted.";
    case CAUSE_CANCELLED:
      return "Stopped.";
    case CAUSE_CONTENT_FILTER:
      return "Provider refused the response.";
    case CAUSE_EMPTY_LOOP:
      return "No productive reply this turn.";
    case CAUSE_DRIVER_SWAP:
      return "Turn ended for a driver change.";
    case CAUSE_CONTEXT_OVERFLOW:
      return "Stopped: context overflow.";
    case CAUSE_TOOL_CALLS:
      return "Turn ended during tool use.";
    case CAUSE_UNSPECIFIED:
    default:
      return DIRTY_FINISH_BANNER;
  }
}

const QUIET_UNSPECIFIED_WIRE = new Set(["stop", "tool_calls"]);

export function lastWireFinishReason(raw: unknown): string {
  return String(raw || "").trim().toLowerCase();
}

/**
 * Chrome decision for a settled turn. Fail-closed internally (caller keeps
 * CAUSE_UNSPECIFIED). If the last wire reason was stop or tool_calls, stay
 * quiet so Continue can show without a dirty-finish banner.
 */
export function dirtyFinishExplanation(opts: {
  cause: TerminalCause;
  finishReason?: unknown;
}): string | null {
  if (opts.cause !== CAUSE_UNSPECIFIED) {
    const copy = terminalCauseCopy(opts.cause);
    return copy || null;
  }
  // Fail-closed internally (caller keeps unspecified). Never paint the crash
  // banner for stop / tool_calls / blank wire — stay quiet so Continue shows.
  const wire = lastWireFinishReason(opts.finishReason);
  if (QUIET_UNSPECIFIED_WIRE.has(wire) || !wire) return null;
  return null;
}

/** Gate boxed turn_terminal chrome: unspecified crash copy stays unpainted. */
export function suppressUnspecifiedDirtyFinish(cause: unknown, text: unknown): boolean {
  if (canonicalizeTerminalCause(cause) !== CAUSE_UNSPECIFIED) return false;
  return String(text || "").trim() === DIRTY_FINISH_BANNER;
}

export function isNaturalStopCause(cause: TerminalCause): boolean {
  return cause === CAUSE_NATURAL;
}

export function isAuthoritativeComplete(opts: {
  stopCause?: unknown;
  userStopped?: boolean;
}): boolean {
  if (opts.userStopped) return true;
  return isNaturalStopCause(canonicalizeTerminalCause(opts.stopCause));
}

export function recoveryControlsAvailable(lifecycle: TurnLifecycle): boolean {
  return (
    lifecycle === TURN_SETTLED_INCOMPLETE
    || lifecycle === TURN_ABORTED
    || lifecycle === TURN_ERROR
    || lifecycle === TURN_INTERRUPTED
  );
}

/** Visible Continue prompt — never an invisible continuation. */
export const CONTINUE_PROMPT = "Continue from where you left off.";

export type TurnItemLike =
  | { kind: "msg"; msg: { role: string; text?: string; workerStream?: boolean } }
  | { kind: string; [key: string]: unknown };

function itemsInCurrentTurn(items: TurnItemLike[]): TurnItemLike[] {
  let lastUser = -1;
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if (it.kind === "msg" && (it as { msg: { role: string } }).msg.role === "user") {
      lastUser = i;
      break;
    }
  }
  return lastUser >= 0 ? items.slice(lastUser + 1) : items;
}

export function latestUserAsk(items: TurnItemLike[]): string {
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if (it.kind !== "msg") continue;
    const msg = (it as { msg: { role: string; text?: string } }).msg;
    const text = String(msg.text || "").trim();
    if (msg.role === "user" && text && text !== CONTINUE_PROMPT) {
      return text;
    }
  }
  return "";
}

export function latestPartialAssistant(items: TurnItemLike[]): string {
  for (const it of [...itemsInCurrentTurn(items)].reverse()) {
    if (it.kind !== "msg") continue;
    const msg = (it as { msg: { role: string; text?: string; workerStream?: boolean } }).msg;
    if (msg.role !== "assistant" || msg.workerStream) continue;
    const text = String(msg.text || "").trim();
    if (text) return text;
  }
  return "";
}

export function hasPartialAssistantAnswer(items: TurnItemLike[]): boolean {
  return Boolean(latestPartialAssistant(items));
}

export function settleFromAssistantDone(opts: {
  stopCause?: unknown;
  finishReason?: unknown;
  incompleteReason?: unknown;
  liveJobs?: boolean;
}): TurnSettle {
  const cause = canonicalizeTerminalCause(opts.stopCause);
  if (opts.liveJobs) {
    return {
      kind: "settle",
      lifecycle: TURN_AWAITING_SWARM,
      cause: isNaturalStopCause(cause) ? CAUSE_NATURAL : cause,
      status: "awaiting_swarm",
      turnOpen: false,
      explanation: isNaturalStopCause(cause)
        ? null
        : dirtyFinishExplanation({ cause, finishReason: opts.finishReason }),
    };
  }
  if (isNaturalStopCause(cause)) {
    return {
      kind: "settle",
      lifecycle: TURN_SETTLED_COMPLETE,
      cause: CAUSE_NATURAL,
      status: "done",
      turnOpen: false,
      explanation: null,
    };
  }
  return {
    kind: "settle",
    lifecycle: TURN_SETTLED_INCOMPLETE,
    cause,
    status: "done",
    turnOpen: false,
    explanation: dirtyFinishExplanation({ cause, finishReason: opts.finishReason }),
  };
}

export function settleFromTransportEof(opts: {
  turnSettled: boolean;
  userStopped: boolean;
  hasPartialAnswer: boolean;
}): TurnSettleResult {
  if (opts.turnSettled || opts.userStopped) {
    return { kind: "already_settled" };
  }
  if (opts.hasPartialAnswer) {
    return {
      kind: "settle",
      lifecycle: TURN_SETTLED_INCOMPLETE,
      cause: CAUSE_PROVIDER_EOF,
      status: "error",
      turnOpen: false,
      explanation: terminalCauseCopy(CAUSE_PROVIDER_EOF),
    };
  }
  return {
    kind: "settle",
    lifecycle: TURN_ABORTED,
    cause: CAUSE_PROVIDER_EOF,
    status: "error",
    turnOpen: false,
    explanation: terminalCauseCopy(CAUSE_PROVIDER_EOF),
  };
}

export function settleFromFramingDone(opts: {
  turnSettled: boolean;
  hasPartialAnswer: boolean;
}): TurnSettleResult {
  if (opts.turnSettled) return { kind: "already_settled" };
  return settleFromTransportEof({
    turnSettled: false,
    userStopped: false,
    hasPartialAnswer: opts.hasPartialAnswer,
  });
}

export function settleFromUserStop(): TurnSettle {
  return {
    kind: "settle",
    lifecycle: TURN_INTERRUPTED,
    cause: CAUSE_CANCELLED,
    status: "idle",
    turnOpen: false,
    explanation: terminalCauseCopy(CAUSE_CANCELLED),
  };
}

export function settleFromInterrupted(): TurnSettle {
  return {
    kind: "settle",
    lifecycle: TURN_INTERRUPTED,
    cause: CAUSE_INTERRUPTED,
    status: "idle",
    turnOpen: false,
    explanation: terminalCauseCopy(CAUSE_INTERRUPTED),
  };
}

const MODEL_STREAM_ERROR_CAUSES = new Set<TerminalCause>([
  CAUSE_LENGTH,
  CAUSE_INCOMPLETE,
  CAUSE_CONTENT_FILTER,
]);

export function settleFromStreamError(
  errorText?: unknown,
  terminalCause?: unknown,
): TurnSettle {
  const named = canonicalizeTerminalCause(terminalCause);
  if (MODEL_STREAM_ERROR_CAUSES.has(named)) {
    return {
      kind: "settle",
      lifecycle: TURN_SETTLED_INCOMPLETE,
      cause: named,
      status: "done",
      turnOpen: false,
      explanation: terminalCauseCopy(named),
    };
  }
  if (named === CAUSE_PROVIDER_EOF) {
    return {
      kind: "settle",
      lifecycle: TURN_SETTLED_INCOMPLETE,
      cause: CAUSE_PROVIDER_EOF,
      status: "error",
      turnOpen: false,
      explanation: terminalCauseCopy(CAUSE_PROVIDER_EOF),
    };
  }
  const raw = String(errorText || "").trim();
  const explanation = raw
    ? (raw.startsWith("[error]") || raw.startsWith("[aborted]")
      ? raw
      : `[error] ${raw}`)
    : terminalCauseCopy(named === CAUSE_UNSPECIFIED ? CAUSE_TRANSPORT_ERROR : named);
  return {
    kind: "settle",
    lifecycle: TURN_ERROR,
    cause: named === CAUSE_UNSPECIFIED ? CAUSE_TRANSPORT_ERROR : named,
    status: "error",
    turnOpen: false,
    explanation,
  };
}

/** Full-auto halt is never a silent success; lifecycle must leave `running`. */
export function settleFromAutoHalt(reason?: unknown): TurnSettle {
  let cause = canonicalizeTerminalCause(reason);
  if (isNaturalStopCause(cause) || cause === CAUSE_UNSPECIFIED) {
    cause = CAUSE_INCOMPLETE;
  }
  return {
    kind: "settle",
    lifecycle: TURN_SETTLED_INCOMPLETE,
    cause,
    status: "done",
    turnOpen: false,
    explanation: terminalCauseCopy(cause),
  };
}

export function settleFromStaleLocalAbandon(opts: {
  turnSettled: boolean;
  userStopped: boolean;
  hasPartialAnswer: boolean;
}): TurnSettleResult {
  if (opts.userStopped || opts.turnSettled) return { kind: "already_settled" };
  return settleFromTransportEof({
    turnSettled: false,
    userStopped: false,
    hasPartialAnswer: opts.hasPartialAnswer,
  });
}

export function settleFromRingReplayDone(opts: {
  turnSettled: boolean;
  hasPartialAnswer: boolean;
}): TurnSettleResult {
  return settleFromFramingDone(opts);
}

/** Mouth stays busy while a session switch has not resolved B's runner yet. */
export function composerBusyDuringSwitch(opts: {
  switchPending: boolean;
  turnOpen: boolean;
  status: string;
  mouthBusy: boolean;
}): boolean {
  if (opts.switchPending) return true;
  return opts.mouthBusy;
}

export type RecoveryContext = {
  sessionId: string;
  generation: number;
};

export function recoveryDispatchAllowed(opts: {
  composerBusy: boolean;
  dispatching: boolean;
  lifecycle: TurnLifecycle;
  boundSessionId?: string | null;
  activeSessionId?: string | null;
  boundGeneration?: number | null;
  activeGeneration?: number | null;
}): boolean {
  if (opts.composerBusy || opts.dispatching) return false;
  if (!recoveryControlsAvailable(opts.lifecycle)) return false;
  if (opts.boundSessionId) {
    if (!opts.activeSessionId || opts.boundSessionId !== opts.activeSessionId) {
      return false;
    }
  }
  if (
    opts.boundGeneration != null
    && opts.activeGeneration != null
    && opts.boundGeneration !== opts.activeGeneration
  ) {
    return false;
  }
  return true;
}
