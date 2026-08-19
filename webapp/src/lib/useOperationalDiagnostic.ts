import { useEffect, useState } from "react";
import {
  getActiveDiagnostic,
  subscribeDiagnostic,
} from "./operationalDiagnosticBus";
import type { OperationalDiagnostic } from "./operationalDiagnostic";

export function useOperationalDiagnostic(): OperationalDiagnostic | null {
  const [diag, setDiag] = useState<OperationalDiagnostic | null>(getActiveDiagnostic);
  useEffect(() => subscribeDiagnostic(setDiag), []);
  return diag;
}
