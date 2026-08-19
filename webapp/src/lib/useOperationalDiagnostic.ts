import { useEffect, useState } from "react";
import {
  getActiveDiagnostic,
  subscribeDiagnostic,
} from "./operationalDiagnosticBus";
import {
  isReadinessDiagnostic,
  panelNotice,
  type DiagnosticScope,
  type OperationalDiagnostic,
} from "./operationalDiagnostic";

export function useOperationalDiagnostic(): OperationalDiagnostic | null {
  const [diag, setDiag] = useState<OperationalDiagnostic | null>(getActiveDiagnostic);
  useEffect(() => subscribeDiagnostic(setDiag), []);
  return diag;
}

/** Operational error text for a panel. Readiness root replaces local copy. */
export function usePanelNotice(
  fallback: string | null | undefined,
  scope?: DiagnosticScope,
): string | null {
  const diag = useOperationalDiagnostic();
  if (isReadinessDiagnostic(diag) || (scope && diag?.scope === scope)) {
    return panelNotice(fallback || "", diag, scope);
  }
  return fallback ?? null;
}
