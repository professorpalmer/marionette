import { describe, expect, it } from "vitest";
import {
  dedupeDisplayItems,
  mergeSwarmResultReuse,
  mergeTranscriptItems,
  shouldPreferLocalTranscript,
  transcriptFingerprint,
  transcriptResponseToItems,
} from "../components/Conversation";
import type { Item } from "../components/TranscriptList";

function card(id: string, goal = "g"): Item {
  return {
    kind: "card",
    card: { id, goal, cwd: null, kind: "write_file", running: false, open: false },
  };
}

function msg(role: "user" | "assistant", text: string): Item {
  return { kind: "msg", msg: { role, text } };
}

describe("dedupeDisplayItems", () => {
  it("drops later cards that reuse an earlier action id", () => {
    const items: Item[] = [
      msg("user", "go"),
      card("a1", "write translator.py"),
      card("a2", "write config"),
      card("a1", "write translator.py AGAIN"),
      msg("assistant", "done"),
    ];
    const out = dedupeDisplayItems(items);
    expect(out.filter((i) => i.kind === "card")).toHaveLength(2);
    expect(out.map((i) => (i.kind === "card" ? i.card.id : i.kind))).toEqual([
      "msg", "a1", "a2", "msg",
    ]);
  });

  it("drops duplicate swarm_result job ids", () => {
    const items: Item[] = [
      {
        kind: "swarm_result",
        job_id: "local-1",
        applied: false,
        files: [],
        summary: "failed",
        error: "x",
      },
      {
        kind: "swarm_result",
        job_id: "local-1",
        applied: false,
        files: [],
        summary: "failed again",
        error: "x",
      },
    ];
    expect(dedupeDisplayItems(items)).toHaveLength(1);
  });

  it("merges richer reuse provenance onto duplicate swarm_result", () => {
    const items: Item[] = [
      {
        kind: "swarm_result",
        job_id: "local-1",
        applied: true,
        files: [],
        summary: "thin",
        error: null,
      },
      {
        kind: "swarm_result",
        job_id: "local-1",
        applied: true,
        files: [],
        summary: "reused prior",
        error: null,
        reuse_status: "partial",
        source_job_id: "local-src",
        reuse_reason: "subset_invalidated",
        invalidated_paths: ["harness/auth.py", "harness/pilot.py"],
      },
    ];
    const out = dedupeDisplayItems(items);
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({
      kind: "swarm_result",
      job_id: "local-1",
      reuse_status: "partial",
      source_job_id: "local-src",
      invalidated_paths: ["harness/auth.py", "harness/pilot.py"],
    });
  });

  it("hydrate dedupe: later fresh/false/[] corrects prior partial provenance", () => {
    const items: Item[] = [
      {
        kind: "swarm_result",
        job_id: "local-1",
        applied: true,
        files: [],
        summary: "partial reuse",
        error: null,
        reuse_status: "partial",
        source_job_id: "local-src",
        reuse_reason: "subset_invalidated",
        invalidated_paths: ["harness/auth.py"],
      },
      {
        kind: "swarm_result",
        job_id: "local-1",
        applied: false,
        files: [],
        summary: "corrected fresh failure",
        error: "swarm findings are thin",
        reuse_status: "fresh",
        source_job_id: "",
        reuse_reason: "",
        invalidated_paths: [],
      },
    ];
    const out = dedupeDisplayItems(items);
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({
      kind: "swarm_result",
      job_id: "local-1",
      applied: false,
      reuse_status: "fresh",
      source_job_id: "",
      invalidated_paths: [],
      error: "swarm findings are thin",
    });
  });

  it("collapses duplicate swarm_pending rows by normalized job ids", () => {
    const items: Item[] = [
      {
        kind: "swarm_pending",
        job_ids: ["b", "a"],
        objective: "wave",
        status: "running",
        terminal_job_ids: [],
      },
      {
        kind: "swarm_pending",
        job_ids: ["a", "b"],
        objective: "wave",
        status: "failed",
        resolved: true,
        terminal_job_ids: ["a"],
      },
    ];
    const out = dedupeDisplayItems(items);
    expect(out).toHaveLength(1);
    expect(out[0]).toMatchObject({
      kind: "swarm_pending",
      job_ids: ["a", "b"],
      status: "failed",
      terminal_job_ids: ["a"],
    });
  });

  it("does not collapse distinct swarm jobs that share an objective", () => {
    const items: Item[] = [
      {
        kind: "swarm_pending",
        job_ids: ["job_one"],
        objective: "same goal",
        status: "running",
        terminal_job_ids: [],
      },
      {
        kind: "swarm_pending",
        job_ids: ["job_two"],
        objective: "same goal",
        status: "running",
        terminal_job_ids: [],
      },
    ];
    expect(dedupeDisplayItems(items)).toHaveLength(2);
  });

  it("collapses interleaved poll/SSE duplicate tool rows by tool call id", () => {
    // Abnormal re-render churn: SSE running card, poll completed card, then
    // another SSE echo of the same id — must be one row, preferring completed.
    const items: Item[] = [
      msg("user", "go"),
      {
        kind: "card",
        card: {
          id: "tool-42",
          goal: "pytest",
          cwd: null,
          kind: "run_command",
          running: true,
          open: false,
        },
      },
      msg("assistant", "running tests"),
      {
        kind: "card",
        card: {
          id: "tool-42",
          goal: "pytest",
          cwd: null,
          kind: "run_command",
          running: false,
          open: false,
          result: { adapter: "local", duration_ms: 40 },
        },
      },
      {
        kind: "card",
        card: {
          id: "tool-42",
          goal: "pytest",
          cwd: null,
          kind: "run_command",
          running: true,
          open: false,
        },
      },
    ];
    const out = dedupeDisplayItems(items);
    const cards = out.filter((i) => i.kind === "card") as Extract<Item, { kind: "card" }>[];
    expect(cards).toHaveLength(1);
    expect(cards[0].card.id).toBe("tool-42");
    expect(cards[0].card.running).toBe(false);
    expect(cards[0].card.result?.duration_ms).toBe(40);
  });

  it("mergeTranscriptItems dedupes local duplicate tool ids from poll/SSE churn", () => {
    const local: Item[] = [
      msg("user", "go"),
      {
        kind: "card",
        card: {
          id: "run-9",
          goal: "ls",
          cwd: null,
          kind: "run_command",
          running: true,
          open: false,
        },
      },
      {
        kind: "card",
        card: {
          id: "run-9",
          goal: "ls",
          cwd: null,
          kind: "run_command",
          running: true,
          open: false,
        },
      },
    ];
    const remote: Item[] = [msg("user", "go")];
    expect(shouldPreferLocalTranscript(local, remote)).toBe(true);
    const merged = mergeTranscriptItems(local, remote);
    expect(merged.filter((i) => i.kind === "card")).toHaveLength(1);
  });
});

