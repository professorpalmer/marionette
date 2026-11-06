import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  TranscriptList,
  clearActivityFoldPrefs,
  countPaintableTranscriptItems,
  type Item,
} from "../components/TranscriptList";
import TranscriptEmptyState from "../components/conversation/TranscriptEmptyState";

afterEach(() => {
  cleanup();
  clearActivityFoldPrefs();
});

function listProps(items: Item[]) {
  return {
    items,
    status: "idle" as const,
    compactingStatus: null as string | null,
    editingIndex: null as number | null,
    auto: false,
    plan: false,
    turnOpen: false,
    holdSwarmAwait: false,
    scrollContainerRef: { current: null },
    onEditMessage: vi.fn(),
    onExecuteSend: vi.fn(),
    onImageClick: vi.fn(),
    onSetCard: vi.fn(),
    onExecutePlan: vi.fn(),
    onCommandApproval: vi.fn(),
  };
}

function expectEmptyFeed() {
  expect(screen.queryByText("Working...")).toBeNull();
  expect(screen.queryByText(/Working\.\.\.?/i)).toBeNull();
  expect(screen.queryByTestId("activity-fold")).toBeNull();
  expect(screen.queryByTestId("thought-fold")).toBeNull();
  expect(screen.queryByTestId("ran-commands-fold")).toBeNull();
}

describe("empty session folds", () => {
  it.each<[string, Item[]]>([
    ["no items", []],
    ["one Working... crumb", [
      { kind: "msg", msg: { role: "assistant", text: "Working..." } },
    ]],
    ["three Working... crumbs", [
      { kind: "msg", msg: { role: "assistant", text: "Working..." } },
      { kind: "msg", msg: { role: "assistant", text: "Working.." } },
      { kind: "msg", msg: { role: "assistant", text: "working..." } },
    ]],
  ])("%s paints no Working... and no fold chrome", (_name, items) => {
    render(<TranscriptList {...listProps(items)} />);
    expectEmptyFeed();
  });

  it("countPaintableTranscriptItems treats Working... crumbs as empty", () => {
    const items: Item[] = [
      { kind: "msg", msg: { role: "assistant", text: "Working..." } },
      { kind: "msg", msg: { role: "assistant", text: "Working.." } },
    ];
    expect(countPaintableTranscriptItems(items)).toBe(0);
    expect(countPaintableTranscriptItems([])).toBe(0);
    expect(
      countPaintableTranscriptItems([
        { kind: "msg", msg: { role: "user", text: "hello" } },
      ]),
    ).toBe(1);
  });

  it("TranscriptEmptyState shows pilot copy when crumbs are paint-invisible", () => {
    const items: Item[] = [
      { kind: "msg", msg: { role: "assistant", text: "Working..." } },
    ];
    render(
      <TranscriptEmptyState
        transcriptStale={false}
        itemCount={countPaintableTranscriptItems(items)}
      />,
    );
    expect(
      screen.getByText("Message the pilot. It plans, investigates via swarms, and explains."),
    ).toBeTruthy();
  });
});
