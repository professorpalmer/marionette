import { describe, expect, it } from "vitest";
import type { Item } from "../components/TranscriptList";
import { appendActionStartCard, applyActionResultCard } from "../components/conversation/streamApply";

// Rig repro: a local server restarting call ids at call_0 each turn made the
// second turn's tool cards vanish into the first turn's.
const priorTurn: Item[] = [
  { kind: "msg", msg: { role: "user", text: "one" } },
  { kind: "card", card: { id: "call_0", call_id: "call_0", kind: "run_command", goal: "echo 1", running: false, open: false, result: { exit_code: 0, duration_ms: 5 } } },
  { kind: "msg", msg: { role: "assistant", text: "done" } },
  { kind: "msg", msg: { role: "user", text: "two" } },
];

const cards = (items: Item[]) => items.flatMap((it) => (it.kind === "card" ? [it.card] : []));

describe("provider call ids reused across turns", () => {
  it("opens and settles the new turn's card without touching the old one", () => {
    let items = appendActionStartCard(priorTurn, { id: "call_0~2", call_id: "call_0", kind: "run_command", goal: "echo 2" });
    expect(cards(items).map((c) => c.id)).toEqual(["call_0", "call_0~2"]);
    items = applyActionResultCard(items, { id: "call_0~2", call_id: "call_0", exit_code: 0, duration_ms: 7 });
    const [old, fresh] = cards(items);
    expect(old.result?.duration_ms).toBe(5);
    expect(fresh.running).toBe(false);
    expect(fresh.result?.duration_ms).toBe(7);
  });

  it("does not settle an old card from a call-id-only result in a new turn", () => {
    const items = applyActionResultCard(priorTurn, { call_id: "call_0", exit_code: 1, duration_ms: 9 });
    expect(cards(items)[0].result?.duration_ms).toBe(5);
  });
});
