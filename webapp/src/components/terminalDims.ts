/** ConPTY / xterm dimension helpers for the built-in Terminal pane. */

export type PtyDims = { cols: number; rows: number };

/**
 * Windows CreatePseudoConsole rejects 0x0. FitAddon can report 0 before the
 * host has layout (collapsed dock / first paint) — fall back to a usable size.
 */
export function safePtyDims(cols: number, rows: number): PtyDims {
  const c = Number.isFinite(cols) ? Math.floor(cols) : 80;
  const r = Number.isFinite(rows) ? Math.floor(rows) : 24;
  return {
    cols: c >= 1 ? c : 80,
    rows: r >= 1 ? r : 24,
  };
}

/** True when the xterm host element has a real layout box. */
export function hostHasLayout(el: HTMLElement | null | undefined): boolean {
  if (!el) return false;
  return el.clientWidth > 0 && el.clientHeight > 0;
}
