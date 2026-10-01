import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { useRef } from "react";
import { TranscriptList, type Item } from "../components/TranscriptList";
import { useTranscriptFind } from "../components/conversation/useTranscriptFind";
import { findMatches, paintFindHighlights, searchableRowText, stepMatch } from "../lib/transcriptFind";

describe("find matching", () => {
  it("searches message prose and steers, case-insensitively, in reading order", () => {
    expect(searchableRowText({ kind: "steer", text: "Probe it" })).toBe("Probe it");
    expect(searchableRowText({ kind: "msg", msg: { role: "assistant", text: "**Probe** done" } })).toBe("Probe done");
    expect(searchableRowText({ kind: "msg", msg: { role: "assistant", text: "x", workerStream: true } })).toBe("");
    expect(searchableRowText({ kind: "checkpoint", id: "c", label: "probe", trigger: "t" })).toBe("");
    expect(findMatches(["probe a PROBE", "", "no", "the probe"], " Probe ")).toEqual([
      { row: 0, occurrence: 0 }, { row: 0, occurrence: 1 }, { row: 3, occurrence: 0 },
    ]);
    expect(findMatches(["abc"], "  ")).toEqual([]);
  });

  it("wraps when stepping past either end", () => {
    expect(stepMatch(-1, 3, 1)).toBe(0);
    expect(stepMatch(-1, 3, -1)).toBe(2);
    expect(stepMatch(2, 3, 1)).toBe(0);
    expect(stepMatch(0, 3, -1)).toBe(2);
    expect(stepMatch(0, 0, 1)).toBe(-1);
  });
});

class FakeHighlight {
  ranges: Range[];
  constructor(...ranges: Range[]) { this.ranges = ranges; }
}

describe("find highlighting", () => {
  let registry: Map<string, FakeHighlight>;
  beforeEach(() => {
    registry = new Map();
    vi.stubGlobal("Highlight", FakeHighlight);
    vi.stubGlobal("CSS", { highlights: registry });
  });
  afterEach(() => vi.unstubAllGlobals());

  it("paints only message bodies and marks the active occurrence", () => {
    document.body.innerHTML = `
      <div id="feed">
        <div data-row-index="0"><span>probe label</span><div class="transcript-msg-body">a probe and a probe</div></div>
        <div data-row-index="1"><div class="transcript-msg-body">third <b>probe</b></div></div>
      </div>`;
    const feed = document.getElementById("feed");
    const current = paintFindHighlights(feed, "PROBE", { row: 0, occurrence: 1 });
    expect(registry.get("transcript-find")!.ranges).toHaveLength(3);
    expect(current?.startOffset).toBe(14);
    expect(registry.get("transcript-find-current")!.ranges).toEqual([current]);
    paintFindHighlights(feed, "", null);
    expect(registry.size).toBe(0);
  });
});

const ITEMS: Item[] = [
  { kind: "msg", msg: { role: "user", text: "run the probes" } },
  { kind: "msg", msg: { role: "assistant", text: "Probe one passed. Probe two passed." } },
  { kind: "steer", text: "skip probe three" },
];

function Harness({ items }: { items: Item[] }) {
  const feedRef = useRef<HTMLDivElement>(null);
  const columnRef = useRef<HTMLDivElement>(null);
  const { findApiRef, findBar } = useTranscriptFind(feedRef, columnRef, items, "S", 0);
  return (
    <div>
      <input aria-label="other pane editor" />
      <div ref={columnRef} className="chat-column">
        {findBar}
        <div ref={feedRef}>
          <TranscriptList
            items={items} status="done" compactingStatus={null} editingIndex={null} auto={false} plan={false}
            turnOpen={false} scrollContainerRef={feedRef} findApiRef={findApiRef}
            onEditMessage={vi.fn()} onExecuteSend={vi.fn()} onImageClick={vi.fn()} onSetCard={vi.fn()}
            onExecutePlan={vi.fn()} onCommandApproval={vi.fn()}
          />
        </div>
      </div>
    </div>
  );
}

describe("Cmd/Ctrl+F in chat", () => {
  it("opens, counts, steps with wraparound and closes", () => {
    render(<Harness items={ITEMS} />);
    expect(screen.queryByTestId("transcript-find")).toBeNull();
    act(() => { fireEvent.keyDown(document.body, { key: "f", metaKey: true }); });
    const input = screen.getByRole("textbox", { name: "Find in chat" });
    expect(document.activeElement).toBe(input);
    fireEvent.change(input, { target: { value: "probe" } });
    expect(screen.getByTestId("transcript-find-status")).toHaveTextContent("1 of 4");
    fireEvent.keyDown(input, { key: "Enter" });
    expect(screen.getByTestId("transcript-find-status")).toHaveTextContent("2 of 4");
    fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
    expect(screen.getByTestId("transcript-find-status")).toHaveTextContent("4 of 4");
    act(() => { fireEvent.keyDown(input, { key: "g", ctrlKey: true }); });
    expect(screen.getByTestId("transcript-find-status")).toHaveTextContent("1 of 4");
    fireEvent.change(input, { target: { value: "nothing here" } });
    expect(screen.getByTestId("transcript-find-status")).toHaveTextContent("No results");
    fireEvent.keyDown(input, { key: "Escape" });
    expect(screen.queryByTestId("transcript-find")).toBeNull();
  });

  it("leaves Cmd/Ctrl+F to other panes' editors", () => {
    render(<Harness items={ITEMS} />);
    const editor = screen.getByRole("textbox", { name: "other pane editor" });
    editor.focus();
    act(() => { fireEvent.keyDown(editor, { key: "f", ctrlKey: true }); });
    expect(screen.queryByTestId("transcript-find")).toBeNull();
  });
});
