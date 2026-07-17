/**
 * Pure reorder helpers for the local msgQueue and server prompt queue UIs.
 */

/** Move an item up or down by one slot (no-op at bounds). */
export function moveItem<T>(
  items: T[],
  index: number,
  direction: "up" | "down",
): T[] {
  const targetIndex = direction === "up" ? index - 1 : index + 1;
  if (targetIndex < 0 || targetIndex >= items.length) return items;
  const next = [...items];
  const temp = next[index];
  next[index] = next[targetIndex];
  next[targetIndex] = temp;
  return next;
}

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
