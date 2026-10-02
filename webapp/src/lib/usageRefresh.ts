/**
 * Ask usage readers (footer, Economics pane) to refresh.
 *
 * Requests inside one window collapse into a single event: a session switch
 * replays every past tool result through the stream handler, and each used to
 * dispatch its own refresh.
 */
export const USAGE_REFRESH_WINDOW_MS = 150;

let pending: number | undefined;

export function requestUsageRefresh(): void {
  if (pending !== undefined) return;
  pending = window.setTimeout(() => {
    pending = undefined;
    window.dispatchEvent(new Event("harness-usage-refresh"));
  }, USAGE_REFRESH_WINDOW_MS);
}