describe("transcriptFingerprint", () => {
  it("matches for identical structure and differs when a card appears", () => {
    const a = [msg("user", "hi"), card("c1")];
    const b = [msg("user", "hi"), card("c1")];
    const c = [msg("user", "hi"), card("c1"), card("c2")];
    expect(transcriptFingerprint(a)).toBe(transcriptFingerprint(b));
    expect(transcriptFingerprint(a)).not.toBe(transcriptFingerprint(c));
  });

  it("changes when swarm_result reuse provenance is enriched", () => {
    const thin: Item[] = [{
      kind: "swarm_result",
      job_id: "local-1",
      applied: true,
      files: [],
      summary: "ok",
      error: null,
    }];
    const rich: Item[] = [{
      kind: "swarm_result",
      job_id: "local-1",
      applied: true,
      files: [],
      summary: "ok",
      error: null,
      reuse_status: "partial",
      source_job_id: "local-src",
      invalidated_paths: ["harness/auth.py"],
    }];
    expect(transcriptFingerprint(thin)).not.toBe(transcriptFingerprint(rich));
  });

  it("changes when swarm_result error or reuse_reason alone changes", () => {
    const base: Item = {
      kind: "swarm_result",
      job_id: "local-1",
      applied: false,
      files: [],
      summary: "ok",
      error: null,
      reuse_status: "fresh",
      reuse_reason: "full_swarm",
      validation_fingerprint: "fp-1",
    };
    const errorOnly: Item[] = [{ ...base, error: "boom" }];
    const reasonOnly: Item[] = [{ ...base, reuse_reason: "fingerprint_match" }];
    expect(transcriptFingerprint([base])).not.toBe(transcriptFingerprint(errorOnly));
    expect(transcriptFingerprint([base])).not.toBe(transcriptFingerprint(reasonOnly));
  });

  it("changes on validation_fingerprint-only clear", () => {
    const withFp: Item[] = [{
      kind: "swarm_result",
      job_id: "local-1",
      applied: true,
      files: [],
      summary: "ok",
      error: null,
      reuse_status: "reused",
      source_job_id: "local-src",
      reuse_reason: "fingerprint_match",
      validation_fingerprint: "fp-partial",
      invalidated_paths: [],
    }];
    const cleared: Item[] = [{
      kind: "swarm_result",
      job_id: "local-1",
      applied: true,
      files: [],
      summary: "ok",
      error: null,
      reuse_status: "reused",
      source_job_id: "local-src",
      reuse_reason: "fingerprint_match",
      validation_fingerprint: "",
      invalidated_paths: [],
    }];
    expect(transcriptFingerprint(withFp)).not.toBe(transcriptFingerprint(cleared));
  });

  it("changes when environment_fingerprint or acceptance_criteria alone change", () => {
    const base: Item = {
      kind: "swarm_result",
      job_id: "local-1",
      applied: false,
      files: [],
      summary: "ok",
      error: null,
      reuse_status: "fresh",
      reuse_reason: "environment_changed",
      environment_fingerprint: "env-fp-a",
      acceptance_criteria: ["tests pass"],
    };
    const envOnly: Item[] = [{ ...base, environment_fingerprint: "env-fp-b" }];
    const envCleared: Item[] = [{ ...base, environment_fingerprint: "" }];
    const criteriaOnly: Item[] = [{ ...base, acceptance_criteria: ["docs updated"] }];
    const criteriaCleared: Item[] = [{ ...base, acceptance_criteria: [] }];
    expect(transcriptFingerprint([base])).not.toBe(transcriptFingerprint(envOnly));
    expect(transcriptFingerprint([base])).not.toBe(transcriptFingerprint(envCleared));
    expect(transcriptFingerprint([base])).not.toBe(transcriptFingerprint(criteriaOnly));
    expect(transcriptFingerprint([base])).not.toBe(transcriptFingerprint(criteriaCleared));
  });

  it("changes on remote swarm_pending running-to-terminal-only transition", () => {
    const running: Item[] = [{
      kind: "swarm_pending",
      job_ids: ["j2", "j1"],
      objective: "ship",
      status: "running",
      resolved: false,
      terminal_job_ids: [],
    }];
    const terminal: Item[] = [{
      kind: "swarm_pending",
      job_ids: ["j2", "j1"],
      objective: "ship",
      status: "done",
      resolved: true,
      terminal_job_ids: ["j1", "j2"],
    }];
    expect(transcriptFingerprint(running)).not.toBe(transcriptFingerprint(terminal));
  });

  it("keeps bounded deterministic ordering for path and job id lists", () => {
    const a: Item[] = [{
      kind: "swarm_result",
      job_id: "local-1",
      applied: true,
      files: ["b.py", "a.py"],
      summary: "ok",
      error: null,
      invalidated_paths: ["z.py", "a.py"],
    }, {
      kind: "swarm_pending",
      job_ids: ["j2", "j1"],
      objective: "ship",
      status: "running",
      terminal_job_ids: ["j2", "j1"],
    }];
    const b: Item[] = [{
      kind: "swarm_result",
      job_id: "local-1",
      applied: true,
      files: ["a.py", "b.py"],
      summary: "ok",
      error: null,
      invalidated_paths: ["a.py", "z.py"],
    }, {
      kind: "swarm_pending",
      job_ids: ["j1", "j2"],
      objective: "ship",
      status: "running",
      terminal_job_ids: ["j1", "j2"],
    }];
    expect(transcriptFingerprint(a)).toBe(transcriptFingerprint(b));
  });
});

