import { describe, expect, it } from "vitest";
import { countRunningTrackerJobs, dashboardJobId, isCommandJob, isPmDashboardJob, isRunningJobStatus, isSwarmTrackerJob, isTrackerHire, isWaveCoordinator } from "../lib/jobClassification";

describe("isCommandJob", () => {
  it("matches job_kind and local-cmd id prefixes", () => {
    expect(isCommandJob({ job_kind: "run_command" })).toBe(true);
    expect(isCommandJob({ job_kind: "run_command_batch" })).toBe(true);
    expect(isCommandJob({ id: "local-cmd-e35cf193" })).toBe(true);
    expect(isCommandJob({ id: "local-cmdbatch-aa11bb22" })).toBe(true);
    expect(isCommandJob({
      id: "local-cmd-e35cf193",
      job_kind: "run_command",
      role: "command",
      adapter: "command",
    })).toBe(true);
  });

  it("does not treat provider swarm / command-adapter workers as command jobs", () => {
    expect(isCommandJob({ id: "job_abc123def456" })).toBe(false);
    expect(isCommandJob({ id: "local-swarm-1", role: "worker", adapter: "agentic" })).toBe(false);
    expect(isCommandJob({ id: "local-impl-1", job_kind: "run_implement" })).toBe(false);
    expect(isCommandJob({ job_kind: "run_swarm" })).toBe(false);
    expect(isCommandJob({ job_kind: "run_parallel" })).toBe(false);
    expect(isCommandJob({ id: "job-timeout", adapter: "command", role: "command" })).toBe(false);
  });
});

describe("isWaveCoordinator", () => {
  it("matches local-wave ids, job_kind, and role/adapter parallel_wave", () => {
    expect(isWaveCoordinator({
      id: "local-wave-call_00_ET_S8G91HzE94famGY0TK0Q8637",
      role: "parallel_wave",
      adapter: "parallel_wave",
      job_kind: "parallel_wave",
    })).toBe(true);
    expect(isWaveCoordinator({ id: "local-wave-abc" })).toBe(true);
    expect(isWaveCoordinator({ job_kind: "parallel_wave" })).toBe(true);
    expect(isWaveCoordinator({ role: "parallel_wave" })).toBe(true);
    expect(isWaveCoordinator({ adapter: "parallel_wave" })).toBe(true);
  });

  it("does not hide real hires", () => {
    expect(isWaveCoordinator({ id: "job_abc123def456" })).toBe(false);
    expect(isWaveCoordinator({ id: "local-swarm-1", job_kind: "run_swarm" })).toBe(false);
    expect(isWaveCoordinator({ id: "local-impl-1", job_kind: "run_implement" })).toBe(false);
    expect(isWaveCoordinator({ job_kind: "run_parallel" })).toBe(false);
  });
});

