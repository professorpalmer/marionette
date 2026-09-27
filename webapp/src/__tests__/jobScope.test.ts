import { afterEach, describe, expect, it, vi } from "vitest";
import {
  filterJobsByScope,
  JOB_SCOPE_CHANGED_EVENT,
  JOB_SCOPE_KEY,
  jobInActiveSession,
  jobIsForeignForScope,
  jobOwnedForScope,
  jobScopeForSession,
  loadJobScope,
  parseJobScopeChoice,
  saveJobScope,
} from "../lib/jobScope";

describe("session-bound scope choice", () => {
  afterEach(() => localStorage.clear());

  it("defaults to session when nothing was picked", () => {
    expect(loadJobScope("sess-1")).toBe("session");
  });

  it("keeps an all pick only inside the session it was made in", () => {
    saveJobScope("all", "sess-1");
    expect(loadJobScope("sess-1")).toBe("all");
    expect(loadJobScope("sess-2")).toBe("session");
    // Switching back to the session where all was picked restores it.
    expect(loadJobScope("sess-1")).toBe("all");
  });

  it("a pick in the new session replaces the old one", () => {
    saveJobScope("all", "sess-1");
    saveJobScope("repo", "sess-2");
    expect(loadJobScope("sess-2")).toBe("repo");
    expect(loadJobScope("sess-1")).toBe("session");
  });

  it("ignores the legacy unbound v1 value", () => {
    localStorage.setItem("marionette.jobScope.v1", "all");
    expect(JOB_SCOPE_KEY).not.toBe("marionette.jobScope.v1");
    expect(loadJobScope("sess-1")).toBe("session");
  });

  it("fails closed on malformed stored values", () => {
    for (const raw of ["all", "{", "null", '{"scope":"all"}', '{"scope":"x","sessionId":"sess-1"}',
      '{"scope":"all","sessionId":""}']) {
      localStorage.setItem(JOB_SCOPE_KEY, raw);
      expect(loadJobScope("sess-1")).toBe("session");
    }
    expect(parseJobScopeChoice({ scope: "all", sessionId: "sess-1" })).toEqual({ scope: "all", sessionId: "sess-1" });
  });

  it("without an active session the effective scope is session", () => {
    expect(jobScopeForSession({ scope: "all", sessionId: "sess-1" }, "")).toBe("session");
    saveJobScope("all", "");
    expect(localStorage.getItem(JOB_SCOPE_KEY)).toBeNull();
  });

  it("announces the change so every tracker re-reads it", () => {
    const heard = vi.fn();
    window.addEventListener(JOB_SCOPE_CHANGED_EVENT, heard);
    saveJobScope("all", "sess-1");
    window.removeEventListener(JOB_SCOPE_CHANGED_EVENT, heard);
    expect(heard).toHaveBeenCalledTimes(1);
  });
});

const jobs = [
  { id: "a", session_id: "sess-1" },
  { id: "b", session_id: "sess-2" },
  { id: "c" },
  { id: "foreign", session_id: "sess-9", cross_project: true },
];

describe("jobInActiveSession", () => {
  it("requires both ids", () => {
    expect(jobInActiveSession({ session_id: "sess-1" }, "")).toBe(false);
    expect(jobInActiveSession({}, "sess-1")).toBe(false);
    expect(jobInActiveSession({ session_id: "sess-1" }, "sess-1")).toBe(true);
  });
});

describe("jobOwnedForScope", () => {
  it("requires a stamped session id", () => {
    expect(jobOwnedForScope({ session_id: "sess-1" })).toBe(true);
    expect(jobOwnedForScope({})).toBe(false);
    expect(jobIsForeignForScope({ id: "foreign", session_id: "sess-9", cross_project: true })).toBe(true);
    expect(jobIsForeignForScope({ id: "c" })).toBe(true);
    expect(jobIsForeignForScope({ id: "b", session_id: "sess-2" })).toBe(false);
  });
});

describe("filterJobsByScope", () => {
  it("session keeps only the active chat", () => {
    expect(filterJobsByScope(jobs, "session", "sess-1").map((j) => j.id)).toEqual(["a"]);
  });

  it("session without an active id fail-closes", () => {
    expect(filterJobsByScope(jobs, "session", "").map((j) => j.id)).toEqual([]);
  });

  it("session does not match jobs missing session_id", () => {
    expect(filterJobsByScope(jobs, "session", "sess-1").map((j) => j.id)).not.toContain("c");
  });

  it("repo drops unstamped and cross_project rows", () => {
    expect(filterJobsByScope(jobs, "repo", "sess-1").map((j) => j.id)).toEqual(["a", "b"]);
  });

  it("all keeps owned rows including owned cross_project", () => {
    expect(filterJobsByScope(jobs, "all", "sess-1").map((j) => j.id)).toEqual([
      "a",
      "b",
      "foreign",
    ]);
  });

  it("includeJobIds does not resurrect a foreign or unstamped id", () => {
    expect(
      filterJobsByScope(jobs, "session", "sess-1", { includeJobIds: ["foreign"] }).map((j) => j.id),
    ).toEqual(["a"]);
    expect(
      filterJobsByScope(jobs, "repo", "sess-1", { includeJobIds: ["foreign"] }).map((j) => j.id),
    ).toEqual(["a", "b"]);
    expect(
      filterJobsByScope(jobs, "session", "sess-1", { includeJobIds: ["c"] }).map((j) => j.id),
    ).toEqual(["a"]);
  });

  it("includeJobIds can pin another owned session job", () => {
    expect(
      filterJobsByScope(jobs, "session", "sess-1", { includeJobIds: ["b"] }).map((j) => j.id),
    ).toEqual(["a", "b"]);
  });
});
