import { useEffect, useRef } from "react";

type PollFn = () => Promise<unknown> | void;

interface PollOptions {
  /** When false, the poll is torn down (e.g. panel hidden). Defaults to true. */
  enabled?: boolean;
  /** Add latency-proportional backoff when the backend responds slowly. Default true. */
  backoff?: boolean;
}

/**
 * Self-scheduling poller. Unlike setInterval, it queues the next run only AFTER
 * the current one settles, so at most one request is ever in flight. It also
 * pauses while the tab is hidden and (by default) backs off when the backend is
 * slow.
 *
 * This exists because the app runs many always-mounted pollers (jobs, reviews,
 * usage, swarm-live). With raw setInterval, an active swarm made the backend
 * slow, requests stacked faster than they returned, each grabbed a server
 * worker slot, and the whole UI starved -- panels loaded in chunks and clicks
 * (like closing Settings) didn't register. One in-flight request per poller,
 * gated on completion, removes that amplification at the source.
 *
 * The callback may be recreated every render; we always invoke the latest via a
 * ref so callers don't need to memoize it.
 */
export function usePolling(fn: PollFn, intervalMs: number, opts: PollOptions = {}) {
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const enabled = opts.enabled ?? true;
  const backoff = opts.backoff ?? true;

  useEffect(() => {
    if (!enabled) return;
    let active = true;
    let timer: number | undefined;
    let inFlight = false;

    const schedule = (ms: number) => {
      if (active) timer = window.setTimeout(tick, ms);
    };

    const tick = () => {
      // No point polling a tab nobody is looking at -- and it stops us piling
      // load on a busy backend while the user is elsewhere.
      if (document.hidden) { schedule(Math.max(intervalMs, 3000)); return; }
      if (inFlight) { schedule(500); return; }
      inFlight = true;
      const startedAt = performance.now();
      // Promise.resolve().then flattens whatever fn returns, so we wait for an
      // async fn's own promise before scheduling the next tick.
      Promise.resolve()
        .then(() => fnRef.current())
        .catch(() => { /* pollers own their error handling; never crash the loop */ })
        .finally(() => {
          inFlight = false;
          if (!active) return;
          const elapsed = performance.now() - startedAt;
          const extra = backoff && elapsed > 1500 ? Math.min(elapsed, 8000) : 0;
          schedule(intervalMs + extra);
        });
    };

    tick();
    const onVisible = () => {
      if (!document.hidden && !inFlight) { window.clearTimeout(timer); tick(); }
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      active = false;
      window.clearTimeout(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [intervalMs, enabled, backoff]);
}
