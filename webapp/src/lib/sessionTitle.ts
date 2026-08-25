import { api } from "./api";
import { isActivityHeadlineText } from "./sessionTitleLock";

/** Matches harness/sessions.py derive_title default. */
export const DEFAULT_SESSION_TITLE = "New session";

/**
 * Derive a short session title from the first user prompt.
 * Mirrors harness.sessions.derive_title so optimistic UI renames match backend.
 * Never accepts Investigating / Explored / Diagnosing / Planning walls —
 * those belong in the activity strip, not session.title.
 */
export function deriveSessionTitle(prompt: string): string {
  if (!prompt) return DEFAULT_SESSION_TITLE;

  const lines = prompt.split(/\r?\n/);
  let firstLine = "";
  for (const line of lines) {
    let cleaned = line.replace(/```[a-zA-Z0-9_\-+]*/g, "");
    cleaned = cleaned.replace(/`/g, "");
    cleaned = cleaned.replace(/[*_~#\-+>]/g, "");
    cleaned = cleaned.split(/\s+/).filter(Boolean).join(" ");
    if (cleaned) {
      firstLine = cleaned;
      break;
    }
  }
  if (!firstLine) return DEFAULT_SESSION_TITLE;
  if (isActivityHeadlineText(firstLine) || isActivityHeadlineText(prompt)) {
    return DEFAULT_SESSION_TITLE;
  }

  const words = firstLine.split(/\s+/);
  const truncatedWords: string[] = [];
  let currentLen = 0;
  for (const w of words) {
    if (truncatedWords.length >= 8) break;
    const addedLen = w.length + (truncatedWords.length ? 1 : 0);
    if (currentLen + addedLen > 48) {
      if (truncatedWords.length === 0) truncatedWords.push(w.slice(0, 48));
      break;
    }
    truncatedWords.push(w);
    currentLen += addedLen;
  }

  let title = truncatedWords.join(" ");
  title = title.replace(/[.,;:?!\- ]+$/, "");
  if (title) {
    title = title[0].toUpperCase() + title.slice(1);
  }
  if (!title || isActivityHeadlineText(title)) return DEFAULT_SESSION_TITLE;
  return title;
}

export function isDefaultSessionTitle(title: string | undefined | null): boolean {
  const trimmed = (title || "").trim();
  return !trimmed || trimmed === DEFAULT_SESSION_TITLE;
}

/**
 * One session row = one human title. Activity headlines / Stopped. never
 * paint as the list title (fall back to Untitled).
 */
export function displaySessionListTitle(title: string | undefined | null): string {
  const trimmed = (title || "").trim();
  if (!trimmed || isActivityHeadlineText(trimmed)) return "Untitled";
  return trimmed;
}

/**
 * On the first real user send, rename a still-default session row optimistically
 * and refresh the left rail. Best-effort — backend set_title_if_default remains
 * authoritative if this races or fails.
 */
export async function renameDefaultSessionIfNeeded(
  sessionId: string,
  prompt: string,
  repoRoot?: string,
): Promise<void> {
  const title = deriveSessionTitle(prompt);
  if (title === DEFAULT_SESSION_TITLE) return;

  try {
    const sessions = await api.sessions(repoRoot || undefined);
    const sess = sessions.find((s) => s.id === sessionId);
    if (!sess || !isDefaultSessionTitle(sess.title)) return;
    // Refuse to stamp an activity-headline wall even if the stored title is
    // still default (guard against bad prompts / mid-turn leakage).
    if (isActivityHeadlineText(title)) return;
    await api.renameSession(sessionId, title);
    window.dispatchEvent(new Event("harness-config-changed"));
  } catch {
    /* best-effort */
  }
}
