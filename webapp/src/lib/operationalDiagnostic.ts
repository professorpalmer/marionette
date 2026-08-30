/**
 * Renderer-owned operational diagnostic contract (issue #74 PR 1).
 *
 * Lifecycle (idle / thinking / error) stays how a turn is progressing.
 * Puppetmaster artifacts stay authoritative for worker outcome quality.
 * This record is only the safe, presentable explanation of an application
 * failure — not a second durable store, not form validation, not a PM taxonomy.
 */

import { getCorrelationId } from "./correlationId";

export type DiagnosticScope =
  | "desktop_bridge"
  | "transport"
  | "backend"
  | "conversation"
  | "workspace"
  | "projects"
  | "sessions"
  | "prompt_queue"
  | "config"
  | "update"
  | "panel";

export type DiagnosticSeverity = "info" | "warning" | "error";

export type DiagnosticRecovery =
  | { kind: "none" }
  | { kind: "retry"; label: string }
  | { kind: "relaunch"; label: string };

export type FailureClass = "operational" | "local";

export type OperationalDiagnostic = {
  id: string;
  scope: DiagnosticScope;
  operation: string;
  code?: string;
  summary: string;
  detail?: string;
  severity: DiagnosticSeverity;
  retryable: boolean;
  /** Undefined when we do not know. Never invent "unsafe". */
  dataSafe?: boolean;
  recovery: DiagnosticRecovery;
  sessionId?: string;
  repo?: string;
  jobId?: string;
  taskId?: string;
  /** Request correlation id for support / log cross-reference. */
  correlationId?: string;
  createdAt: number;
};

export const DESKTOP_BRIDGE_MISSING = "desktop_bridge_missing";
export const TRANSPORT_HTTP = "transport_http";
export const TRANSPORT_IPC = "transport_ipc";
export const TRANSPORT_UNCERTAIN = "transport_uncertain";
export const TRANSPORT_BUSY = "transport_busy";
export const BACKEND_NOT_READY = "backend_not_ready";
export const BACKEND_WARNING = "backend_warning";
export const AUTH_FAILURE = "provider_auth_failure";
export const CONVERSATION_TURN_FAILURE = "conversation_turn_failure";

const SUMMARY_MAX = 160;
const DETAIL_MAX = 280;

const SECRET_LIKE =
  /(?:bearer\s+[a-z0-9._\-+=\/]+|sk-[a-z0-9]{8,}|api[_-]?key\s*[:=]\s*\S+|x-harness-token\s*[:=]\s*\S+|authorization\s*[:=]\s*\S+|AIza[0-9A-Za-z_\-]{10,}|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)/gi;

let nextId = 0;

function newDiagnosticId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  nextId += 1;
  return `diag-${nextId}`;
}

/** Form, upload, and field validation stay beside their controls. */
export function classifyFailure(input: {
  kind?: "validation" | "upload" | "form" | "operational";
  scope?: DiagnosticScope;
}): FailureClass {
  if (input.kind === "validation" || input.kind === "upload" || input.kind === "form") {
    return "local";
  }
  return "operational";
}

export function isOperationalDiagnostic(value: unknown): value is OperationalDiagnostic {
  if (!value || typeof value !== "object") return false;
  const d = value as OperationalDiagnostic;
  return Boolean(d.id && d.scope && d.operation && d.summary && d.severity && d.recovery);
}

export function sanitizeDiagnosticText(text: string, max = SUMMARY_MAX): string {
  const redacted = String(text || "").replace(/\r/g, "").replace(SECRET_LIKE, "[redacted]");
  if (redacted.length <= max) return redacted;
  return redacted.slice(0, Math.max(0, max - 1)).trimEnd() + "…";
}

export function desktopShellExpected(env?: {
  userAgent?: string;
  shellFlag?: boolean;
}): boolean {
  if (env?.shellFlag) return true;
  const ua = env?.userAgent ?? (typeof navigator !== "undefined" ? navigator.userAgent : "");
  return /Electron/i.test(ua);
}

export function desktopBridgeMissing(env?: {
  userAgent?: string;
  shellFlag?: boolean;
  hasBridge?: boolean;
}): boolean {
  const expected = desktopShellExpected(env);
  const hasBridge = env?.hasBridge ?? (
    typeof window !== "undefined" && !!(window as { harnessIPC?: unknown }).harnessIPC
  );
  return expected && !hasBridge;
}

export function createOperationalDiagnostic(
  input: Omit<OperationalDiagnostic, "id" | "createdAt" | "summary" | "detail" | "recovery"> & {
    summary: string;
    detail?: string;
    recovery?: DiagnosticRecovery;
    id?: string;
    createdAt?: number;
  },
): OperationalDiagnostic {
  const recovery = input.recovery || { kind: "none" };
  const correlationId = input.correlationId ?? (getCorrelationId() || undefined);
  return {
    ...input,
    id: input.id || newDiagnosticId(),
    createdAt: input.createdAt ?? Date.now(),
    summary: sanitizeDiagnosticText(input.summary, SUMMARY_MAX),
    detail: input.detail ? sanitizeDiagnosticText(input.detail, DETAIL_MAX) : undefined,
    recovery,
    correlationId,
  };
}

