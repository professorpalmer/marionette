import { describe, expect, it } from "vitest";
import { isCommandJob } from "../lib/jobClassification";

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