describe("transcriptResponseToItems", () => {
  it("hydrates swarm_pending display rows instead of falling through to msg", () => {
    const items = transcriptResponseToItems({
      display: [{
        type: "swarm_pending",
        job_ids: ["job_dugout"],
        objective: "Dugout swarm",
        status: "done",
        session_id: "other-session",
      }],
    });
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      kind: "swarm_pending",
      job_ids: ["job_dugout"],
      objective: "Dugout swarm",
      status: "done",
      resolved: true,
    });
  });

  it("hydrates invalidated_paths onto swarm_result rows", () => {
    const items = transcriptResponseToItems({
      display: [{
        type: "swarm_result",
        job_id: "local-1",
        applied: true,
        files: [],
        summary: "partial reuse",
        error: null,
        reuse_status: "partial",
        source_job_id: "local-src",
        invalidated_paths: ["harness/auth.py", "harness/pilot.py"],
      }],
    });
    expect(items[0]).toMatchObject({
      kind: "swarm_result",
      reuse_status: "partial",
      invalidated_paths: ["harness/auth.py", "harness/pilot.py"],
    });
  });

  it("hydrates environment_fingerprint and acceptance_criteria onto swarm_result", () => {
    const items = transcriptResponseToItems({
      display: [{
        type: "swarm_result",
        job_id: "local-1",
        applied: false,
        files: [],
        summary: "full swarm after env drift",
        error: null,
        reuse_status: "fresh",
        reuse_reason: "environment_changed",
        environment_fingerprint: "env-fp-live",
        acceptance_criteria: ["  keep env stamp  ", "", "tests pass"],
      }],
    });
    expect(items[0]).toMatchObject({
      kind: "swarm_result",
      reuse_status: "fresh",
      reuse_reason: "environment_changed",
      environment_fingerprint: "env-fp-live",
      acceptance_criteria: ["keep env stamp", "tests pass"],
    });
  });

  it("hydrate+dedupe: later environment_fingerprint/acceptance_criteria clear prior", () => {
    const items = transcriptResponseToItems({
      display: [
        {
          type: "swarm_result",
          job_id: "local-1",
          applied: true,
          files: [],
          summary: "reused",
          error: null,
          reuse_status: "reused",
          source_job_id: "local-src",
          reuse_reason: "fingerprint_match",
          environment_fingerprint: "env-old",
          acceptance_criteria: ["old criterion"],
          validation_fingerprint: "fp-old",
        },
        {
          type: "swarm_result",
          job_id: "local-1",
          applied: false,
          files: [],
          summary: "fresh after environment_changed",
          error: null,
          reuse_status: "fresh",
          source_job_id: "",
          reuse_reason: "environment_changed",
          environment_fingerprint: "",
          acceptance_criteria: [],
          validation_fingerprint: "",
        },
      ],
    });
    expect(items).toHaveLength(1);
    const row = items[0] as Extract<Item, { kind: "swarm_result" }>;
    expect(row).toMatchObject({
      reuse_status: "fresh",
      reuse_reason: "environment_changed",
      environment_fingerprint: "",
      acceptance_criteria: [],
      validation_fingerprint: "",
      source_job_id: "",
    });
  });

  it("mergeSwarmResultReuse updates and clears environment_fingerprint/criteria", () => {
    const prev: Extract<Item, { kind: "swarm_result" }> = {
      kind: "swarm_result",
      job_id: "local-1",
      applied: true,
      files: [],
      summary: "reused",
      error: null,
      reuse_status: "reused",
      source_job_id: "local-src",
      reuse_reason: "fingerprint_match",
      environment_fingerprint: "env-old",
      acceptance_criteria: ["keep"],
      validation_fingerprint: "fp-old",
    };
    const enriched = mergeSwarmResultReuse(prev, {
      ...prev,
      environment_fingerprint: "env-new",
      acceptance_criteria: ["tests pass", "docs ok"],
    });
    expect(enriched.environment_fingerprint).toBe("env-new");
    expect(enriched.acceptance_criteria).toEqual(["tests pass", "docs ok"]);

    // Omitted fields inherit prior provenance.
    const thin = mergeSwarmResultReuse(enriched, {
      kind: "swarm_result",
      job_id: "local-1",
      applied: true,
      files: [],
      summary: "thin patch",
      error: null,
    });
    expect(thin.environment_fingerprint).toBe("env-new");
    expect(thin.acceptance_criteria).toEqual(["tests pass", "docs ok"]);
    expect(thin.summary).toBe("thin patch");

    // Explicit clears (including fresh environment_changed) replace prior values.
    const cleared = mergeSwarmResultReuse(enriched, {
      kind: "swarm_result",
      job_id: "local-1",
      applied: false,
      files: [],
      summary: "fresh",
      error: null,
      reuse_status: "fresh",
      source_job_id: "",
      reuse_reason: "environment_changed",
      environment_fingerprint: "",
      acceptance_criteria: [],
      validation_fingerprint: "",
    });
    expect(cleared).toMatchObject({
      reuse_status: "fresh",
      reuse_reason: "environment_changed",
      environment_fingerprint: "",
      acceptance_criteria: [],
      validation_fingerprint: "",
      source_job_id: "",
    });
  });

  it("hydrate+dedupe: later fresh/false/[]/empty provenance clears prior partial", () => {
    // Explicit empty-string source_job_id / reuse_reason must survive hydrate
    // (not coerce via || undefined) so dedupe merge can clear stale UI provenance.
    const items = transcriptResponseToItems({
      display: [
        {
          type: "swarm_result",
          job_id: "local-1",
          applied: true,
          files: ["old.ts"],
          summary: "partial reuse",
          error: null,
          reuse_status: "partial",
          source_job_id: "local-src",
          reuse_reason: "subset_invalidated",
          invalidated_paths: ["harness/auth.py"],
        },
        {
          type: "swarm_result",
          job_id: "local-1",
          applied: false,
          files: [],
          summary: "corrected fresh failure",
          error: "swarm findings are thin",
          reuse_status: "fresh",
          source_job_id: "",
          reuse_reason: "",
          invalidated_paths: [],
        },
      ],
    });
    expect(items).toHaveLength(1);
    const row = items[0] as Extract<Item, { kind: "swarm_result" }>;
    expect(row).toMatchObject({
      kind: "swarm_result",
      job_id: "local-1",
      applied: false,
      reuse_status: "fresh",
      source_job_id: "",
      reuse_reason: "",
      invalidated_paths: [],
      error: "swarm findings are thin",
      files: [],
    });
    // UI-facing: empty clears must not leave the prior source/reason strings.
    expect(row.source_job_id).toBe("");
    expect(row.reuse_reason).toBe("");
    expect(row.source_job_id || row.reuse_reason).toBeFalsy();
  });

  it("nullish hydrate: explicit empty reuse_status/validation_fingerprint clear", () => {
    const items = transcriptResponseToItems({
      display: [
        {
          type: "swarm_result",
          job_id: "local-1",
          applied: true,
          files: [],
          summary: "partial",
          error: null,
          reuse_status: "partial",
          source_job_id: "local-src",
          reuse_reason: "subset_invalidated",
          validation_fingerprint: "fp-partial",
          invalidated_paths: ["a.py"],
        },
        {
          type: "swarm_result",
          job_id: "local-1",
          applied: false,
          files: [],
          summary: "cleared",
          error: "thin",
          reuse_status: "",
          source_job_id: "",
          reuse_reason: "",
          validation_fingerprint: "",
          invalidated_paths: [],
        },
      ],
    });
    expect(items).toHaveLength(1);
    const row = items[0] as Extract<Item, { kind: "swarm_result" }>;
    expect(row.reuse_status).toBe("");
    expect(row.validation_fingerprint).toBe("");
    expect(row.source_job_id).toBe("");
    expect(row.invalidated_paths).toEqual([]);
  });

  it("nullish hydrate: omitted reuse_status/validation_fingerprint inherit via dedupe", () => {
    const items = transcriptResponseToItems({
      display: [
        {
          type: "swarm_result",
          job_id: "local-1",
          applied: true,
          files: [],
          summary: "partial",
          error: null,
          reuse_status: "partial",
          source_job_id: "local-src",
          validation_fingerprint: "fp-keep",
          invalidated_paths: ["a.py"],
        },
        {
          type: "swarm_result",
          job_id: "local-1",
          applied: true,
          files: [],
          summary: "later thin patch",
          error: null,
          // reuse_status / validation_fingerprint omitted → inherit
        },
      ],
    });
    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({
      reuse_status: "partial",
      validation_fingerprint: "fp-keep",
      source_job_id: "local-src",
      summary: "later thin patch",
    });
  });

  it("dedupes repeated display cards from the API payload", () => {
    const items = transcriptResponseToItems({
      display: [
        { type: "message", role: "user", text: "go" },
        { type: "card", id: "x", goal: "write", kind: "write_file", result: {} },
        { type: "card", id: "x", goal: "write", kind: "write_file", result: {} },
        { type: "message", role: "assistant", text: "ok" },
      ],
    });
    expect(items.filter((i) => i.kind === "card")).toHaveLength(1);
  });

  it("marks result-null display cards as running (in-flight action_start)", () => {
    const items = transcriptResponseToItems({
      display: [
        { type: "message", role: "user", text: "go" },
        { type: "card", id: "a1", goal: "pytest", kind: "run_command", result: null },
      ],
    });
    const c = items.find((i) => i.kind === "card") as Extract<Item, { kind: "card" }>;
    expect(c.card.running).toBe(true);
    expect(c.card.result).toBeUndefined();
  });
});

