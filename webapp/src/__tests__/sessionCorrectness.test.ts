import { expect, it } from "vitest";
import type { Item } from "../components/TranscriptList";
import { appendCheckpoint } from "../components/conversation/streamApply";
import { dedupeDisplayItems, transcriptResponseToItems } from "../components/conversation/transcriptItems";

it("dedupes full checkpoint identities within each user turn only", () => {
  const user: Item = { kind: "msg", msg: { role: "user", text: "continue" } };
  const checkpoint = { id: "1234567890-full-a", label: "Restored", trigger: "restore" };
  const once = appendCheckpoint([user], checkpoint);
  expect(appendCheckpoint(once, checkpoint)).toEqual(once);
  const different = appendCheckpoint(once, { ...checkpoint, id: "1234567890-full-b" });
  expect(different).toHaveLength(3);
  const later = appendCheckpoint([...different, user], checkpoint);
  expect(later).toHaveLength(5);
  expect(dedupeDisplayItems([...once, once[1], user, once[1]])).toHaveLength(4);
  expect(dedupeDisplayItems(dedupeDisplayItems(later))).toEqual(later);
});

it("hydrates checkpoint identities without turning them into messages", () => {
  const checkpoint = { type: "checkpoint", id: "full-id", label: "Restore", trigger: "restore" };
  const items = transcriptResponseToItems({ display: [
    { role: "user", text: "first" }, checkpoint, checkpoint,
    { role: "user", text: "second" }, checkpoint,
  ] });
  expect(items.filter(item => item.kind === "checkpoint")).toHaveLength(2);
});
