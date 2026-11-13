import { isActivityHeadlineText } from "./sessionTitleLock";

/** Window event: refetch the session rows (title, preview, age). */
export const SESSIONS_CHANGED_EVENT = "harness-sessions-changed";

/**
 * One session row = one human title. Activity headlines / Stopped. never
 * paint as the list title (fall back to Untitled).
 */
export function displaySessionListTitle(title: string | undefined | null, userTitled = false): string {
  const trimmed = (title || "").trim();
  if (!trimmed) return "Untitled";
  // A title the user typed is shown as typed, even "Planning Q4 launch".
  if (!userTitled && isActivityHeadlineText(trimmed)) return "Untitled";
  return trimmed;
}

/**
 * After the first send of a session the server names it from the prompt
 * (set_title_if_default, before the turn streams). Refetch the rows so the
 * sidebar shows the new title.
 */
export function refreshSessionRowsAfterFirstSend(delayMs = 1500): void {
  window.setTimeout(() => window.dispatchEvent(new Event(SESSIONS_CHANGED_EVENT)), delayMs);
}
