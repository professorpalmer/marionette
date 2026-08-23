import { describe, expect, it } from "vitest";
import { filterJobsByScope, jobInActiveSession } from "../lib/jobScope";

const jobs = [
  { id: "a", session_id: "sess-1" },
  { id: "b", session_id: "sess-2" },
  { id: "c" },
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

  it("repo keeps the full already-visible list", () => {
    expect(filterJobsByScope(jobs, "repo", "sess-1").map((j) => j.id)).toEqual(["a", "b", "c"]);
  });

  it("session without an active id keeps the list", () => {
    expect(filterJobsByScope(jobs, "session", "").map((j) => j.id)).toEqual(["a", "b", "c"]);
  });
});
