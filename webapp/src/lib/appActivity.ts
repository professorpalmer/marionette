/**
 * Whether anything is working right now: a session runner (shared state feed)
 * or the open turn in the chat. Pollers for state that only changes while
 * something runs use this to slow down when the app is idle.
 */
type Listener = (busy: boolean) => void;

/** Idle interval for pollers whose state only changes while something runs. */
export const IDLE_POLL_MS = 15_000;

const sources = new Map<string, boolean>();
const listeners = new Set<Listener>();

export function appBusy(): boolean {
  for (const busy of sources.values()) if (busy) return true;
  return false;
}

/** Report one source's state; listeners hear only idle <-> busy transitions. */
export function setActivity(source: string, busy: boolean): void {
  const before = appBusy();
  sources.set(source, busy);
  const after = appBusy();
  if (after !== before) for (const listener of [...listeners]) listener(after);
}

export function subscribeActivity(listener: Listener): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function _resetActivityForTests(): void {
  sources.clear();
  listeners.clear();
}
