import { describe, expect, it } from "vitest";
import { CONTINUE_PROMPT, recoveryActionFor, type TurnItemLike } from "../lib/turnTerminal";

const user = (text: string): TurnItemLike => ({ kind: "msg", msg: { role: "user", text } });
const assistant = (text: string): TurnItemLike => ({ kind: "msg", msg: { role: "assistant", text } });

describe("recoveryActionFor: one failure gives one recovery action", () => {
  it("offers Retry when the request failed before any work (the batch-variant 404)", () => {
    expect(recoveryActionFor([
      user("Find the bug"),
      assistant("[error] pilot: the selected model does not take chat requests."),
    ])).toBe("retry");
  });

  it("offers Continue when the turn left an answer or a tool run", () => {
    expect(recoveryActionFor([user("Fix it"), assistant("Reading calc.py first."), assistant("[error] stream ended")])).toBe("continue");
    expect(recoveryActionFor([user("Fix it"), { kind: "card", id: "c1" }, assistant("[error] stream ended")])).toBe("continue");
  });

  it("looks only at the latest turn", () => {
    expect(recoveryActionFor([
      user("First"), assistant("Done."),
      user("Second"), assistant("[aborted] stopped"),
    ])).toBe("retry");
  });

  it("offers Retry for the original ask when a Continue itself failed early", () => {
    expect(recoveryActionFor([user("Fix it"), assistant("[error] x"), user(CONTINUE_PROMPT), assistant("[error] y")])).toBe("retry");
  });

  it("offers nothing when there is no ask and no work", () => {
    expect(recoveryActionFor([assistant("[error] boot failed")])).toBeNull();
  });
});
