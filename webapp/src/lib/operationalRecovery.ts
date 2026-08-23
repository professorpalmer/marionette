import {
  conversationTurnFailureDiagnostic,
  isConversationTurnFailureDiagnostic,
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
  if (settle.lifecycle === TURN_ERROR || settle.lifecycle === TURN_ABORTED) {
    const summary = String(settle.explanation || "Turn failed").trim();
    publishDiagnostic(conversationTurnFailureDiagnostic(summary, { sessionId }));
    return;
  }
  if (settle.lifecycle === TURN_SETTLED_COMPLETE) {
    const active = getActiveDiagnostic();
    if (isConversationTurnFailureDiagnostic(active)) {
      clearDiagnostic(active!);
    }
  }
}
