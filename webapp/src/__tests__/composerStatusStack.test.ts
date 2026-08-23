import { describe, expect, it } from "vitest";
import { buildComposerStatusStackRows } from "../components/conversation/composerStatusStackData";

describe("composerStatusStack", () => {
  it("filters to owned jobs and hides stale terminal rows", () => {
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

    expect(rows.map((row) => row.id)).toEqual(["job_abc123def456", "cmd-1"]);
    expect(rows[0]).toMatchObject({
      kind: "swarm",
      state: "running",
      label: "Audit composer stack",
    });
    expect(rows[1]).toMatchObject({
      kind: "terminal",
      state: "running",
      label: "pytest -q",
    });
  });
});
