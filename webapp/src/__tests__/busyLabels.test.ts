import { describe, expect, it } from "vitest";
import { deriveBusyProgress, headlineFocusCard, type TurnItem } from "../lib/turnProgress";

const user: TurnItem = { kind: "msg", msg: { role: "user", text: "run the probes" } };
const done = (id: string, goal: string): TurnItem => ({
  kind: "card",
  card: { id, kind: "run_command", goal, running: false, result: { exit_code: 0 } },
});

// Measured in the rig: the footer read "Still working…" while narration was
// visibly streaming, and the fold header flipped "run" / "run echo probe N".
describe("busy labels say what is happening", () => {
  it("says Writing while the pilot's prose streams", () => {
    const p = deriveBusyProgress(
      [user, done("a", "echo probe 1"), { kind: "msg", msg: { role: "assistant", text: "Halfway there", streaming: true } }],
      "thinking",
      3000,
    );
    expect(p.label.startsWith("Writing…")).toBe(true);
  });

  it("keeps Still working for a quiet gap with nothing streaming", () => {
    const p = deriveBusyProgress([user, done("a", "echo probe 1")], "thinking", 3000);
    expect(p.label.startsWith("Still working…")).toBe(true);
  });

  it("names the latest command with arguments while the next one's arguments stream", () => {
    const goal = (c: { goal?: string }) => c.goal || "";
    const a = { id: "a", goal: "echo probe 1" };
    const b = { id: "b", goal: "" };
    expect(headlineFocusCard([a, b], b, goal)).toBe(a);
    const b2 = { id: "b", goal: "echo probe 2" };
    expect(headlineFocusCard([a, b2], b2, goal)).toBe(b2);
    expect(headlineFocusCard([a], undefined, goal)).toBeUndefined();
  });
});
