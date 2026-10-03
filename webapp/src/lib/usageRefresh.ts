/**
 * Ask usage readers (footer, Economics pane) to refresh.
 *
 * Requests inside one window collapse into a single event: a session switch
 * replays every past tool result through the stream handler, and each used to
 * dispatch its own refresh.
 */
export const USAGE_REFRESH_WINDOW_MS = 150;

let pending: { target: Window; timer: number } | undefined;

export function requestUsageRefresh(): void {
  if (pending !== undefined) return;
  // Dispatch on the window that scheduled the refresh: the global can be gone
  // by the time the timer fires (a torn-down test environment).
  const target = window;
  pending = {
    target,
    timer: target.setTimeout(() => {
      pending = undefined;
      target.dispatchEvent(new Event("harness-usage-refresh"));
    }, USAGE_REFRESH_WINDOW_MS),
  };
}

/** Drop a pending refresh (test teardown). */
export function cancelUsageRefresh(): void {
  if (pending === undefined) return;
  pending.target.clearTimeout(pending.timer);
  pending = undefined;
}
