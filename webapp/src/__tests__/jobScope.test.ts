import { describe, expect, it } from "vitest";
import { filterJobsByScope, jobInActiveSession } from "../lib/jobScope";

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

  it("repo drops cross_project rows", () => {
    expect(filterJobsByScope(jobs, "repo", "sess-1").map((j) => j.id)).toEqual(["a", "b", "c"]);
  });

  it("all keeps cross_project rows", () => {
    expect(filterJobsByScope(jobs, "all", "sess-1").map((j) => j.id)).toEqual([
      "a",
      "b",
      "c",
      "foreign",
    ]);
  });

  it("includeJobIds resurrects a foreign id under session", () => {
    expect(
      filterJobsByScope(jobs, "session", "sess-1", { includeJobIds: ["foreign"] }).map((j) => j.id),
    ).toEqual(["a", "foreign"]);
  });

  it("includeJobIds resurrects a foreign id under repo", () => {
    expect(
      filterJobsByScope(jobs, "repo", "sess-1", { includeJobIds: ["foreign"] }).map((j) => j.id),
    ).toEqual(["a", "b", "c", "foreign"]);
  });
});
