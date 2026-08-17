/**
 * Per-session bodies for `@terminal:` composer mentions.
 * Mentions live in the draft; this map is the payload expanded on send.
 */

const terminalSelectionsBySessionId = new Map<string, Record<string, string>>();

function cacheKey(sessionId: string): string {
  return sessionId || "_draft";
}

/** Test helper: drop every cached selection. */
export function clearTerminalSelectionCache(): void {
  terminalSelectionsBySessionId.clear();
}

export function peekTerminalSelections(sessionId: string): Record<string, string> {
  return { ...(terminalSelectionsBySessionId.get(cacheKey(sessionId)) || {}) };
}

export function putTerminalSelection(sessionId: string, label: string, text: string): void {
  const key = cacheKey(sessionId);
  const tag = String(label || "").trim();
  const body = String(text || "").trim();
  if (!tag || !body) return;
  const prev = terminalSelectionsBySessionId.get(key) || {};
  terminalSelectionsBySessionId.set(key, { ...prev, [tag]: body });
}

export function dropTerminalLabels(sessionId: string, labels: string[]): void {
  const key = cacheKey(sessionId);
  const prev = terminalSelectionsBySessionId.get(key);
  if (!prev) return;
  const next = { ...prev };
  for (const label of labels) {
    delete next[label];
  }
  if (Object.keys(next).length === 0) terminalSelectionsBySessionId.delete(key);
  else terminalSelectionsBySessionId.set(key, next);
}
