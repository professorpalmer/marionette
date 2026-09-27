import { createOperationalDiagnostic, fromTransportFailure, type OperationalDiagnostic } from "./operationalDiagnostic";
import { clearDiagnostic, publishDiagnostic } from "./operationalDiagnosticBus";
import { isTransientHarnessConnError } from "./transport";

export type TransportFailureContext = {
  operation: string;
  path?: string;
  sessionId?: string;
  repo?: string;
  failureKind?: "action" | "operational";
};

export function publishTransportFailure(
  err: unknown,
  ctx: TransportFailureContext,
): void {
  if (err instanceof Error && "code" in err && err.code === "ENDPOINT_RECONNECT_REQUIRED") {
    publishDiagnostic(createOperationalDiagnostic({
      scope: "transport", operation: ctx.operation, code: "ENDPOINT_RECONNECT_REQUIRED",
      summary: "Backend changed. Reconnect, then retry your action.", detail: err.message,
      severity: "warning", retryable: true, recovery: {kind:"retry", label:"Reconnect"},
      sessionId: ctx.sessionId, repo: ctx.repo,
    }));
    return;
  }
  // Input failures stay composer-local (inputFailureMessage); codes/status are
  // preserved on the sanitized stream/error object and in electron.log.
  if (isInputFailure(err)) return;
  // A session switch supersedes in-flight reads; their 409 is not a failure.
  if (isSupersededBySwitch(err)) return;
  // Only a recognized action with a structured backend reason stays local.
  // Unknown statuses and malformed responses still report operational failure.
  if (ctx.failureKind === "action" && isLocalActionFailure(err)) return;
  const diag = fromTransportFailure({
    operation: ctx.operation,
    path: ctx.path,
    err,
    isTransient: isTransientHarnessConnError(err),
    sessionId: ctx.sessionId,
    repo: ctx.repo,
  });
  publishDiagnostic(diag);
}

export function clearTransportFailure(
  repaired: Pick<OperationalDiagnostic, "id" | "code" | "scope" | "operation">,
): void {
  clearDiagnostic(repaired);
}

function isLocalActionFailure(err: unknown): boolean {
  if (!(err instanceof Error) || !("status" in err) || !("body" in err)) return false;
  const body = err.body;
  if (!body || typeof body !== "object" || Array.isArray(body)
    || !("error" in body) || typeof body.error !== "string" || !body.error.trim()) return false;
  if (err.status === 400 || err.status === 409 || err.status === 422) return true;
  return err.status === 503 && "code" in body
    && (body.code === "queue_write_failed" || body.code === "queue_read_failed");
}

function isInputFailure(err: unknown): boolean {
  if (!err || typeof err !== "object") return false;
  const body = "body" in err ? err.body : err;
  return !!body && typeof body === "object" && "code" in body
    && typeof body.code === "string" && body.code.startsWith("input_");
}

const SWITCH_RACE_CODES = new Set(["session_changed", "view_changed", "input_session_changed"]);

function isSupersededBySwitch(err: unknown): boolean {
  if (!(err instanceof Error) || !("status" in err) || err.status !== 409 || !("body" in err)) return false;
  const body = err.body;
  if (!body || typeof body !== "object") return false;
  if ("code" in body && typeof body.code === "string" && SWITCH_RACE_CODES.has(body.code)) return true;
  return "error" in body && body.error === "session changed or missing";
}
