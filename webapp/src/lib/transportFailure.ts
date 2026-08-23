import { fromTransportFailure } from "./operationalDiagnostic";
import { clearDiagnostic, publishDiagnostic } from "./operationalDiagnosticBus";
import { isTransientHarnessConnError } from "./transport";

export type TransportFailureContext = {
  operation: string;
  path?: string;
  sessionId?: string;
  repo?: string;
};

export function publishTransportFailure(
  err: unknown,
  ctx: TransportFailureContext,
): void {
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
  repaired: Pick<import("./operationalDiagnostic").OperationalDiagnostic, "id" | "code" | "scope" | "operation">,
): void {
  clearDiagnostic(repaired);
}