describe("isSwarmTrackerJob allowlist", () => {
  it("shows real hires only", () => {
    expect(isSwarmTrackerJob({ job_kind: "run_swarm" })).toBe(true);
    expect(isSwarmTrackerJob({ job_kind: "run_implement" })).toBe(true);
    expect(isSwarmTrackerJob({ id: "job_abc123def456" })).toBe(true);
    expect(isSwarmTrackerJob({ id: "local-swarm-1", role: "worker", adapter: "agentic" })).toBe(true);
    expect(isSwarmTrackerJob({ id: "local-impl-1", job_kind: "run_implement" })).toBe(true);
    expect(isSwarmTrackerJob({
      id: "job_hire",
      job_kind: "run_implement",
      adapter: "command",
      role: "command",
    })).toBe(true);
    expect(isSwarmTrackerJob({ job_kind: "run_swarm", id: "job_abc" })).toBe(true);
    expect(isSwarmTrackerJob({ job_kind: "provider", id: "local-cedfbf8c", adapter: "agentic" })).toBe(true);
  });

  it("hides command jobs, wave parents, and command/wave role stamps", () => {
    expect(isSwarmTrackerJob({
      id: "local-cmd-e35cf193",
      job_kind: "run_command",
      role: "command",
      adapter: "command",
    })).toBe(false);
    expect(isSwarmTrackerJob({
      id: "local-wave-call_00_ET_S8G91HzE94famGY0TK0Q8637",
      role: "parallel_wave",
      adapter: "parallel_wave",
      job_kind: "parallel_wave",
    })).toBe(false);
    expect(isSwarmTrackerJob({ id: "job-timeout", adapter: "command", role: "command" })).toBe(false);
    expect(isSwarmTrackerJob({ id: "local-cmdbatch-aa11bb22", job_kind: "run_command_batch" })).toBe(false);
    expect(isSwarmTrackerJob({ job_kind: "run_command", id: "local-cmd-1" })).toBe(false);
    expect(isSwarmTrackerJob({ job_kind: "parallel_wave", id: "local-wave-1" })).toBe(false);
  });

  it("keeps unnamed store-shaped swarm rows that are not coordinators", () => {
    expect(isSwarmTrackerJob({ id: "job-live", goal: "Live audit" } as any)).toBe(true);
    expect(isSwarmTrackerJob({ id: "job_par", goal: "run_parallel wave" })).toBe(true);
    expect(isTrackerHire({ job_kind: "run_swarm" })).toBe(true);
  });
});

describe("isPmDashboardJob", () => {
  it("hosts every Jobs-rail hire, including agentic locals", () => {
    expect(isPmDashboardJob({ id: "job_abc123def456" })).toBe(true);
    expect(isPmDashboardJob({
      id: "local-impl-1",
      job_kind: "run_implement",
      job_ref: { job_id: "job_abc123def456" },
      source: "harness",
    })).toBe(true);
    expect(isPmDashboardJob({
      id: "local-cedfbf8c",
      adapter: "agentic",
      local_ref: { job_id: "local-cedfbf8c", incarnation: "n1" },
    })).toBe(true);
    expect(isPmDashboardJob({
      id: "job_abc123def456",
      local_ref: { job_id: "job_abc123def456", incarnation: "n1" },
    })).toBe(true);
    expect(isPmDashboardJob({ job_kind: "run_swarm" })).toBe(true);
  });

  it("still hides commands and wave coordinators", () => {
    expect(isPmDashboardJob({ job_kind: "run_command", id: "local-cmd-1" })).toBe(false);
    expect(isPmDashboardJob({ job_kind: "parallel_wave", id: "local-wave-1" })).toBe(false);
  });
});

describe("dashboardJobId", () => {
  it("prefers a durable job_ token over a local alias", () => {
    expect(dashboardJobId({
      id: "local-impl-1",
      job_ref: { job_id: "job_abc123def456" },
    })).toBe("job_abc123def456");
    expect(dashboardJobId({
      id: "local-cedfbf8c",
      adapter: "agentic",
      local_ref: { job_id: "local-cedfbf8c", incarnation: "n1" },
      parent_ref: { job_id: "job_parenthire01" },
    })).toBe("job_parenthire01");
    expect(dashboardJobId({
      id: "local-cedfbf8c",
      local_ref: { job_id: "local-cedfbf8c", incarnation: "n1" },
    })).toBe("");
    expect(dashboardJobId({
      id: "local-swarm-call_1799376",
      adapter: "agentic",
    })).toBe("");
  });
});

describe("countRunningTrackerJobs", () => {
  it("pulses only real swarm hires, not run_command terminals", () => {
    expect(isRunningJobStatus("running")).toBe(true);
    expect(isRunningJobStatus("in_progress")).toBe(true);
    expect(isRunningJobStatus("complete")).toBe(false);
    expect(countRunningTrackerJobs([
      { id: "local-cmd-1", job_kind: "run_command", status: "running" },
      { id: "job_abc", job_kind: "run_swarm", status: "running" },
      { id: "job_done", job_kind: "run_swarm", status: "complete" },
      { id: "local-wave-1", job_kind: "parallel_wave", status: "running" },
    ])).toBe(1);
  });
});
