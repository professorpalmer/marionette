import { describe, expect, it } from "vitest";
import { transcriptResponseToItems } from "../components/conversation/transcriptItems";
import { activityWorkDurationMs, turnSpanMs } from "../lib/turnProgress";

// A turn's "Worked for" must read the same after a reload as it did live.
describe("durable turn work time", () => {
  const display = [
    { type: "message", role: "user", text: "run the probes", ts: 1_000 },
    { type: "card", id: "a", kind: "run_command", goal: "echo 1", result: { duration_ms: 20 }, ts: 2_400 },
    { type: "message", role: "assistant", text: "Halfway.", ts: 3_000 },
    { type: "card", id: "b", kind: "run_command", goal: "echo 2", result: { duration_ms: 30 }, ts: 7_970 },
    { type: "message", role: "assistant", text: "Done.", ts: 8_500 },
    { type: "message", role: "user", text: "next", ts: 20_000 },
    { type: "card", id: "c", kind: "run_command", goal: "echo 3", result: { duration_ms: 10 }, ts: 21_000 },
  ];

  it("spans from the user's message to the end of the last action", () => {
    const items = transcriptResponseToItems({ display }, "s");
    const firstTurn = items.filter((it) => it.kind === "card" && ["a", "b"].includes(it.card.id));
    expect(activityWorkDurationMs(firstTurn)).toBe(50);
    expect(turnSpanMs(firstTurn)).toBe(7_000);
    const secondTurn = items.filter((it) => it.kind === "card" && it.card.id === "c");
    expect(turnSpanMs(secondTurn)).toBe(1_010);
  });

  it("is unknown for transcripts saved before rows carried times", () => {
    const items = transcriptResponseToItems({
      display: display.map(({ ts: _ts, ...row }) => row),
    }, "s");
    expect(turnSpanMs(items)).toBeNull();
  });
});
