import { describe, expect, it } from "vitest";
import {
  columnRowKind,
  columnRowKinds,
  type TranscriptColumnRowKind,
} from "../components/conversation/transcriptColumnRows";
import {
  groupAgentActivity,
  type GroupedItem,
  type Item,
} from "../components/TranscriptList";

const card: Item = {
  kind: "card",
  card: { id: "read-1", goal: "read file", running: false, open: false },
};

function activityInnerKinds(row: GroupedItem): string[] {
  if (row.kind !== "activity_group") return [];
  return row.items.map((item) => item.kind);
}

describe("columnRowKind presentation union (v0.9.319)", () => {
  it("maps grouped rows to the four main-column kinds", () => {
    const rows: GroupedItem[] = [
      { kind: "msg", msg: { role: "user", text: "hi" } },
      { kind: "activity_group", items: [card] },
      { kind: "command_approval", id: "a", command: "x", commandHash: "h", sessionId: "s", workspaceRoot: "/", category: "c", reason: "r", matched: "m", status: "pending" },
      { kind: "secret_request", id: "sr", label: "key", connector: "openai", field: "api_key", description: "", sessionId: "s", status: "pending" },
      { kind: "pending_review", id: "pr", summary: "held" },
      { kind: "steer", text: "try again" },
      { kind: "auth_failure", message: "denied", id: "af" },
      { kind: "turn_terminal", cause: "halt", state: "done", text: "stopped" },
    ];
    expect(columnRowKinds(rows)).toEqual([
      "msg",
      "activity",
      "question",
      "question",
      "file",
      "msg",
      "activity",
      "activity",
    ] satisfies TranscriptColumnRowKind[]);
  });

  it("classifies inner telemetry variants as activity when folded", () => {
    expect(
      columnRowKind({
        kind: "activity_group",
        items: [
          card,
          { kind: "thinking", id: "t1", text: "reasoning" },
          { kind: "checkpoint", id: "k1", label: "snap", trigger: "manual" },
          {
            kind: "swarm_pending",
            job_ids: ["j1"],
            objective: "audit",
            status: "running",
          },
          {
            kind: "swarm_result",
            job_id: "j1",
            applied: true,
            files: [],
            summary: "ok",
            error: null,
          },
          { kind: "compaction", before_tokens: 100, after_tokens: 50 },
          { kind: "quality_gate", outcome: "pass", passed: true },
          { kind: "verifying", cmd: "pytest" },
          { kind: "verification", passed: true, cmd: "pytest" },
        ],
      }),
    ).toBe("activity");
  });
});

describe("groupAgentActivity union collapse (v0.9.319)", () => {
  it("folds thinking, cards, checkpoint, verify, and swarm into activity strips", () => {
    const items: Item[] = [
      card,
      { kind: "checkpoint", id: "ck1", label: "before tools", trigger: "manual" },
      { kind: "thinking", id: "think-1", text: "Checking routes." },
      { kind: "compaction", before_tokens: 9000, after_tokens: 3000 },
      { kind: "quality_gate", outcome: "pass", passed: true, cmd: "pytest" },
      { kind: "verifying", cmd: "pytest" },
      { kind: "verification", passed: true, cmd: "pytest", output: "ok" },
      {
        kind: "swarm_pending",
        job_ids: ["job-live"],
        objective: "audit",
        status: "running",
      },
      {
        kind: "swarm_result",
        job_id: "job-live",
        applied: false,
        files: [],
        summary: "",
        error: "routing failed",
        objective: "audit",
      },
    ];

    const grouped = groupAgentActivity(items, new Set());
    expect(grouped).toHaveLength(1);
    expect(grouped[0].kind).toBe("activity_group");
    expect(columnRowKind(grouped[0]!)).toBe("activity");
    expect(activityInnerKinds(grouped[0]!)).toEqual([
      "card",
      "checkpoint",
      "thinking",
      "compaction",
      "quality_gate",
      "verifying",
      "verification",
      "swarm_pending",
      "swarm_result",
    ]);
    expect(grouped.some((row) => row.kind === "swarm_result")).toBe(false);
  });

  it("keeps user and assistant messages as msg rows", () => {
    const items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "fix the import" } },
      card,
      { kind: "msg", msg: { role: "assistant", text: "Updated the path." } },
    ];
    const grouped = groupAgentActivity(items, new Set());
    expect(columnRowKinds(grouped)).toEqual(["msg", "activity", "msg"]);
  });

  it("keeps command_approval and secret_request as question rows", () => {
    const items: Item[] = [
      card,
      {
        kind: "command_approval",
        id: "appr-1",
        command: "rm -rf /",
        commandHash: "h",
        sessionId: "s",
        workspaceRoot: "/tmp",
        category: "destructive",
        reason: "needs a decision",
        matched: "rm",
        status: "pending",
      },
      {
        kind: "secret_request",
        id: "sec-1",
        label: "OpenAI key",
        connector: "openai",
        field: "api_key",
        description: "required",
        sessionId: "s",
        status: "pending",
      },
    ];
    const grouped = groupAgentActivity(items, new Set());
    expect(columnRowKinds(grouped)).toEqual(["activity", "question", "question"]);
  });

  it("keeps pending_review as a file row", () => {
    const items: Item[] = [
      card,
      { kind: "pending_review", id: "rev-1", summary: "2 files held" },
    ];
    const grouped = groupAgentActivity(items, new Set());
    expect(columnRowKinds(grouped)).toEqual(["activity", "file"]);
  });

  it("preserves chronological user → activity → answer ordering", () => {
    const items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "audit screenshot" } },
      {
        kind: "card",
        card: { id: "swarm-a", goal: "inspect", running: false, open: false },
      },
      {
        kind: "swarm_result",
        job_id: "job-1",
        applied: true,
        files: [],
        summary: "complete",
        error: null,
      },
      { kind: "msg", msg: { role: "assistant", text: "Found the regression." } },
    ];
    const grouped = groupAgentActivity(items, new Set());
    expect(columnRowKinds(grouped)).toEqual(["msg", "activity", "msg"]);
    if (grouped[1]?.kind !== "activity_group") return;
    expect(grouped[1].items.map((item) => item.kind)).toEqual([
      "card",
      "swarm_result",
    ]);
  });
});
