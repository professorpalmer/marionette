import type { SessionTodoSnapshot } from "./api";

let snapshot: SessionTodoSnapshot = { phases: [] };
let sessionId = "";
let version = 0;
const listeners = new Set<() => void>();

function emit(): void {
  version += 1;
  listeners.forEach((fn) => fn());
}

export function publishSessionTodos(
  next: SessionTodoSnapshot | null | undefined,
  nextSessionId = "",
): void {
  const sid = String(nextSessionId || "").trim();
  const phases = Array.isArray(next?.phases) ? next.phases : [];
  snapshot = {
    op: next?.op ?? null,
    phases,
    storage: next?.storage || "session",
    next: next?.next ?? null,
  };
  if (sid) sessionId = sid;
  emit();
}

export function clearSessionTodos(): void {
  snapshot = { phases: [] };
  sessionId = "";
  emit();
}

export function getSessionTodos(): SessionTodoSnapshot {
  return snapshot;
}

export function getSessionTodosSessionId(): string {
  return sessionId;
}

export function getSessionTodosVersion(): number {
  return version;
}

export function subscribeSessionTodos(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
