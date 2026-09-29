import { expect, it } from "vitest";
import { countPaintableTranscriptItems, type Item } from "../components/TranscriptList";

it("recounting the same transcript array does not regroup it", () => {
  const base: Item[] = Array.from({ length: 50 }, (_, i) => ({
    kind: "msg" as const,
    msg: { id: `m${i}`, role: i % 2 ? "assistant" as const : "user" as const, text: `message ${i}` },
  }));
  let reads = 0;
  const items = new Proxy(base, {
    get(target, key, receiver) {
      if (typeof key === "string" && /^\d+$/.test(key)) reads += 1;
      return Reflect.get(target, key, receiver);
    },
  });
  const first = countPaintableTranscriptItems(items);
  expect(first).toBe(50);
  const readsAfterFirst = reads;
  expect(readsAfterFirst).toBeGreaterThan(0);
  // A keystroke re-render passes the same array: answer from cache.
  expect(countPaintableTranscriptItems(items)).toBe(first);
  expect(reads).toBe(readsAfterFirst);
  // A new array (a real transcript change) is counted fresh.
  expect(countPaintableTranscriptItems([...base.slice(0, 10)])).toBe(10);
});
