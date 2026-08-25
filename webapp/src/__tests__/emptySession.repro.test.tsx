import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  TranscriptList,
  clearActivityFoldPrefs,
  type Item,
} from "../components/TranscriptList";

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
  it("empty items → zero Working... and zero fold chrome", () => {
    render(<TranscriptList {...listProps([])} />);
    expectEmptyFeed();
  });

  it("Working... assistant crumb stays hidden and mounts no folds", () => {
    const items: Item[] = [
      { kind: "msg", msg: { role: "assistant", text: "Working..." } },
    ];
    render(<TranscriptList {...listProps(items)} />);
    expectEmptyFeed();
  });

  it("three Working... assistant crumbs still paint nothing", () => {
    const items: Item[] = [
      { kind: "msg", msg: { role: "assistant", text: "Working..." } },
      { kind: "msg", msg: { role: "assistant", text: "Working.." } },
      { kind: "msg", msg: { role: "assistant", text: "working..." } },
    ];
    render(<TranscriptList {...listProps(items)} />);
    expectEmptyFeed();
  });
});
