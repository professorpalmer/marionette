import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { TranscriptList, type Item } from "../components/TranscriptList";
import { feedLayoutMotionEnabled } from "../components/conversation/feedMotion";

const webappRoot = join(dirname(fileURLToPath(import.meta.url)), "../..");

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
    const pkg = JSON.parse(
      readFileSync(join(webappRoot, "package.json"), "utf8"),
    ) as { dependencies?: Record<string, string> };
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
    const column = readFileSync(
      join(webappRoot, "src/components/conversation/ConversationChatColumn.tsx"),
      "utf8",
    );
    const helpers = readFileSync(
      join(webappRoot, "src/components/conversation/feedMotion.tsx"),
      "utf8",
    );
    const list = readFileSync(
      join(webappRoot, "src/components/TranscriptList.tsx"),
      "utf8",
    );
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
