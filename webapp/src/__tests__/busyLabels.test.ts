import { describe, expect, it } from "vitest";
import { currentTurnStep, deriveBusyProgress, headlineFocusCard, liveActivityLine, toolVerb, type TurnItem } from "../lib/turnProgress";

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

  it("does not name a finished tool of another kind while a goal-less tool runs", () => {
    // Measured in the rig: the live line kept "Running run command batch
    // command batch (3 commands)" for the whole swarm after the batch ended.
    const goal = (c: { goal?: string }) => c.goal || "";
    const batch = { id: "a", kind: "run_command_batch", goal: "command batch (3 commands)" };
    const swarm = { id: "b", kind: "run_swarm", goal: "" };
    expect(headlineFocusCard([batch, swarm], swarm, goal)).toBe(swarm);
  });

  it("says each tool's verb once", () => {
    expect(liveActivityLine({ runningKind: "run_command_batch", runningGoal: "command batch (3 commands)" }).text)
      .toBe("Running command batch (3 commands)");
    expect(toolVerb("run_command_batch", false)).toBe("Ran");
    expect(liveActivityLine({ runningKind: "run_swarm" }).text).toBe("Running swarm");
    expect(toolVerb("run_swarm", false)).toBe("Ran swarm");
    expect(toolVerb("run_implement", true)).toBe("Running implement");
  });
});

describe("currentTurnStep", () => {
  it("names the latest tool of the open turn, skipping todo bookkeeping", () => {
    const items: TurnItem[] = [
      { kind: "msg", msg: { role: "user", text: "go" } },
      { kind: "card", card: { id: "t", kind: "todo", goal: "init", running: false } },
      done("a", "echo probe 1"),
      done("b", "echo probe 2"),
    ];
    expect(currentTurnStep(items)).toMatch(/echo probe 2$/);
    expect(currentTurnStep(items.slice(0, 2))).toBeNull();
  });
});
