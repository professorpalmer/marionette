import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import column from "../components/conversation/ConversationChatColumn.tsx?raw";
import helpers from "../components/conversation/feedMotion.tsx?raw";
import list from "../components/TranscriptList.tsx?raw";
import pkgJson from "../../package.json?raw";
import { TranscriptList, type Item } from "../components/TranscriptList";
import { feedLayoutMotionEnabled } from "../components/conversation/feedMotion";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function listProps(items: Item[]) {
  return {
    items,
    status: "done" as const,
    compactingStatus: null as string | null,
    editingIndex: null as number | null,
    auto: false,
    plan: false,
    turnOpen: false,
    scrollContainerRef: { current: null },
    onEditMessage: vi.fn(),
    onExecuteSend: vi.fn(),
    onImageClick: vi.fn(),
    onSetCard: vi.fn(),
    onExecutePlan: vi.fn(),
    onCommandApproval: vi.fn(),
  };
}

describe("feed Motion (v0.9.318)", () => {
  it("declares the official motion package in webapp dependencies", () => {
    const pkg = JSON.parse(pkgJson) as {
      dependencies?: Record<string, string>;
    };
    expect(pkg.dependencies?.motion).toBeTruthy();
    expect(pkg.dependencies?.["framer-motion"]).toBeUndefined();
  });

  it("mounts transcript rows inside motion layout shells", () => {
    render(
      <TranscriptList
        {...listProps([
          { kind: "msg", msg: { role: "user", text: "hello" } },
          { kind: "msg", msg: { role: "assistant", text: "hi there" } },
        ])}
      />,
    );
    expect(screen.getByTestId("transcript-virtual-list")).toBeTruthy();
    expect(screen.getByText("hello")).toBeTruthy();
    expect(screen.getByText("hi there")).toBeTruthy();
  });

  it("disables feed layout motion when prefers-reduced-motion is set", () => {
    expect(feedLayoutMotionEnabled(true)).toBe(false);
    expect(feedLayoutMotionEnabled(false)).toBe(true);
    expect(feedLayoutMotionEnabled(null)).toBe(true);
  });

  it("keeps layoutScroll on the feed scrollport and popLayout on list presence", () => {
    expect(column).toContain("layoutScroll");
    expect(column).toContain("[overflow-anchor:auto]");
    expect(column).toContain("[scroll-padding-bottom:var(--feed-chrome-clearance");
    expect(column).not.toContain("overflow-anchor:none");
    expect(helpers).toContain('mode="popLayout"');
    expect(list).toContain("FeedMotionPresence");
    expect(list).toContain("useVirtualizer");
    expect(list).not.toMatch(/@stylexjs|create\(|stylex\./);
  });
});