/** One root cause for the v0.9.249–v0.9.252 sandboxed-preload crash. */
export function desktopBridgeMissingDiagnostic(opts?: {
  operation?: string;
  sessionId?: string;
  repo?: string;
}): OperationalDiagnostic {
  return createOperationalDiagnostic({
    scope: "desktop_bridge",
    operation: opts?.operation || "preload",
    code: DESKTOP_BRIDGE_MISSING,
    summary: "Desktop bridge is missing",
    detail:
      "The Electron preload did not expose harnessIPC. Backend data can still be intact; panels fail independently until the shell is relaunched.",
    severity: "error",
    retryable: false,
    dataSafe: true,
    recovery: { kind: "relaunch", label: "Relaunch Marionette" },
    sessionId: opts?.sessionId,
    repo: opts?.repo,
  });
}

export function fromTransportFailure(input: {
  operation: string;
  err?: unknown;
  path?: string;
  isTransient?: boolean;
  hasBridge?: boolean;
  userAgent?: string;
  shellFlag?: boolean;
  sessionId?: string;
  repo?: string;
}): OperationalDiagnostic {
  if (desktopBridgeMissing(input)) {
    return desktopBridgeMissingDiagnostic({
      operation: input.operation,
      sessionId: input.sessionId,
      repo: input.repo,
    });
  }
  const err = input.err as { message?: string; code?: string; status?: number } | undefined;
  const raw = String(err?.message || err || "request failed");
  if (input.isTransient) {
    return createOperationalDiagnostic({
      scope: "transport",
      operation: input.operation,
      code: TRANSPORT_UNCERTAIN,
      summary: "Backend connection is uncertain",
      detail: sanitizeDiagnosticText(raw, DETAIL_MAX),
      severity: "warning",
      retryable: true,
      recovery: { kind: "retry", label: "Retry" },
      sessionId: input.sessionId,
      repo: input.repo,
    });
  }
  const viaIpc = input.hasBridge ?? (
    typeof window !== "undefined" && !!(window as { harnessIPC?: unknown }).harnessIPC
  );
  return createOperationalDiagnostic({
    scope: "transport",
    operation: input.operation,
    code: viaIpc ? TRANSPORT_IPC : TRANSPORT_HTTP,
    summary: "Request failed",
    detail: sanitizeDiagnosticText(input.path ? `${input.path}: ${raw}` : raw, DETAIL_MAX),
    severity: "error",
    retryable: true,
    recovery: { kind: "retry", label: "Retry" },
    sessionId: input.sessionId,
    repo: input.repo,
  });
}

type BackendDiagnosticWire = {
  scope?: DiagnosticScope;
  operation?: string;
  code?: string;
  summary?: string;
  detail?: string;
  severity?: DiagnosticSeverity;
  retryable?: unknown;
  dataSafe?: boolean;
  recovery?: DiagnosticRecovery;
  sessionId?: string;
  repo?: string;
  jobId?: string;
  taskId?: string;
  id?: string;
  createdAt?: number;
  correlation_id?: string;
};

/** Parse GET /api/diagnostics diagnostic payload into the renderer contract. */
const DIAGNOSTIC_SCOPES = new Set<DiagnosticScope>([
  "desktop_bridge",
  "transport",
  "backend",
  "conversation",
  "workspace",
  "projects",
  "sessions",
  "prompt_queue",
  "config",
  "update",
  "panel",
]);
const DIAGNOSTIC_SEVERITIES = new Set<DiagnosticSeverity>(["info", "warning", "error"]);

function parseWireBool(value: unknown): boolean {
  if (typeof value === "boolean") return value;
  if (typeof value === "number") return value === 1;
  const text = String(value ?? "").trim().toLowerCase();
  return text === "1" || text === "true" || text === "yes" || text === "on";
}

export function fromBackendDiagnostic(
  wire: BackendDiagnosticWire | null | undefined,
): OperationalDiagnostic | null {
  if (!wire || !wire.summary || !wire.scope || !wire.operation || !wire.severity) {
    return null;
  }
  if (!DIAGNOSTIC_SCOPES.has(wire.scope as DiagnosticScope)) return null;
  if (!DIAGNOSTIC_SEVERITIES.has(wire.severity as DiagnosticSeverity)) return null;
  return createOperationalDiagnostic({
    id: wire.id,
    scope: wire.scope as DiagnosticScope,
    operation: wire.operation,
    code: wire.code,
    summary: wire.summary,
    detail: wire.detail,
    severity: wire.severity as DiagnosticSeverity,
    retryable: parseWireBool(wire.retryable),
    dataSafe: wire.dataSafe,
    recovery: wire.recovery,
    sessionId: wire.sessionId,
    repo: wire.repo,
    jobId: wire.jobId,
    taskId: wire.taskId,
    createdAt: wire.createdAt,
    correlationId: wire.correlation_id,
  });
}

