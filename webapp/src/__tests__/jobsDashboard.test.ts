import { describe, expect, it, vi, afterEach } from "vitest";
import {
  buildDashboardEmbedUrl,
  dashboardLocateError,
  dashboardUnavailableMessage,
  jobsListEmptyTruth,
  trackerReadState,
  jobsRailIsPuppetmasterViewport,
  jobsRailViewportUrl,
  JOBS_RAIL_VIEWPORT,
  JOBS_DASHBOARD_CHROME_EVENT,
  JOBS_DASHBOARD_EXPAND_MIN_PX,
  JOBS_DASHBOARD_FOCUS_MIN_PX,
  REQUEST_RIGHT_MIN_WIDTH_EVENT,
  notifyJobsDashboardChrome,
  requestRightMinWidth,
} from "../lib/jobsDashboard";

describe("jobsDashboard URLs", () => {
  it("always asks for embed=1 and keeps the job query", () => {
    expect(buildDashboardEmbedUrl("127.0.0.1", 8787, "job_abcdef012345")).toBe(
      "http://127.0.0.1:8787/?job=job_abcdef012345&embed=1",
    );
    expect(buildDashboardEmbedUrl("127.0.0.1", 8790)).toBe("http://127.0.0.1:8790/?embed=1");
    expect(jobsRailViewportUrl("127.0.0.1", 8787, "job_abcdef012345")).toBe(
      buildDashboardEmbedUrl("127.0.0.1", 8787, "job_abcdef012345"),
    );
    expect(jobsRailIsPuppetmasterViewport()).toBe(false);
    expect(JOBS_RAIL_VIEWPORT).toBe("native-strip");
  });
});

describe("requestRightMinWidth", () => {
  afterEach(() => { vi.restoreAllMocks(); });

  it("asks the shell to grow the Jobs pane for a focused dashboard", () => {
    const spy = vi.spyOn(window, "dispatchEvent");
    requestRightMinWidth(JOBS_DASHBOARD_FOCUS_MIN_PX);
    const event = spy.mock.calls[0]?.[0] as CustomEvent<{ minPx: number }>;
    expect(event.type).toBe(REQUEST_RIGHT_MIN_WIDTH_EVENT);
    expect(event.detail.minPx).toBe(JOBS_DASHBOARD_FOCUS_MIN_PX);
    expect(JOBS_DASHBOARD_FOCUS_MIN_PX).toBe(640);
    expect(JOBS_DASHBOARD_EXPAND_MIN_PX).toBe(800);
    expect(JOBS_DASHBOARD_EXPAND_MIN_PX).toBeGreaterThan(JOBS_DASHBOARD_FOCUS_MIN_PX);
  });

  it("tells the Jobs card to drop its own chrome while a hire is hosted", () => {
    const spy = vi.spyOn(window, "dispatchEvent");
    notifyJobsDashboardChrome(true);
    const event = spy.mock.calls[0]?.[0] as CustomEvent<{ focused: boolean }>;
    expect(event.type).toBe(JOBS_DASHBOARD_CHROME_EVENT);
    expect(event.detail.focused).toBe(true);
  });
});


describe("Jobs rail honesty", () => {
  it("names locate/embed failures instead of Job data unavailable", () => {
    expect(dashboardLocateError({
      ok: false,
      error: "state_dir_unavailable",
      detail: "No Puppetmaster project store for this workspace.",
    })).toBe("No Puppetmaster project store for this workspace.");
    expect(dashboardUnavailableMessage()).toBe("Could not locate the Puppetmaster dashboard.");
    expect(jobsListEmptyTruth({
      failedRead: true,
      viewReady: true,
      working: false,
      hiddenCount: 0,
      filter: "all",
      hasJobs: false,
    })).toEqual({
      title: "Job observations could not be loaded",
      detail: "Retry updates; an empty view does not establish no work.",
    });
    expect(jobsListEmptyTruth({
      failedRead: false,
      viewReady: true,
      working: false,
      hiddenCount: 0,
      filter: "all",
      hasJobs: false,
    }).title).toBe("No jobs yet");
  });
});

describe("trackerReadState", () => {
  const known = { availability: "known" as const, missing: [], refreshing: false, refresh: "idle" };
  const base = { storeError: false, view: known, streamFailed: false, sourcesPending: false, localFailed: false, working: false, settled: true, hasJobs: false };

  it("treats initial source discovery as loading, not a failed read", () => {
    const initial = { availability: "unavailable" as const, missing: ["sources_not_refreshed"], refreshing: false, refresh: "idle" };
    expect(trackerReadState({ ...base, view: initial, settled: false })).toEqual({ failedRead: false, loading: true });
    expect(trackerReadState({ ...base, view: { ...initial, refresh: "pending" }, settled: false })).toEqual({ failedRead: false, loading: true });
    expect(trackerReadState({ ...base, view: { ...known, availability: "unavailable", missing: ["source_discovery_unavailable"], refreshing: true } }))
      .toEqual({ failedRead: false, loading: true });
  });

  it("treats a store written after discovery as loading while it is rediscovered", () => {
    expect(trackerReadState({ ...base, sourcesPending: true, settled: false })).toEqual({ failedRead: false, loading: true });
  });

  it("keeps a settled empty list settled while a background poll runs", () => {
    expect(trackerReadState({ ...base, working: true })).toEqual({ failedRead: false, loading: false });
    expect(trackerReadState({ ...base, working: true, settled: false })).toEqual({ failedRead: false, loading: true });
  });

  it("still reports real failures", () => {
    expect(trackerReadState({ ...base, storeError: true }).failedRead).toBe(true);
    expect(trackerReadState({ ...base, streamFailed: true }).failedRead).toBe(true);
    expect(trackerReadState({ ...base, view: { ...known, availability: "unavailable", missing: ["response_budget"] } }).failedRead).toBe(true);
    expect(trackerReadState({ ...base, view: { ...known, availability: "unavailable", missing: ["no_known_sources"] } }).failedRead).toBe(true);
  });
});
