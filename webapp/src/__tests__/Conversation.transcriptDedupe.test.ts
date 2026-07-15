import { describe, expect, it } from "vitest";
import {
  dedupeDisplayItems,
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
});

describe("transcriptFingerprint", () => {
  it("matches for identical structure and differs when a card appears", () => {
    const a = [msg("user", "hi"), card("c1")];
    const b = [msg("user", "hi"), card("c1")];
    const c = [msg("user", "hi"), card("c1"), card("c2")];
    expect(transcriptFingerprint(a)).toBe(transcriptFingerprint(b));
    expect(transcriptFingerprint(a)).not.toBe(transcriptFingerprint(c));
  });
});

describe("transcriptResponseToItems", () => {
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
});
