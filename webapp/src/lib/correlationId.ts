/** Client-side correlation id threaded on harness HTTP requests. */

let activeCorrelationId = "";

export function newCorrelationId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  activeCorrelationId = `corr-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
  return activeCorrelationId;
}

export function getCorrelationId(): string {
  return activeCorrelationId;
}

export function setCorrelationId(id: string): void {
  activeCorrelationId = String(id || "").trim();
}

export function correlationHeaders(): Record<string, string> {
  if (!activeCorrelationId) activeCorrelationId = newCorrelationId();
  return { "X-Correlation-Id": activeCorrelationId };
}
