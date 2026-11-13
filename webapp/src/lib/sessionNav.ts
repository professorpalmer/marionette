import type { Session } from "./api";
import { isDefaultSessionTitle } from "./sessionTitle";

/** Window events that let shell surfaces (empty session, Cmd+K) drive the
 *  rail, the composer and onboarding without new prop chains. */

export const SWITCH_SESSION_EVENT = "harness-switch-session";
export const COMPOSER_PREFILL_EVENT = "harness-composer-prefill";
export const OPEN_ONBOARDING_EVENT = "harness-open-onboarding";
export const FOCUS_COMPOSER_EVENT = "harness-focus-input";

/** Moves keyboard focus to the message box of the active session. */
export function focusComposer(): void {
  window.dispatchEvent(new Event(FOCUS_COMPOSER_EVENT));
}

/** LeftRail owns the switch (optimistic paint, attach, revert on failure). */
export function requestSessionSwitch(sessionId: string): void {
  window.dispatchEvent(new CustomEvent<{ sessionId: string }>(SWITCH_SESSION_EVENT, { detail: { sessionId } }));
}

/** Puts text in the composer for the user to edit and send. It never sends. */
export function prefillComposer(text: string): void {
  window.dispatchEvent(new CustomEvent<{ text: string }>(COMPOSER_PREFILL_EVENT, { detail: { text } }));
}

export function openOnboarding(): void {
  window.dispatchEvent(new Event(OPEN_ONBOARDING_EVENT));
}

export function eventDetailString(event: Event, key: string): string {
  const detail: unknown = (event as CustomEvent<unknown>).detail;
  if (!detail || typeof detail !== "object") return "";
  const value = (detail as Record<string, unknown>)[key];
  return typeof value === "string" ? value : "";
}

/** "2 min ago", "3 h ago", "Yesterday", "4 d ago". Epoch seconds of the last activity. */
export function sessionAge(atSeconds: number, nowMs = Date.now()): string {
  const seconds = Math.max(0, Math.floor(nowMs / 1000 - atSeconds));
  if (seconds < 60) return "Just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.floor(hours / 24);
  return days === 1 ? "Yesterday" : `${days} d ago`;
}

/** Folder name of a workspace root. The durable Home workspace
 *  (~/.pmharness/home) and an empty root are "Home". */
export function workspaceName(root: string | undefined): string {
  const trimmed = (root || "").replace(/[\\/]+$/, "");
  if (!trimmed || /[\\/]\.pmharness[\\/]home$/i.test(trimmed)) return "Home";
  const parts = trimmed.split(/[\\/]/);
  return parts[parts.length - 1] || "Home";
}

/** Sessions worth reopening, most recently active first: not the current one, not
 *  archived, not an untouched "New session". */
export function recentSessionRows(rows: readonly Session[], currentId: string | null | undefined, limit: number): Session[] {
  return rows
    .filter((row) => row.id !== currentId && !row.archived && !isDefaultSessionTitle(row.title))
    .sort((a, b) => (b.updated ?? b.created ?? 0) - (a.updated ?? a.created ?? 0))
    .slice(0, limit);
}
