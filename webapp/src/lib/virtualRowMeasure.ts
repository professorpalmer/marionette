import type { Virtualizer } from "@tanstack/react-virtual";

/** Measure a virtual row right after React's commit, still before paint.
 *
 * Rows measure from layout effects, which run inside the commit. A first
 * measurement of a row above the fold makes the virtualizer adjust scrollTop and
 * flushSync its re-render, which React refuses there ("flushSync was called from
 * inside a lifecycle method") and postpones to the end of the commit anyway. A
 * microtask runs at that same point, so the same-paint adjustment still lands. */
export function measureAfterCommit<T extends Element>(virtualizer: Virtualizer<HTMLDivElement, T>, element: T): void {
  queueMicrotask(() => {
    if (element.isConnected) virtualizer.measureElement(element);
  });
}
