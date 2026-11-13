/**
 * Whether anything is working right now: a session runner (shared state feed)
 * or the open turn in the chat. Pollers for state that only changes while
 * something runs use this to slow down when the app is idle.
 */
import { useSyncExternalStore } from "react";

type Listener = (busy: boolean) => void;

/** Idle interval for pollers whose state only changes while something runs. */
export const IDLE_POLL_MS = 15_000;

const sources = new Map<string, boolean>();
const listeners = new Set<Listener>();
const sourceListeners = new Set<() => void>();

export function appBusy(): boolean {
  for (const busy of sources.values()) if (busy) return true;
  return false;
}

/** Report one source's state; listeners hear only idle <-> busy transitions. */
export function setActivity(source: string, busy: boolean): void {
  if ((sources.get(source) ?? false) === busy) return;
  const before = appBusy();
  sources.set(source, busy);
  const after = appBusy();
  if (after !== before) for (const listener of [...listeners]) listener(after);
  for (const listener of [...sourceListeners]) listener();
}

function subscribeSources(listener: () => void): () => void {
  sourceListeners.add(listener);
  return () => { sourceListeners.delete(listener); };
}

/** One source's state as React state ("turn": the open chat turn shows Stop). */
export function useActivitySource(source: string): boolean {
  return useSyncExternalStore(subscribeSources, () => sources.get(source) === true, () => false);
}

export function subscribeActivity(listener: Listener): () => void {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
}

export function _resetActivityForTests(): void {
  sources.clear();
  listeners.clear();
  sourceListeners.clear();
}
