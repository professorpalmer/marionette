import { isProviderFailureWaitHint } from "./composerWaitHint";
import {
  conversationTurnFailureDiagnostic,
  isConversationTurnFailureDiagnostic,
  isUncertainTransport,
  type OperationalDiagnostic,
} from "./operationalDiagnostic";
import {
  clearDiagnostic,
  getActiveDiagnostic,
  publishDiagnostic,
} from "./operationalDiagnosticBus";
import {
  TURN_ABORTED,
  TURN_ERROR,
  TURN_SETTLED_COMPLETE,
  type TurnSettle,
} from "./turnTerminal";
import { getHarnessIpc } from "./transport";

export async function executeDiagnosticRecovery(
  diag: OperationalDiagnostic,
  retry: () => void | Promise<void>,
): Promise<void> {
  if (diag.recovery.kind === "retry") {
    await Promise.resolve(retry());
    return;
  }
  if (diag.recovery.kind === "relaunch") {
    const bridge = getHarnessIpc();
    if (bridge?.restart) {
      await bridge.restart();
      return;
    }
    window.location.reload();
  }
}

export function clearDiagnosticAfterSuccess(
  diag: OperationalDiagnostic | null | undefined,
): void {
  if (!diag) return;
  clearDiagnostic(diag);
}

/** Publish or clear the settled turn-failure diagnostic — never from wait notices. */
export function syncConversationTurnFailureDiagnostic(
  settle: TurnSettle,
  sessionId?: string,
): void {
  const explanation = String(settle.explanation || "").trim();
  // Sidecar driver miss is a wait notice, not Trace/Retry.
  if (isProviderFailureWaitHint(explanation)) {
    return;
  }
  const failed =
    settle.status === "error"
    || settle.lifecycle === TURN_ERROR
    || settle.lifecycle === TURN_ABORTED;
  if (failed) {
    publishDiagnostic(conversationTurnFailureDiagnostic(explanation || "Turn failed", { sessionId }));
    return;
  }
  if (settle.lifecycle === TURN_SETTLED_COMPLETE || settle.status === "done") {
    const active = getActiveDiagnostic();
    if (isConversationTurnFailureDiagnostic(active)) {
      clearDiagnostic(active!);
      return;
    }
    if (active && isUncertainTransport(active)) {
      clearDiagnostic(active);
    }
  }
}
