import { describe, expect, it } from "vitest";
import { buildComposerTasks, composerTasksRemainVisible, pickTaskSourceJob, taskProgress, taskState, waveHeaderText, waveProgress } from "../lib/composerTasks";
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
    expect(taskState("degraded")).toBe("degraded");
    expect(taskState("partial")).toBe("degraded");
    expect(taskState("timeout")).toBe("failed");
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

  it("prefers the wave coordinator in the active session", () => {
    const wave: Job = {
      ...job("local-wave-abc", "partial", "sess-1", [
        { id: "c1", role: "implement", instruction: "one", status: "completed", adapter: "x" },
        { id: "c2", role: "implement", instruction: "two", status: "failed", adapter: "x" },
      ]),
      job_kind: "parallel_wave",
      role: "parallel_wave",
      adapter: "parallel_wave",
      child_count: 8,
    };
    const picked = pickTaskSourceJob([
      job("local-child-1", "failed", "sess-1", [
        { id: "c1", role: "implement", instruction: "child", status: "failed", adapter: "x" },
      ]),
      wave,
    ], "sess-1");
    expect(picked?.id).toBe("local-wave-abc");
  });

  it("does not let a completed 5/5 wave hide a partial wave in the same session", () => {
    const done: Job = {
      ...job("local-wave-done", "completed", "sess-1", Array.from({ length: 5 }, (_, i) => ({
        id: `d${i}`,
        role: "implement",
        instruction: "implement",
        status: "completed",
        adapter: "x",
      }))),
      job_kind: "parallel_wave",
      role: "parallel_wave",
      adapter: "parallel_wave",
    };
    const partial: Job = {
      ...job("local-wave-mix", "partial", "sess-1", [
        { id: "c1", role: "implement", instruction: "one", status: "completed", adapter: "x" },
        { id: "c2", role: "implement", instruction: "two", status: "failed", adapter: "x" },
      ]),
      job_kind: "parallel_wave",
      role: "parallel_wave",
      adapter: "parallel_wave",
    };
    expect(pickTaskSourceJob([done, partial], "sess-1")?.id).toBe("local-wave-mix");
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

  it("collapses a multi-line worker prompt onto one line", () => {
    const tasks = buildComposerTasks(job("j", "running", "sess-1", [
      {
        id: "1",
        role: "reviewer",
        instruction: "Audit the Marionette harness\n\nPython backend under harness/.\nReact UI under webapp/src/.",
        status: "running",
        adapter: "x",
      },
    ]));
    expect(tasks[0].content).toBe(
      "reviewer · Audit the Marionette harness Python backend under harness/. React UI under webapp/src/.",
    );
    expect(tasks[0].content.includes("\n")).toBe(false);
  });

  it("paints degraded workers instead of clean-green complete", () => {
    const tasks = buildComposerTasks({
      id: "j",
      goal: "audit",
      status: "complete",
      session_id: "sess-1",
      tasks: [
        { id: "ok", role: "impl", instruction: "ship", status: "complete", adapter: "x" },
        { id: "deg", role: "review", instruction: "review", status: "complete", adapter: "x" },
      ],
      artifacts: [
        { type: "verification", headline: "review", task_id: "deg", result: "degraded", failure: "capability" },
      ],
    } as Job);
    expect(tasks[0].state).toBe("completed");
    expect(tasks[1].state).toBe("degraded");
    expect(taskProgress(tasks)).toEqual({ done: 2, total: 2 });
  });

  it("wave coordinator header and progress use child counts 4/8", () => {
    const tasks = Array.from({ length: 8 }, (_, i) => ({
      id: `c${i}`,
      role: "implement",
      instruction: `goal ${i}`,
      status: i < 4 ? "completed" : "failed",
      adapter: "x",
      applied: i < 4,
      failure_stage: i < 4 ? "" : "agentic_error",
      failure_reason: i < 4 ? "" : "adapter boom",
    }));
    const wave = {
      ...job("local-wave-mix", "partial", "sess-1", tasks),
      job_kind: "parallel_wave",
      role: "parallel_wave",
      child_count: 8,
      review_required: true,
    } as Job;
    const built = buildComposerTasks(wave);
    expect(built.filter((t) => t.state === "failed")).toHaveLength(4);
    expect(built.filter((t) => t.state === "completed")).toHaveLength(4);
    expect(built[4].detail).toBe("agentic_error: adapter boom");
    expect(waveProgress(wave)).toEqual({ completed: 4, failed: 4, applied: 4, total: 8 });
    expect(waveHeaderText(wave)).toBe(
      "Parallel wave — partial 4/8 completed · 4 failed · 4 patches applied · review required",
    );
  });

  it("drops a fully completed parallel wave so the composer does not keep implement flags", () => {
    const wave = {
      ...job("local-wave-done", "completed", "sess-1", Array.from({ length: 5 }, (_, i) => ({
        id: `c${i}`,
        role: "implement",
        instruction: "implement",
        status: "completed",
        adapter: "x",
      }))),
      job_kind: "parallel_wave",
      role: "parallel_wave",
      adapter: "parallel_wave",
      child_count: 5,
    } as Job;
    const tasks = buildComposerTasks(wave);
    expect(taskProgress(tasks)).toEqual({ done: 5, total: 5 });
    expect(composerTasksRemainVisible(tasks)).toBe(false);
  });

  it("keeps a partial or failed wave visible", () => {
    const tasks = buildComposerTasks({
      ...job("local-wave-mix", "partial", "sess-1", [
        { id: "c1", role: "implement", instruction: "one", status: "completed", adapter: "x" },
        { id: "c2", role: "implement", instruction: "two", status: "failed", adapter: "x" },
      ]),
      job_kind: "parallel_wave",
    } as Job);
    expect(composerTasksRemainVisible(tasks)).toBe(true);
  });
});
