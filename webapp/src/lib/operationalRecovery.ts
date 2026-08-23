import type { OperationalDiagnostic } from "./operationalDiagnostic";
import { clearDiagnostic } from "./operationalDiagnosticBus";
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
