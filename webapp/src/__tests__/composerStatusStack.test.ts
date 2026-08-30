import { describe, expect, it } from "vitest";
import {
  buildComposerStatusStackRows,
  visibleCommandJob,
  visibleSwarmJob,
} from "../components/conversation/composerStatusStackData";
import type { Job } from "../lib/api";

describe("composerStatusStack", () => {
  it("filters to owned jobs and ignores session-only commands", () => {
    const now = Date.parse("2026-08-23T12:00:00Z");
    const rows = buildComposerStatusStackRows({
      nowMs: now,
      swarmJobs: [
        {
          id: "job_abc123def456",
          goal: "Audit composer stack",
          source: "harness",
          status: "running",
          updated_at: now - 1000,
        } as any,
        {
          id: "job_zzz999yyy888",
          goal: "CLI noise",
          source: "cli",
          status: "running",
          updated_at: now - 1000,
        } as any,
        {
          id: "job_failed000001",
          goal: "Old failed job",
          source: "harness",
          status: "failed",
          updated_at: now - 13_000,
        } as any,
      ],
      commandSessions: [
        {
          id: "cmd-1",
          command: "pytest -q",
          output: "running...",
          state: "running",
          updatedAt: now - 500,
        },
        {
          id: "cmd-2",
          command: "npm test",
          output: "done",
          state: "done",
          updatedAt: now - 5_000,
        },
        {
          id: "cmd-3",
          command: "pnpm lint",
          output: "failed",
          state: "failed",
          updatedAt: now - 13_000,
        },
      ],
    });

    expect(rows.map((row) => row.id)).toEqual(["job_abc123def456"]);
    expect(rows[0]).toMatchObject({
      kind: "swarm",
      state: "running",
      label: "Audit composer stack",
    });
  });

  it("yields only a Puppetmaster row for a running swarm session plus swarm job", () => {
    const now = Date.parse("2026-08-23T12:00:00Z");
    const swarmJob = {
      id: "job_abc123def456",
      goal: "Audit composer stack",
      source: "harness",
      status: "running",
      updated_at: now - 1000,
      job_kind: "run_swarm",
    } satisfies Job;
    const rows = buildComposerStatusStackRows({
      nowMs: now,
      swarmJobs: [swarmJob],
      commandSessions: [{
        id: "job_abc123def456",
        command: "Audit composer stack",
        output: "working",
        state: "running",
        updatedAt: now - 200,
      }],
    });
    expect(rows.map((row) => ({ id: row.id, kind: row.kind }))).toEqual([
      { id: "job_abc123def456", kind: "swarm" },
    ]);
  });

  it("removes the Terminal row when the command job is gone", () => {
    const now = Date.parse("2026-08-23T12:00:00Z");
    const session = {
      id: "local-cmd-e35cf193",
      command: "sleep 999",
      output: "still running",
      state: "running" as const,
      updatedAt: now - 200,
    };
    const commandJob = {
      id: "local-cmd-e35cf193",
      goal: "sleep 999",
      source: "harness",
      status: "running",
      updated_at: now - 1000,
      job_kind: "run_command",
      role: "command",
      adapter: "command",
      command_preview: "sleep 999",
    } satisfies Job;
    expect(buildComposerStatusStackRows({
      nowMs: now,
      swarmJobs: [commandJob],
      commandSessions: [session],
    }).map((row) => row.id)).toEqual(["local-cmd-e35cf193"]);
    expect(buildComposerStatusStackRows({
      nowMs: now,
      swarmJobs: [],
      commandSessions: [session],
    })).toEqual([]);
  });

  it("keeps two live command jobs with the same command text as separate rows", () => {
    const now = Date.parse("2026-08-23T12:00:00Z");
    const rows = buildComposerStatusStackRows({
      nowMs: now,
      swarmJobs: [
        {
          id: "local-cmd-aaa",
          goal: "pytest -q",
          source: "harness",
          status: "running",
          updated_at: now - 1000,
          job_kind: "run_command",
          command_preview: "pytest -q",
        } satisfies Job,
        {
          id: "local-cmd-bbb",
          goal: "pytest -q",
          source: "harness",
          status: "running",
          updated_at: now - 500,
          job_kind: "run_command",
          command_preview: "pytest -q",
        } satisfies Job,
      ],
      commandSessions: [
        {
          id: "local-cmd-aaa",
          command: "pytest -q",
          output: "a",
          state: "running",
          updatedAt: now - 200,
        },
        {
          id: "local-cmd-bbb",
          command: "pytest -q",
          output: "b",
          state: "running",
          updatedAt: now - 100,
        },
      ],
    });
    expect(rows.map((row) => ({ id: row.id, output: row.output }))).toEqual([
      { id: "local-cmd-bbb", output: "b" },
      { id: "local-cmd-aaa", output: "a" },
    ]);
  });

  it("drops a completed command job after linger even if a session is still running", () => {
    const now = Date.parse("2026-08-23T12:00:00Z");
    const rows = buildComposerStatusStackRows({
      nowMs: now,
      swarmJobs: [{
        id: "local-cmd-stale",
        goal: "sleep 1",
        source: "harness",
        status: "completed",
        updated_at: now - 5_000,
        job_kind: "run_command",
        command_preview: "sleep 1",
      } satisfies Job],
      commandSessions: [{
        id: "local-cmd-stale",
        command: "sleep 1",
        output: "ok",
        state: "running",
        updatedAt: now - 200,
      }],
    });
    expect(rows).toEqual([]);
  });


  it("reclassifies run_command live jobs as terminal, not swarm", () => {
    const now = Date.parse("2026-08-23T12:00:00Z");
    const commandJob = {
      id: "local-cmd-e35cf193",
      goal: "sleep 999",
      source: "harness",
      status: "running",
      updated_at: now - 1000,
      job_kind: "run_command",
      role: "command",
      adapter: "command",
      command_preview: "sleep 999",
    } satisfies Job;
    const swarmJob = {
      id: "job_abc123def456",
      goal: "Audit composer stack",
      source: "harness",
      status: "running",
      updated_at: now - 1000,
    } as any;
    expect(visibleSwarmJob(commandJob, now)).toBeNull();
    expect(visibleCommandJob(commandJob, now)).toMatchObject({
      id: "local-cmd-e35cf193",
      kind: "terminal",
      command: "sleep 999",
      state: "running",
    });
    const rows = buildComposerStatusStackRows({
      nowMs: now,
      swarmJobs: [commandJob, swarmJob],
      commandSessions: [],
    });
    expect(rows.map((row) => ({ id: row.id, kind: row.kind }))).toEqual([
      { id: "job_abc123def456", kind: "swarm" },
      { id: "local-cmd-e35cf193", kind: "terminal" },
    ]);
  });


  it("classifies run_command_batch and local-cmd* ids as terminal", () => {
    const now = Date.parse("2026-08-23T12:00:00Z");
    const cases = [
      {
        id: "job_batch",
        goal: "echo a",
        source: "harness",
        status: "running",
        updated_at: now - 1000,
        job_kind: "run_command_batch",
        role: "command_batch",
        adapter: "command_batch",
        command_preview: "echo a",
      },
      {
        id: "job_role_only",
        goal: "sleep 1",
        source: "harness",
        status: "running",
        updated_at: now - 1000,
        job_kind: "run_command",
        role: "command",
        adapter: "command",
      },
      {
        id: "local-cmd-deadbeef",
        goal: "sleep 1",
        source: "harness",
        status: "running",
        updated_at: now - 1000,
      },
      {
        id: "local-cmdbatch-aa11bb22",
        goal: "echo batch",
        source: "harness",
        status: "running",
        updated_at: now - 1000,
        command_preview: "echo batch",
      },
    ] as any[];
    for (const job of cases) {
      expect(visibleSwarmJob(job, now), job.id).toBeNull();
      expect(visibleCommandJob(job, now)?.kind, job.id).toBe("terminal");
    }
  });

  it("hides command-stamped non-hires and wave parents from the swarm stack", () => {
    const now = Date.parse("2026-08-23T12:00:00Z");
    const commandStamp = {
      id: "job-timeout",
      goal: "Timed-out command",
      source: "harness",
      status: "timeout",
      updated_at: now - 1000,
      adapter: "command",
      role: "command",
    } as any;
    expect(visibleSwarmJob(commandStamp, now)).toBeNull();
    expect(visibleCommandJob(commandStamp, now)).toBeNull();
    const wave = {
      id: "local-wave-call_00_ET_S8G91HzE94famGY0TK0Q8637",
      goal: "Parallel wave (2 jobs)",
      source: "harness",
      status: "running",
      updated_at: now - 1000,
      role: "parallel_wave",
      adapter: "parallel_wave",
      job_kind: "parallel_wave",
    } as any;
    expect(visibleSwarmJob(wave, now)).toBeNull();
    expect(visibleCommandJob(wave, now)).toBeNull();
  });

  it("keeps a command-adapter hire on the swarm stack", () => {
    const now = Date.parse("2026-08-23T12:00:00Z");
    const hire = {
      id: "job_abc123def456",
      goal: "run_implement fix",
      source: "harness",
      status: "running",
      updated_at: now - 1000,
      job_kind: "run_implement",
      adapter: "command",
      role: "command",
    } as any;
    expect(visibleSwarmJob(hire, now)?.kind).toBe("swarm");
    expect(visibleCommandJob(hire, now)).toBeNull();
  });

  it("keeps run_swarm / run_implement / run_parallel as swarm", () => {
    const now = Date.parse("2026-08-23T12:00:00Z");
    const jobs = [
      { id: "job_swarm", goal: "run_swarm audit", source: "harness", status: "running", updated_at: now - 1000 },
      { id: "job_impl", goal: "run_implement fix", source: "harness", status: "running", updated_at: now - 1000, role: "implementer", adapter: "agentic" },
      { id: "job_par", goal: "run_parallel wave", source: "harness", status: "running", updated_at: now - 1000 },
    ] as any[];
    for (const job of jobs) {
      expect(visibleSwarmJob(job, now)?.kind, job.id).toBe("swarm");
      expect(visibleCommandJob(job, now), job.id).toBeNull();
    }
  });

  it("dedupes a live command job against the same command-session id", () => {
    const now = Date.parse("2026-08-23T12:00:00Z");
    const rows = buildComposerStatusStackRows({
      nowMs: now,
      swarmJobs: [{
        id: "local-cmd-e35cf193",
        goal: "sleep 999",
        source: "harness",
        status: "running",
        updated_at: now - 1000,
        job_kind: "run_command",
        role: "command",
        adapter: "command",
        command_preview: "sleep 999",
      } as any],
      commandSessions: [{
        id: "local-cmd-e35cf193",
        command: "sleep 999",
        output: "still running",
        state: "running",
        updatedAt: now - 200,
      }],
    });
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      id: "local-cmd-e35cf193",
      kind: "terminal",
      output: "still running",
    });
  });

  it("treats truncated command jobs as failed, not running", () => {
    const now = Date.parse("2026-08-23T12:00:00Z");
    const job = {
      id: "local-cmd-trunc",
      goal: "find ~/.pmharness",
      source: "harness",
      status: "truncated",
      updated_at: now - 1000,
      job_kind: "run_command",
      role: "command",
      adapter: "command",
      command_preview: "find ~/.pmharness",
    } as any;
    expect(visibleCommandJob(job, now)).toMatchObject({
      id: "local-cmd-trunc",
      kind: "terminal",
      state: "failed",
    });
  });

  it("lets a truncated live job overlay a still-running command session", () => {
    const now = Date.parse("2026-08-23T12:00:00Z");
    const rows = buildComposerStatusStackRows({
      nowMs: now,
      swarmJobs: [{
        id: "local-cmd-e35cf193",
        goal: "find ~/.pmharness",
        source: "harness",
        status: "truncated",
        updated_at: now - 1000,
        job_kind: "run_command",
        role: "command",
        adapter: "command",
        command_preview: "find ~/.pmharness",
      } as any],
      commandSessions: [{
        id: "local-cmd-e35cf193",
        command: "find ~/.pmharness",
        output: "truncated (exit -1)",
        state: "running",
        updatedAt: now - 200,
      }],
    });
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      id: "local-cmd-e35cf193",
      kind: "terminal",
      state: "failed",
      output: "truncated (exit -1)",
    });
  });

  it("hides leftover Term rows from another chat on a New session", () => {
    const now = Date.parse("2026-08-23T12:00:00Z");
    const rows = buildComposerStatusStackRows({
      nowMs: now,
      sessionId: "sess-new",
      swarmJobs: [
        {
          id: "local-cmd-foreign",
          goal: "git checkout -- tsconfig.json",
          source: "harness",
          status: "completed",
          session_id: "sess-old",
          updated_at: now - 1000,
          job_kind: "run_command",
          command_preview: "git checkout -- tsconfig.json",
        } as any,
        {
          id: "local-cmd-here",
          goal: "echo hi",
          source: "harness",
          status: "running",
          session_id: "sess-new",
          updated_at: now - 1000,
          job_kind: "run_command",
          command_preview: "echo hi",
        } as any,
        {
          id: "local-cmd-cli",
          goal: "cursor leftover",
          source: "cli",
          status: "running",
          session_id: "sess-new",
          updated_at: now - 1000,
          job_kind: "run_command",
          command_preview: "cursor leftover",
        } as any,
      ],
      commandSessions: [
        {
          id: "leftover-index",
          command: "sqliteTable tournaments slug",
          output: "done",
          state: "done",
          updatedAt: now - 500,
          sessionId: "sess-old",
        },
        {
          id: "orphan-index",
          command: "git status --short",
          output: "done",
          state: "done",
          updatedAt: now - 500,
        },
      ],
    });
    expect(rows.map((row) => row.id)).toEqual(["local-cmd-here"]);
    expect(rows[0]).toMatchObject({ kind: "terminal", command: "echo hi" });
  });

  it("does not let a live poll flip a finished command back to running", () => {
    const now = Date.parse("2026-08-23T12:00:00Z");
    const rows = buildComposerStatusStackRows({
      nowMs: now,
      swarmJobs: [{
        id: "local-cmd-e35cf193",
        goal: "brew install llama.cpp",
        source: "harness",
        status: "",
        updated_at: now - 200,
        job_kind: "run_command",
        command_preview: "brew install llama.cpp",
      } as any],
      commandSessions: [{
        id: "local-cmd-e35cf193",
        command: "brew install llama.cpp",
        output: "ok",
        state: "done",
        updatedAt: now - 3_000,
      }],
    });
    expect(rows).toHaveLength(1);
    expect(rows[0].state).toBe("done");
    expect(rows[0].updatedAt).toBe(now - 200);
  });

  it("treats a recent blank command-job status as done", () => {
    const now = Date.parse("2026-08-23T12:00:00Z");
    const job = {
      id: "local-cmd-blank-recent",
      goal: "echo done",
      source: "harness",
      status: "",
      updated_at: now - 1000,
      job_kind: "run_command",
      command_preview: "echo done",
    } satisfies Job;
    expect(visibleCommandJob(job, now)).toMatchObject({
      id: "local-cmd-blank-recent",
      kind: "terminal",
      state: "done",
      updatedAt: now - 1000,
    });
    const rows = buildComposerStatusStackRows({
      nowMs: now,
      swarmJobs: [job],
      commandSessions: [],
    });
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      id: "local-cmd-blank-recent",
      state: "done",
    });
  });

  it("drops an old blank command-job status without a session", () => {
    const now = Date.parse("2026-08-23T12:00:00Z");
    const job = {
      id: "local-cmd-blank-old",
      goal: "echo stale",
      source: "harness",
      status: "   ",
      updated_at: now - 5_000,
      job_kind: "run_command",
      command_preview: "echo stale",
    } satisfies Job;
    expect(visibleCommandJob(job, now)).toBeNull();
    expect(buildComposerStatusStackRows({
      nowMs: now,
      swarmJobs: [job],
      commandSessions: [],
    })).toEqual([]);
  });

  it("lets a matching running session overlay a recent blank command job", () => {
    const now = Date.parse("2026-08-23T12:00:00Z");
    const rows = buildComposerStatusStackRows({
      nowMs: now,
      swarmJobs: [{
        id: "local-cmd-blank-live",
        goal: "sleep 999",
        source: "harness",
        status: "",
        updated_at: now - 1000,
        job_kind: "run_command",
        command_preview: "sleep 999",
      } satisfies Job],
      commandSessions: [{
        id: "local-cmd-blank-live",
        command: "sleep 999",
        output: "still going",
        state: "running",
        updatedAt: now - 200,
      }],
    });
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      id: "local-cmd-blank-live",
      state: "running",
      output: "still going",
    });
  });

  it("keeps explicit pending, queued, running, and active command statuses running", () => {
    const now = Date.parse("2026-08-23T12:00:00Z");
    for (const status of ["pending", "queued", "running", "active"]) {
      const job = {
        id: `local-cmd-${status}`,
        goal: "sleep 1",
        source: "harness",
        status,
        updated_at: now - 5_000,
        job_kind: "run_command",
        command_preview: "sleep 1",
      } satisfies Job;
      expect(visibleCommandJob(job, now)?.state, status).toBe("running");
    }
  });

  it("hides settled transcript history that was never observed running", () => {
    const now = Date.parse("2026-08-23T12:00:00Z");
    const rows = buildComposerStatusStackRows({
      nowMs: now,
      swarmJobs: [],
      commandSessions: [{
        id: "old-card",
        command: "brew install llama.cpp",
        output: "ok",
        state: "done",
        updatedAt: now - 400,
        railVisible: false,
      }],
    });
    expect(rows).toEqual([]);
  });

  it("lets a live command job own the row even when the session is rail-hidden", () => {
    const now = Date.parse("2026-08-23T12:00:00Z");
    const rows = buildComposerStatusStackRows({
      nowMs: now,
      swarmJobs: [{
        id: "local-cmd-aa11",
        goal: "brew install llama.cpp",
        source: "harness",
        status: "",
        updated_at: now - 100,
        job_kind: "run_command",
        command_preview: "brew install llama.cpp",
      } as any],
      commandSessions: [{
        id: "local-cmd-aa11",
        command: "brew install llama.cpp",
        output: "ok",
        state: "done",
        updatedAt: now - 400,
        railVisible: false,
      }],
    });
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({
      id: "local-cmd-aa11",
      kind: "terminal",
      state: "done",
      output: "ok",
    });
  });

  it("keeps a settled command job briefly, then drops a session-only linger", () => {
    const now = Date.parse("2026-08-23T12:00:00Z");
    const commandJob = {
      id: "live-then-done",
      goal: "brew install llama.cpp",
      source: "harness",
      status: "completed",
      updated_at: now - 400,
      job_kind: "run_command",
      command_preview: "brew install llama.cpp",
    } satisfies Job;
    expect(buildComposerStatusStackRows({
      nowMs: now,
      swarmJobs: [commandJob],
      commandSessions: [{
        id: "live-then-done",
        command: "brew install llama.cpp",
        output: "ok",
        state: "done",
        updatedAt: now - 400,
        railVisible: true,
      }],
    }).map((row) => row.id)).toEqual(["live-then-done"]);
    expect(buildComposerStatusStackRows({
      nowMs: now,
      swarmJobs: [],
      commandSessions: [{
        id: "live-then-done",
        command: "brew install llama.cpp",
        output: "ok",
        state: "done",
        updatedAt: now - 400,
        railVisible: true,
      }],
    })).toEqual([]);
  });

  it("collapses command preview whitespace so the task bar does not stair-step", () => {
    const now = Date.parse("2026-08-23T12:00:00Z");
    const job = {
      id: "local-cmd-wrap",
      goal: "git status",
      source: "harness",
      status: "running",
      updated_at: now - 1000,
      job_kind: "run_command",
      command_preview: "git status\n  && echo ok",
    } as any;
    const row = visibleCommandJob(job, now);
    expect(row?.label).toBe("git status && echo ok");
    expect(row?.command).toContain("git status");
  });
});
