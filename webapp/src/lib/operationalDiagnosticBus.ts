/**
 * In-memory renderer diagnostic. Not durable — transcripts, logs, and
 * Puppetmaster artifacts remain the authorities for their own records.
 */
import {
  nextDiagnostic,
  resolveRepaired,
  type OperationalDiagnostic,
} from "./operationalDiagnostic";

let current: OperationalDiagnostic | null = null;
const listeners = new Set<(diag: OperationalDiagnostic | null) => void>();

function emit(): void {
  for (const listener of listeners) listener(current);
}

export function getActiveDiagnostic(): OperationalDiagnostic | null {
  return current;
}

export function publishDiagnostic(incoming: OperationalDiagnostic | null): OperationalDiagnostic | null {
  current = nextDiagnostic(current, incoming);
  emit();
  return current;
}

export function clearDiagnostic(
  repaired?: Pick<OperationalDiagnostic, "id" | "code" | "scope" | "operation">,
): OperationalDiagnostic | null {
  current = repaired ? resolveRepaired(current, repaired) : null;
  emit();
  return current;
}

export function resetDiagnosticBus(): void {
  current = null;
  emit();
}

export function subscribeDiagnostic(
  listener: (diag: OperationalDiagnostic | null) => void,
): () => void {
  listeners.add(listener);
  listener(current);
  return () => {
    listeners.delete(listener);
  };
}