describe("shouldPreferLocalTranscript / mergeTranscriptItems", () => {
  it("keeps local when remote is missing a running card (no Investigating blink)", () => {
    const local: Item[] = [
      msg("user", "go"),
      {
        kind: "card",
        card: {
          id: "run-1",
          goal: "pytest",
          cwd: null,
          kind: "run_command",
          running: true,
          open: false,
        },
      },
    ];
    const remote: Item[] = [msg("user", "go"), msg("assistant", "narration only")];
    expect(shouldPreferLocalTranscript(local, remote)).toBe(true);
    const merged = mergeTranscriptItems(local, remote);
    expect(merged.some((i) => i.kind === "card" && i.card.id === "run-1")).toBe(true);
  });

  it("takes remote result when the same card finished on disk", () => {
    const local: Item[] = [
      {
        kind: "card",
        card: {
          id: "run-1",
          goal: "pytest",
          cwd: null,
          kind: "run_command",
          running: true,
          open: false,
        },
      },
    ];
    const remote: Item[] = [
      {
        kind: "card",
        card: {
          id: "run-1",
          goal: "pytest",
          cwd: null,
          kind: "run_command",
          running: false,
          open: false,
          result: { adapter: "local", duration_ms: 12 },
        },
      },
    ];
    // Remote still has the card id — do not prefer-local solely for running.
    expect(shouldPreferLocalTranscript(local, remote)).toBe(false);
    const merged = mergeTranscriptItems(local, remote);
    const c = merged[0] as Extract<Item, { kind: "card" }>;
    expect(c.card.running).toBe(false);
    expect(c.card.result?.duration_ms).toBe(12);
  });

  it("prefers local when remote has fewer completed cards", () => {
    const local: Item[] = [card("a"), card("b"), card("c")];
    const remote: Item[] = [card("a")];
    expect(shouldPreferLocalTranscript(local, remote)).toBe(true);
  });

  it("equal card counts take remote but keep a still-pending approval card", () => {
    const hash = "c".repeat(64);
    const local: Item[] = [
      card("run-1"),
      {
        kind: "command_approval",
        id: "call-1",
        command: "ssh prod reboot",
        commandHash: hash,
        sessionId: "s1",
        workspaceRoot: "/repo",
        category: "remote",
        reason: "ssh",
        matched: "ssh",
        status: "pending",
      },
    ];
    const remote: Item[] = [
      {
        kind: "card",
        card: {
          id: "run-1",
          goal: "g-run-1",
          cwd: null,
          kind: "read_file",
          running: false,
          open: false,
          result: { adapter: "local", duration_ms: 3 },
        },
      },
    ];
    expect(shouldPreferLocalTranscript(local, remote)).toBe(false);
    const merged = mergeTranscriptItems(local, remote);
    expect(merged.some((i) => i.kind === "command_approval" && i.status === "pending")).toBe(true);
    const c = merged.find((i) => i.kind === "card") as Extract<Item, { kind: "card" }>;
    expect(c.card.result?.duration_ms).toBe(3);
  });

  it("prefer-local splice keeps remote call_id cards before final when local missed every prep", () => {
    // Earlier-turn extra local cards force shouldPreferLocalTranscript.
    const local: Item[] = [
      msg("user", "prior"),
      card("prior-x", "old"),
      card("prior-y", "old2"),
      card("prior-z", "old3"),
      msg("assistant", "prior done"),
      msg("user", "now"),
      msg("assistant", "final answer"),
    ];
    const remote: Item[] = [
      msg("user", "prior"),
      msg("assistant", "prior done"),
      msg("user", "now"),
      {
        kind: "card",
        card: {
          id: "call-a",
          goal: "a.ts",
          cwd: null,
          kind: "Read",
          running: false,
          open: false,
          call_id: "call-a",
          result: { status: "complete" },
        },
      },
      {
        kind: "card",
        card: {
          id: "call-b",
          goal: "b.ts",
          cwd: null,
          kind: "Grep",
          running: false,
          open: false,
          call_id: "call-b",
          result: { status: "complete" },
        },
      },
      msg("assistant", "final answer"),
    ];
    expect(shouldPreferLocalTranscript(local, remote)).toBe(true);
    const merged = mergeTranscriptItems(local, remote);
    const current = merged.slice(
      merged.findIndex((i) => i.kind === "msg" && i.msg.role === "user" && i.msg.text === "now"),
    );
    const surface = current.map((i) => {
      if (i.kind === "card") return `card:${i.card.call_id || i.card.id}`;
      if (i.kind === "msg") return `msg:${i.msg.role}:${i.msg.text}`;
      return i.kind;
    });
    expect(surface).toEqual([
      "msg:user:now",
      "card:call-a",
      "card:call-b",
      "msg:assistant:final answer",
    ]);
  });

  it("prefer-local splice preserves pre-tool narration before missing call_id cards", () => {
    const local: Item[] = [
      msg("user", "prior"),
      card("prior-extra", "x"),
      card("prior-extra-2", "y"),
      msg("assistant", "prior done"),
      msg("user", "now"),
      msg("assistant", "Checking next."),
      msg("assistant", "Done."),
    ];
    const remote: Item[] = [
      msg("user", "prior"),
      msg("assistant", "prior done"),
      msg("user", "now"),
      msg("assistant", "Checking next."),
      {
        kind: "card",
        card: {
          id: "call-n",
          goal: "n.ts",
          cwd: null,
          kind: "Read",
          running: false,
          open: false,
          call_id: "call-n",
          result: { status: "complete" },
        },
      },
      msg("assistant", "Done."),
    ];
    expect(shouldPreferLocalTranscript(local, remote)).toBe(true);
    const merged = mergeTranscriptItems(local, remote);
    const current = merged.slice(
      merged.findIndex((i) => i.kind === "msg" && i.msg.role === "user" && i.msg.text === "now"),
    );
    const surface = current.map((i) => {
      if (i.kind === "card") return `card:${i.card.call_id || i.card.id}`;
      if (i.kind === "msg") return `msg:${i.msg.role}:${i.msg.text}`;
      return i.kind;
    });
    expect(surface).toEqual([
      "msg:user:now",
      "msg:assistant:Checking next.",
      "card:call-n",
      "msg:assistant:Done.",
    ]);
  });

  it("prefer-local still corrects remote authoritative swarm_result provenance", () => {
    // Extra local cards force prefer-local; remote must still correct reuse fields.
    const local: Item[] = [
      msg("user", "go"),
      card("extra-a", "local-only"),
      card("extra-b", "local-only-2"),
      {
        kind: "swarm_result",
        job_id: "local-1",
        applied: true,
        files: [],
        summary: "partial reuse",
        error: null,
        reuse_status: "partial",
        source_job_id: "local-src",
        reuse_reason: "subset_invalidated",
        invalidated_paths: ["harness/auth.py"],
        validation_fingerprint: "fp-old",
      },
    ];
    const remote: Item[] = [
      msg("user", "go"),
      {
        kind: "swarm_result",
        job_id: "local-1",
        applied: false,
        files: [],
        summary: "corrected fresh failure",
        error: "swarm findings are thin",
        reuse_status: "fresh",
        source_job_id: "",
        reuse_reason: "",
        invalidated_paths: [],
        validation_fingerprint: "",
      },
    ];
    expect(shouldPreferLocalTranscript(local, remote)).toBe(true);
    const merged = mergeTranscriptItems(local, remote);
    expect(merged.filter((i) => i.kind === "card")).toHaveLength(2);
    const result = merged.find((i) => i.kind === "swarm_result") as Extract<
      Item,
      { kind: "swarm_result" }
    >;
    expect(result).toMatchObject({
      job_id: "local-1",
      applied: false,
      reuse_status: "fresh",
      source_job_id: "",
      reuse_reason: "",
      invalidated_paths: [],
      validation_fingerprint: "",
      error: "swarm findings are thin",
    });
  });

  it("prefer-local terminalizes remote authoritative swarm_pending by identity", () => {
    const local: Item[] = [
      msg("user", "go"),
      card("extra-1", "x"),
      card("extra-2", "y"),
      {
        kind: "swarm_pending",
        job_ids: ["a", "b"],
        objective: "wave",
        status: "running",
        resolved: false,
        terminal_job_ids: [],
      },
    ];
    const remote: Item[] = [
      msg("user", "go"),
      {
        kind: "swarm_pending",
        job_ids: ["b", "a"],
        objective: "wave",
        status: "failed",
        resolved: true,
        terminal_job_ids: ["a", "b"],
      },
    ];
    expect(shouldPreferLocalTranscript(local, remote)).toBe(true);
    const merged = mergeTranscriptItems(local, remote);
    expect(merged.filter((i) => i.kind === "card")).toHaveLength(2);
    const pending = merged.filter((i) => i.kind === "swarm_pending");
    expect(pending).toHaveLength(1);
    expect(pending[0]).toMatchObject({
      job_ids: ["a", "b"],
      status: "failed",
      resolved: true,
      terminal_job_ids: ["a", "b"],
    });
  });
});