/** Settled conversation turn failure — Trace + Retry in ConversationHeader. */
export function conversationTurnFailureDiagnostic(
  summary: string,
  opts?: { sessionId?: string; detail?: string },
): OperationalDiagnostic {
  const text = sanitizeDiagnosticText(
    String(summary || "").replace(/^\[(?:error|aborted)\]\s*/i, "").trim() || "Turn failed",
    SUMMARY_MAX,
  );
  return createOperationalDiagnostic({
    scope: "conversation",
    operation: "turn",
    code: CONVERSATION_TURN_FAILURE,
    summary: text,
    detail: opts?.detail,
    severity: "error",
    retryable: true,
    recovery: { kind: "retry", label: "Retry" },
    sessionId: opts?.sessionId,
  });
}

export function isConversationTurnFailureDiagnostic(
  diag: Pick<OperationalDiagnostic, "code" | "scope" | "operation"> | null | undefined,
): boolean {
  return Boolean(
    diag
    && diag.scope === "conversation"
    && diag.operation === "turn"
    && diag.code === CONVERSATION_TURN_FAILURE,
  );
}

/** Loud provider auth rejection surfaced in the transcript. */
export function authFailureDiagnostic(message: string, opts?: {
  sessionId?: string;
  jobId?: string;
}): OperationalDiagnostic {
  return createOperationalDiagnostic({
    scope: "conversation",
    operation: "provider_auth",
    code: AUTH_FAILURE,
    summary: "Provider auth failure",
    detail: sanitizeDiagnosticText(message, DETAIL_MAX),
    severity: "error",
    retryable: true,
    recovery: { kind: "retry", label: "Fix key and retry" },
    sessionId: opts?.sessionId,
    jobId: opts?.jobId,
  });
}

export function sameRoot(
  a: Pick<OperationalDiagnostic, "id" | "code" | "scope" | "operation">,
  b: Pick<OperationalDiagnostic, "id" | "code" | "scope" | "operation">,
): boolean {
  if (a.id && b.id && a.id === b.id) return true;
  return Boolean(a.code && a.code === b.code && a.scope === b.scope && a.operation === b.operation);
}

export function belongsToActiveScope(
  diag: OperationalDiagnostic,
  active: { sessionId?: string; repo?: string },
): boolean {
  if (diag.sessionId && active.sessionId && diag.sessionId !== active.sessionId) return false;
  if (diag.repo && active.repo && diag.repo !== active.repo) return false;
  return true;
}

export function isUncertainTransport(diag: OperationalDiagnostic): boolean {
  return diag.code === TRANSPORT_UNCERTAIN || diag.code === TRANSPORT_BUSY;
}

/** Busy or uncertain transport must not erase a known failure. */
export function nextDiagnostic(
  current: OperationalDiagnostic | null,
  incoming: OperationalDiagnostic | null,
): OperationalDiagnostic | null {
  if (!incoming) return current;
  if (!current) return incoming;
  if (isUncertainTransport(incoming) && !isUncertainTransport(current)) return current;
  return incoming;
}

/** A successful retry clears only the diagnostic it repaired. */
export function resolveRepaired(
  current: OperationalDiagnostic | null,
  repaired: Pick<OperationalDiagnostic, "id" | "code" | "scope" | "operation">,
): OperationalDiagnostic | null {
  if (!current) return null;
  return sameRoot(current, repaired) ? null : current;
}

export function isReadinessDiagnostic(
  diag: OperationalDiagnostic | null | undefined,
): boolean {
  return Boolean(diag && (diag.scope === "desktop_bridge" || diag.scope === "backend"));
}

/** Shared readiness surfaces reuse one root summary instead of inventing local causes. */
export function sharedReadinessNotice(
  fallback: string,
  diag: OperationalDiagnostic | null | undefined,
): string {
  return panelNotice(fallback, diag);
}

/** Panel operational copy: readiness root wins; matching scope wins; else fallback. */
export function panelNotice(
  fallback: string,
  diag: OperationalDiagnostic | null | undefined,
  scope?: DiagnosticScope,
): string {
  if (diag && isReadinessDiagnostic(diag)) return diag.summary;
  if (scope && diag && diag.scope === scope) return diag.summary;
  return fallback;
}

/** Failed-turn lifecycle is settled; the diagnostic owns the explanation. */
export function conversationLifecycleAfterFailure(): "idle" {
  return "idle";
}
