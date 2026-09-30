import { describe, expect, it } from "vitest";
import type { Item } from "../components/TranscriptList";
import { stampTranscriptMessageIds, withLiveMessageId } from "../components/conversation/transcriptRowIdentity";

const msg = (text: string, extra: object = {}): Item => ({ kind: "msg", msg: { role: "assistant", text, ...extra } });

describe("message identity", () => {
  it("never reuses an id after a worker preview is dropped", () => {
    const stamped = stampTranscriptMessageIds([
      msg("A"),
      msg("worker tokens", { workerStream: true, worker_id: "w1" }),
      msg("B"),
    ], "s");
    const withoutPreview = stamped.filter((it) => !(it.kind === "msg" && it.msg.workerStream));
    const next = withLiveMessageId(withoutPreview, { role: "assistant", text: "C" }, "s");
    const ids = [...withoutPreview.map((it) => (it.kind === "msg" ? it.msg.id : "")), next.id];
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("gives live and reloaded transcripts the same ids for durable messages", () => {
    const live = stampTranscriptMessageIds([
      msg("A"),
      msg("worker tokens", { workerStream: true, worker_id: "w1" }),
      msg("B"),
    ], "s");
    const reloaded = stampTranscriptMessageIds([msg("A"), msg("B")], "s");
    const durable = (items: Item[]) => items.flatMap((it) => (it.kind === "msg" && !it.msg.workerStream ? [it.msg.id] : []));
    expect(durable(live)).toEqual(durable(reloaded));
  });

  it("keys parallel worker previews apart", () => {
    const stamped = stampTranscriptMessageIds([
      msg("x", { workerStream: true, worker_id: "w1" }),
      msg("y", { workerStream: true, worker_id: "w2" }),
    ], "s");
    const ids = stamped.map((it) => (it.kind === "msg" ? it.msg.id : ""));
    expect(new Set(ids).size).toBe(2);
  });
});
