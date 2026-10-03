/**
 * Jobs rail is a compact native strip. Puppetmaster dashboard URLs are for
 * Board pop-out (a webpage). Adapters locate that page; they do not replace
 * the rail.
 */

export const JOBS_RAIL_VIEWPORT = "native-strip" as const;

export const JOBS_DASHBOARD_FOCUS_MIN_PX = 640;
export const JOBS_DASHBOARD_EXPAND_MIN_PX = 800;
export const REQUEST_RIGHT_MIN_WIDTH_EVENT = "harness-request-right-min-width";
export const JOBS_DASHBOARD_CHROME_EVENT = "harness-jobs-dashboard-chrome";

export type DashboardLocate = {
  ok: boolean;
  reused?: boolean;
  host?: string;
  port?: number;
  url?: string;
  embed_url?: string;
  job_id?: string | null;
  error?: string;
  detail?: string;
};

export function buildDashboardEmbedUrl(host: string, port: number, jobId?: string | null): string {
  const params = new URLSearchParams();
  const id = (jobId || "").trim();
  if (id) params.set("job", id);
  params.set("embed", "1");
  return `http://${host}:${port}/?${params.toString()}`;
}

/** Build the Puppetmaster board URL for pop-out. */
export function jobsRailViewportUrl(
  host: string,
  port: number,
  jobId?: string | null,
): string {
  return buildDashboardEmbedUrl(host, port, jobId);
}

export function jobsRailIsPuppetmasterViewport(): false {
  return false;
}

export function requestRightMinWidth(minPx: number): void {
  if (!(minPx > 0)) return;
  try {
    window.dispatchEvent(
      new CustomEvent(REQUEST_RIGHT_MIN_WIDTH_EVENT, { detail: { minPx } }),
    );
  } catch {
    /* ignore */
  }
}

/** Hide the Jobs card drag/close bar so the host chrome is the only Marionette strip. */
export function notifyJobsDashboardChrome(focused: boolean): void {
  try {
    window.dispatchEvent(
      new CustomEvent(JOBS_DASHBOARD_CHROME_EVENT, { detail: { focused } }),
    );
  } catch {
    /* ignore */
  }
}

export function dashboardUnavailableMessage(err?: unknown): string {
  if (err instanceof Error && err.message.trim()) return err.message.trim();
  if (typeof err === "string" && err.trim()) return err.trim();
  return "Could not locate the Puppetmaster dashboard.";
}

export function dashboardLocateError(payload: DashboardLocate & { stderr?: string }): string {
  const stderr = typeof payload.stderr === "string" ? payload.stderr.trim() : "";
  const base = (payload.detail || payload.error || "").trim() || dashboardUnavailableMessage();
  return stderr ? `${base}: ${stderr.slice(0, 280)}` : base;
}

export type JobsListEmptyInput = {
  failedRead: boolean;
  viewReady: boolean;
  working: boolean;
  hiddenCount: number;
  filter: string;
  hasJobs: boolean;
};

export type TrackerReadInput = {
  storeError: boolean;
  view: { availability: "known" | "unavailable"; missing: string[]; refreshing: boolean; refresh: string } | null;
  streamFailed: boolean;
  /** A list read named a store written after discovery; rediscovery is underway. */
  sourcesPending: boolean;
  localFailed: boolean;
  working: boolean;
  /** At least one list read has completed for the current view. */
  settled: boolean;
  hasJobs: boolean;
};

/** Source discovery is loading, not a failed read; a settled empty list stays settled
 *  while background polls run. Without both, the tracker flickers on every refresh. */
export function trackerReadState(input: TrackerReadInput): { failedRead: boolean; loading: boolean } {
  const view = input.view;
  const discovering = input.sourcesPending || !!view && (view.refreshing || view.refresh === "pending"
    || (view.availability === "unavailable" && view.missing.length > 0 && view.missing.every(m => m === "sources_not_refreshed")));
  const failedRead = input.storeError || (!!view
    && ((view.availability === "unavailable" && !discovering) || input.streamFailed || input.localFailed));
  const loading = !failedRead && !input.hasJobs && (!view || discovering || (input.working && !input.settled));
  return { failedRead, loading };
}

/** Honest empty-list copy — never "Job data unavailable" as a blank lie. */
export function jobsListEmptyTruth(input: JobsListEmptyInput): { title: string; detail?: string } {
  if (input.failedRead) {
    return {
      title: "Job observations could not be loaded",
      detail: "Retry updates; an empty view does not establish no work.",
    };
  }
  if (!input.viewReady || (!input.hasJobs && input.working)) {
    return { title: "Loading jobs…" };
  }
  if (input.filter !== "all" && !input.hasJobs) {
    return { title: "No jobs match this filter" };
  }
  if (input.hiddenCount > 0 && !input.hasJobs) {
    return { title: "All jobs are hidden" };
  }
  return { title: "No jobs yet" };
}
