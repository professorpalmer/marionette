import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { transcriptResponseToItems } from "../components/conversation/transcriptItems";
import { finalizePilotMessage } from "../components/conversation/streamApply";
import { TranscriptList, type Item } from "../components/TranscriptList";

afterEach(cleanup);

// Measured in the rig: a turn read "Worked for 9s" live and "Worked for 8s"
// after a reload. The live value is the busy clock, from the send to the end
// of the final answer. After a reload the fold counted only to the end of the
// last tool, so the time the model spent on the final answer was lost.
describe("Worked for after a reload", () => {
  it("counts from the user's message to the final answer", () => {
    const display = [
      { type: "message", role: "user", text: "run the probes", ts: 1_000 },
      { type: "card", id: "a", kind: "run_command", goal: "echo 1", result: { duration_ms: 20 }, ts: 2_400 },
      { type: "message", role: "assistant", text: "Halfway.", ts: 3_000 },
      { type: "card", id: "b", kind: "run_command", goal: "echo 2", result: { duration_ms: 30 }, ts: 4_970 },
      { type: "thinking", text: "Write it up.", duration_ms: 3_000, ts: 9_000 },
      { type: "message", role: "assistant", text: "Done.", ts: 9_600 },
    ];
    render(
      <TranscriptList items={transcriptResponseToItems({ display }, "s")} status="done" compactingStatus={null}
        editingIndex={null} auto={false} plan={false} turnOpen={false} scrollContainerRef={{ current: null }}
        onEditMessage={vi.fn()} onExecuteSend={vi.fn()} onImageClick={vi.fn()} onSetCard={vi.fn()} onExecutePlan={vi.fn()} />,
    );
    expect(screen.getByTestId("turn-work-fold").textContent).toBe("Worked for 8s");
  });
});

describe("final answer time on the live row", () => {
  // A reload first paints the cached live rows, and the server rows with the
  // same fingerprint do not replace them. So the live final answer must carry
  // the time the server saved for it, or the fold loses its end after a reload.
  const user: Item = { kind: "msg", msg: { role: "user", text: "go" } };
  it("seals a streamed bubble with the server time", () => {
    const items: Item[] = [user, { kind: "msg", msg: { role: "assistant", text: "Do", streaming: true } }];
    const out = finalizePilotMessage(items, "Done.", { streamed: true, ts: 9_600 });
    expect(out.at(-1)).toMatchObject({ kind: "msg", msg: { text: "Done.", ts: 9_600 } });
  });
  it("stamps a bubble that a done frame already sealed with the same text", () => {
    const items: Item[] = [user, { kind: "msg", msg: { role: "assistant", text: "Done." } }];
    const out = finalizePilotMessage(items, "Done.", { streamed: true, ts: 9_600 });
    expect(out.at(-1)).toMatchObject({ kind: "msg", msg: { text: "Done.", ts: 9_600 } });
  });
  it("stamps a new final answer", () => {
    const out = finalizePilotMessage([user], "Done.", { ts: 9_600 });
    expect(out.at(-1)).toMatchObject({ kind: "msg", msg: { text: "Done.", ts: 9_600 } });
  });
});
