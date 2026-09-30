import { describe, expect, it } from "vitest";
import { transcriptRowKeys, type GroupedItem } from "../components/TranscriptList";

const card = (id: string) => ({ kind: "card" as const, card: { id, goal: id, running: false, open: false, kind: "read_file" } });
const fold = (id: string): GroupedItem => ({ kind: "activity_group", items: [card(id)] } as unknown as GroupedItem);
const msg = (id: string): GroupedItem => ({ kind: "msg", msg: { role: "assistant", text: id, id } } as GroupedItem);

// Keys carrying the row index remounted every fold below an inserted or
// dropped row (and dropped its measured height), shifting the rows below.
describe("transcript row keys", () => {
  it("keeps a row's key when a row above it is inserted or dropped", () => {
    const before = transcriptRowKeys([msg("m1"), fold("a"), msg("m2"), fold("b")]);
    const after = transcriptRowKeys([msg("m1"), msg("crumb"), fold("a"), msg("m2"), fold("b")]);
    expect(after.filter((k) => k !== after[1])).toEqual(before);
  });

  it("stays unique when two rows share a base key", () => {
    const keys = transcriptRowKeys([{ kind: "steer", text: "x" } as GroupedItem, { kind: "steer", text: "y" } as GroupedItem]);
    expect(new Set(keys).size).toBe(2);
  });
});
