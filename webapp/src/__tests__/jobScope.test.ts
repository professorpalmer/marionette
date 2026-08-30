import { describe, expect, it } from "vitest";
import { filterJobsByScope, jobInActiveSession, jobIsForeignForScope, jobOwnedForScope } from "../lib/jobScope";

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
