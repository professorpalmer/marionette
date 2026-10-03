import { describe, expect, it } from "vitest";
import { isJobsListRow } from "../lib/jobMetadataContext";
import { isSwarmTrackerJob } from "../lib/jobClassification";

describe("Jobs list and Jobs strip row filters", () => {
  it("hides command rows from the Jobs panel", () => {
    expect(isJobsListRow({ job_kind: "run_command", id: "local-cmd-1" })).toBe(false);
    expect(isJobsListRow({ job_kind: "run_command_batch", id: "local-cmdbatch-1" })).toBe(false);
    expect(isJobsListRow({ job_kind: "provider" })).toBe(false);
    expect(isJobsListRow({ job_kind: "run_swarm", id: "job_abc" })).toBe(true);
    expect(isJobsListRow({ job_kind: "run_implement", id: "local-impl-1" })).toBe(true);
  });

  it("lists a swarm's dispatch alias before its Puppetmaster job exists", () => {
    // The backend projects every non-command local job as kind "provider",
    // including the run_swarm placeholder. Until the canonical job replaced it,
    // the tracker showed the swarm while the Jobs panel said "No jobs".
    expect(isJobsListRow({ job_kind: "provider", id: "local-swarm-toolu_01DX9mTRjHQiNgGCiQTe49gm" })).toBe(true);
    expect(isJobsListRow({ job_kind: "provider", id: "local-impl-toolu_01abc" })).toBe(true);
    expect(isJobsListRow({ job_kind: "provider", id: "local-cedfbf8c", adapter: "agentic" })).toBe(false);
  });

  it("hides command and wave-parent rows from the Jobs strip", () => {
    expect(isSwarmTrackerJob({ job_kind: "run_command", id: "local-cmd-1" })).toBe(false);
    expect(isSwarmTrackerJob({ job_kind: "parallel_wave", id: "local-wave-1" })).toBe(false);
    expect(isSwarmTrackerJob({ job_kind: "run_swarm", id: "job_abc" })).toBe(true);
    expect(isSwarmTrackerJob({ job_kind: "provider", id: "local-cedfbf8c", adapter: "agentic" })).toBe(true);
  });
});
