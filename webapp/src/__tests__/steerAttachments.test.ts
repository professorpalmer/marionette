import { describe, expect, it } from "vitest";
import { transcriptResponseToItems } from "../components/Conversation";
import { documentChips, steerTranscriptItem } from "../components/conversation/composerSend";

describe("steer and message attachments", () => {
  it("live steer rows carry what was attached", () => {
    expect(steerTranscriptItem({
      text: "look",
      chrome: "steer",
      images: [{ path: "/up/a.png", name: "a.png", previewUrl: "blob:x" }],
      documents: [{ name: "notes.txt" }],
    })).toEqual({
      kind: "steer",
      text: "look",
      images: [{ path: "/up/a.png", name: "a.png", previewUrl: "blob:x" }],
      documents: [{ name: "notes.txt" }],
    });
    expect(steerTranscriptItem({ text: "look", chrome: "queue", images: [] })).toBeNull();
  });

  it("names document chips from the name, else the locator basename", () => {
    expect(documentChips([
      { path: "/tmp/uploads/report.pdf" },
      { ref: "input:9", name: "spec.md" },
      { path: "C:\\up\\win.txt" },
    ])).toEqual([{ name: "report.pdf" }, { name: "spec.md" }, { name: "win.txt" }]);
  });

  it("reload restores steer chrome and attachments from the display transcript", () => {
    const items = transcriptResponseToItems({
      display: [
        { type: "message", role: "user", text: "check these", input_id: "in-1",
          attachments: [{ kind: "image", name: "a.png", ref: "input:a" }, { kind: "document", name: "n.txt", ref: "input:n" }] },
        { type: "message", role: "assistant", text: "ok" },
        { type: "message", role: "user", text: "also this", input_id: "in-2", steer: true,
          attachments: [{ kind: "image", name: "b.png", ref: "input:b" }] },
      ],
    }, "S");
    expect(items[0]).toMatchObject({
      kind: "msg",
      msg: { role: "user", text: "check these",
        images: [{ path: "input:a", name: "a.png", previewUrl: "" }], documents: [{ name: "n.txt" }] },
    });
    expect(items[2]).toEqual({
      kind: "steer", text: "also this", images: [{ path: "input:b", name: "b.png", previewUrl: "" }],
    });
  });

  it("ignores malformed attachment metadata", () => {
    const items = transcriptResponseToItems({
      display: [{ type: "message", role: "user", text: "hi", attachments: [null, { kind: "image" }, { kind: "document" }, "x"] }],
    }, "S");
    expect(items[0]).toMatchObject({ kind: "msg", msg: { role: "user", text: "hi" } });
    const msg = (items[0] as { msg: Record<string, unknown> }).msg;
    expect("images" in msg || "documents" in msg).toBe(false);
  });
});

import { appendStreamingTextToItems } from "../components/conversation/streamBubbles";
import type { Item } from "../components/TranscriptList";

describe("a steer row mid-stream", () => {
  it("does not split the pilot's open bubble (no duplicated tail)", () => {
    const items: Item[] = [
      { kind: "msg", msg: { role: "user", text: "run the probes" } },
      { kind: "msg", msg: { role: "assistant", text: "Now let me run probe", streaming: true } },
      { kind: "steer", text: "look at this too" },
    ];
    const next = appendStreamingTextToItems(items, " 1 and see what it prints.");
    expect(next).toHaveLength(3);
    expect(next[1]).toMatchObject({ msg: { text: "Now let me run probe 1 and see what it prints." } });
    expect(next[2]).toEqual({ kind: "steer", text: "look at this too" });
  });
});
