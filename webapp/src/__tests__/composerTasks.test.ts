import { describe, expect, it } from "vitest";
import { buildComposerTasks, pickTaskSourceJob, taskProgress, taskState } from "../lib/composerTasks";
import type { Job } from "../lib/api";

const job = (id: string, status: string, session_id: string, tasks: Job["tasks"]): Job => ({
  id,
  goal: id,
  status,
  session_id,
  tasks,
});

describe("taskState", () => {
  it("maps worker statuses", () => {
    expect(taskState("complete")).toBe("completed");
    expect(taskState("running")).toBe("in_progress");
    expect(taskState("pending")).toBe("pending");
    expect(taskState("failed")).toBe("failed");
  });
});

describe("pickTaskSourceJob", () => {
  it("prefers the active session running job", () => {
    const picked = pickTaskSourceJob([
      job("old", "complete", "sess-1", [{ id: "t0", role: "review", instruction: "old", status: "complete", adapter: "x" }]),
      job("live", "running", "sess-1", [{ id: "t1", role: "impl", instruction: "Implement one-time reheat", status: "running", adapter: "x" }]),
      job("other", "running", "sess-2", [{ id: "t2", role: "impl", instruction: "other", status: "running", adapter: "x" }]),
    ], "sess-1");
    expect(picked?.id).toBe("live");
  });
});

describe("buildComposerTasks + progress", () => {
  it("builds a Tasks N/M list", () => {
    const tasks = buildComposerTasks(job("j", "running", "sess-1", [
      { id: "1", role: "r", instruction: "Reproduce lifecycle", status: "complete", adapter: "x" },
      { id: "2", role: "r", instruction: "Add failing test", status: "complete", adapter: "x" },
      { id: "3", role: "r", instruction: "Implement reheat", status: "running", adapter: "x" },
      { id: "4", role: "r", instruction: "Run frontend tests", status: "pending", adapter: "x" },
      { id: "5", role: "r", instruction: "Commit and push", status: "pending", adapter: "x" },
    ]));
    expect(taskProgress(tasks)).toEqual({ done: 2, total: 5 });
    expect(tasks[2].state).toBe("in_progress");
  });
});
