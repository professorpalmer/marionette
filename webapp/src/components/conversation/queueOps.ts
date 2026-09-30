/**
 * Pure reorder helpers for the server prompt queue UI.
 */

/** Reorder by drag-drop: remove from `from` and insert at `to`. */
export function reorderByDrag<T>(items: T[], from: number, to: number): T[] {
  if (from === to) return items;
  if (from < 0 || from >= items.length) return items;
  if (to < 0 || to >= items.length) return items;
  const next = [...items];
  const [dragged] = next.splice(from, 1);
  next.splice(to, 0, dragged);
  return next;
}

/** Drop stale playlist rows immediately on session switch (before refresh). */
export function blankQueueItemsOnSessionSwitch(): [] {
  return [];
}

/**
 * Apply a queueList result only when it still matches the active session and
 * the latest fetch generation (soft-fail: never paint A onto B).
 */
export function shouldApplyQueueRefresh(opts: {
  requestSessionId: string | null;
  activeSessionId: string | null;
  requestGen: number;
  currentGen: number;
}): boolean {
  return (
    opts.requestGen === opts.currentGen
    && opts.requestSessionId === opts.activeSessionId
  );
}

/** A hop in flight is not a queue failure. Drop the foreign row; do not paint. */
export function applyQueueListIdentity(
  res: { session_id?: string },
  requestSessionId: string | null,
): "apply" | "drop" {
  const returned = (res.session_id || "").trim();
  const requested = (requestSessionId || "").trim();
  if (requested && returned && returned !== requested) return "drop";
  return "apply";
}

export const QUEUE_LOAD_FAIL_NOTICE = "Couldn’t refresh prompt queue";

