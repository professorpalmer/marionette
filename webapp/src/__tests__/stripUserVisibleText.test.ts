import { describe, expect, it } from "vitest";
import {
  stripUserVisibleText,
  transcriptResponseToItems,
} from "../components/Conversation";

const TRAILER =
  "\n\n[context for this turn]\n" +
  "CODEGRAPH HAS ALREADY BEEN QUERIED FOR THIS TASK. The relevant " +
  "symbols are below.\n\n## CodeGraph\n- **Foo**\n";

describe("stripUserVisibleText", () => {
  it("cuts at the turn-context marker through end of string", () => {
    expect(stripUserVisibleText("fix the leak" + TRAILER)).toBe("fix the leak");
    expect(stripUserVisibleText("clean prose")).toBe("clean prose");
  });

  it("is idempotent", () => {
    const once = stripUserVisibleText("ship it" + TRAILER);
    expect(once).toBe("ship it");
    expect(stripUserVisibleText(once)).toBe(once);
  });

  it("clears standalone CODEGRAPH injection with no user prose", () => {
    const injection =
      "CODEGRAPH HAS ALREADY BEEN QUERIED FOR THIS TASK. symbols below.";
    expect(stripUserVisibleText(injection)).toBe("");
    expect(stripUserVisibleText("  " + injection)).toBe("");
  });
});

describe("transcriptResponseToItems strips trailers", () => {
  it("scrubs user text on the display mapping path", () => {
    const items = transcriptResponseToItems({
      display: [
        { type: "message", role: "user", text: "hello" + TRAILER },
        { type: "message", role: "assistant", text: "ok" + TRAILER },
      ],
    });
    const user = items.find((i) => i.kind === "msg" && i.msg.role === "user");
    const assistant = items.find(
      (i) => i.kind === "msg" && i.msg.role === "assistant",
    );
    expect(user && user.kind === "msg" ? user.msg.text : null).toBe("hello");
    // Assistant text is not scrubbed — only user-visible user turns.
    expect(assistant && assistant.kind === "msg" ? assistant.msg.text : null).toBe(
      "ok" + TRAILER,
    );
  });

  it("scrubs user text on the history fallback path", () => {
    const items = transcriptResponseToItems({
      history: [
        { role: "user", content: "from history" + TRAILER },
        { role: "assistant", content: "reply" },
      ],
    });
    const user = items.find((i) => i.kind === "msg" && i.msg.role === "user");
    expect(user && user.kind === "msg" ? user.msg.text : null).toBe("from history");
  });
});
