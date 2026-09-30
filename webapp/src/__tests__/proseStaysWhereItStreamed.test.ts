import { describe, expect, it } from "vitest";
import {
  collectIntermediateAssistantItems,
  groupAgentActivity,
  type Item,
} from "../components/TranscriptList";

// Prose that streamed as a top-level bubble used to be re-filed into the
// Investigating fold as soon as a tool card followed it: users saw it flash
// as if it were the answer, then vanish into the collapse. Painted prose now
// stays put; the tools after it open a fold below it, in order.

const user: Item = { kind: "msg", msg: { role: "user", text: "fix the parser" } };
const card = (id: string): Item => ({
  kind: "card",
  card: { id, goal: `${id}.ts`, cwd: null, kind: "read_file", running: false, open: false, result: { status: "ok" } },
});

function shape(items: Item[], loopOpen: boolean): string[] {
  return groupAgentActivity(items, collectIntermediateAssistantItems(items, loopOpen)).map((g) =>
    g.kind === "activity_group"
      ? `fold[${g.items.map((it) => (it.kind === "card" ? it.card.id : it.kind)).join(",")}]`
      : g.kind === "msg"
        ? `${g.msg.role}:${g.msg.text}`
        : g.kind,
  );
}

describe("prose stays where it streamed", () => {
  it("keeps streamed narration top-level when the next tool card arrives", () => {
    const streaming: Item = { kind: "msg", msg: { role: "assistant", text: "Now let me check the lexer", streaming: true } };
    const sealed: Item = { kind: "msg", msg: { role: "assistant", text: "Now let me check the lexer" } };

    expect(shape([user, card("a"), streaming], true)).toEqual([
      "user:fix the parser", "fold[a]", "assistant:Now let me check the lexer",
    ]);
    expect(shape([user, card("a"), sealed, card("b")], true)).toEqual([
      "user:fix the parser", "fold[a]", "assistant:Now let me check the lexer", "fold[b]",
    ]);
  });

  it("lays history out the same way after the turn ends", () => {
    const sealed: Item = { kind: "msg", msg: { role: "assistant", text: "Checking the lexer" } };
    const answer: Item = { kind: "msg", msg: { role: "assistant", text: "Fixed." } };
    expect(shape([user, card("a"), sealed, card("b"), answer], false)).toEqual([
      "user:fix the parser", "fold[a]", "assistant:Checking the lexer", "fold[b]", "assistant:Fixed.",
    ]);
  });
});
