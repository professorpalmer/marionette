import { describe, expect, it } from "vitest";
import {
  collectIntermediateAssistantItems,
  groupAgentActivity,
  wrapSealedTurns,
  type GroupedItem,
  type Item,
} from "../components/TranscriptList";

// Prose that streamed as a top-level bubble used to be re-filed into the
// Investigating fold as soon as a tool card followed it: users saw it flash
// as if it were the answer, then vanish into the collapse. While a turn runs,
// painted prose stays put and each run of tools between prose lines is its
// own fold below it (stream order, as in Cursor). When the turn ends, one
// Worked for fold takes everything except the final answer.

const user: Item = { kind: "msg", msg: { role: "user", text: "fix the parser" } };
const card = (id: string): Item => ({
  kind: "card",
  card: { id, goal: `${id}.ts`, cwd: null, kind: "read_file", running: false, open: false, result: { status: "ok" } },
});

function label(g: GroupedItem): string {
  if (g.kind === "activity_group") {
    return `fold[${g.items.map((it) => (it.kind === "card" ? it.card.id : it.kind)).join(",")}]`;
  }
  if (g.kind === "turn_work") return `work[${g.rows.map((row) => label(row)).join(" ")}]`;
  if (g.kind === "msg") return `${g.msg.role}:${g.msg.text}`;
  return g.kind;
}

function shape(items: Item[], loopOpen: boolean): string[] {
  const grouped = groupAgentActivity(items, collectIntermediateAssistantItems(items, loopOpen));
  return wrapSealedTurns(grouped, loopOpen).map(label);
}

describe("prose stays where it streamed", () => {
  it("keeps streamed narration top-level and opens a new fold below it", () => {
    const streaming: Item = { kind: "msg", msg: { role: "assistant", text: "Now let me check the lexer", streaming: true } };
    const sealed: Item = { kind: "msg", msg: { role: "assistant", text: "Now let me check the lexer" } };

    expect(shape([user, card("a"), streaming], true)).toEqual([
      "user:fix the parser", "fold[a]", "assistant:Now let me check the lexer",
    ]);
    expect(shape([user, card("a"), sealed, card("b")], true)).toEqual([
      "user:fix the parser", "fold[a]", "assistant:Now let me check the lexer", "fold[b]",
    ]);
  });

  it("puts everything but the final answer under one Worked for fold when the turn ends", () => {
    const said: Item = { kind: "msg", msg: { role: "assistant", text: "Checking the lexer" } };
    const answer: Item = { kind: "msg", msg: { role: "assistant", text: "Fixed." } };
    expect(shape([user, card("a"), said, card("b"), answer], false)).toEqual([
      "user:fix the parser",
      "work[fold[a] assistant:Checking the lexer fold[b]]",
      "assistant:Fixed.",
    ]);
  });

  it("leaves a lone tool group as the turn's own fold", () => {
    const answer: Item = { kind: "msg", msg: { role: "assistant", text: "Fixed." } };
    expect(shape([user, card("a"), answer], false)).toEqual([
      "user:fix the parser", "fold[a]", "assistant:Fixed.",
    ]);
  });

  it("keeps an explicit answer and operator rows outside the fold", () => {
    const explicit: Item = { kind: "msg", msg: { role: "assistant", text: "Here is the plan.", channel: "answer" } };
    const said: Item = { kind: "msg", msg: { role: "assistant", text: "Checking" } };
    const steer: Item = { kind: "steer", text: "also the lexer" };
    const answer: Item = { kind: "msg", msg: { role: "assistant", text: "Done." } };
    expect(shape([user, card("a"), explicit, said, card("b"), steer, card("c"), answer], false)).toEqual([
      "user:fix the parser",
      "fold[a]",
      "assistant:Here is the plan.",
      "work[assistant:Checking fold[b]]",
      "steer",
      "fold[c]",
      "assistant:Done.",
    ]);
  });

  it("a new user turn opens its own fold", () => {
    const next: Item = { kind: "msg", msg: { role: "user", text: "and the lexer" } };
    const said: Item = { kind: "msg", msg: { role: "assistant", text: "Done." } };
    expect(shape([user, card("a"), said, next, card("b")], false)).toEqual([
      "user:fix the parser", "fold[a]", "assistant:Done.", "user:and the lexer", "fold[b]",
    ]);
  });
});
