import { createRef, type ReactNode } from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ConversationChatColumn from "../components/conversation/ConversationChatColumn";
import {
  FEED_CONTENT_PADDING_BOTTOM_PX,
  FEED_SCROLLPORT_OVERFLOW_ANCHOR,
  FEED_SCROLLPORT_SCROLL_PADDING_BOTTOM_PX,
  feedContentLayoutClass,
} from "../components/conversation/feedScroll";
import type { Item } from "../components/TranscriptList";

afterEach(() => {
  cleanup();
});

function renderColumn(opts: {
  items?: Item[];
  composerDock?: ReactNode;
  status?: "idle" | "thinking" | "executing" | "done" | "error" | "streaming" | "awaiting_swarm";
} = {}) {
  const feedRef = createRef<HTMLDivElement>();
  const feedContentRef = createRef<HTMLDivElement>();
  render(
    <div style={{ height: 640, display: "flex", flexDirection: "column" }}>
      <ConversationChatColumn
        feedRef={feedRef}
        feedContentRef={feedContentRef}
        transcriptStale={false}
        items={opts.items ?? []}
        status={opts.status ?? "idle"}
        compactingStatus={null}
        editingIndex={null}
        auto={false}
        plan={false}
        busyElapsedMs={null}
        turnOpen={false}
        onEditMessage={vi.fn()}
        onExecuteSend={vi.fn()}
        onImageClick={vi.fn()}
        onSetCard={vi.fn()}
        onExecutePlan={vi.fn()}
        onCommandApproval={vi.fn()}
        composerDock={opts.composerDock ?? <div data-testid="fake-composer">Message the composer</div>}
      />
    </div>,
  );
  return { feedRef, feedContentRef };
}

describe("chat column feed alignment", () => {
  it("top-aligns empty-session greeting above the composer dock", () => {
    const { feedRef, feedContentRef } = renderColumn();
    const greeting = screen.getByText(
      "Message the pilot. It plans, investigates via swarms, and explains.",
    );
    const scrollport = screen.getByTestId("transcript-feed-scrollport");
    const content = screen.getByTestId("transcript-feed-content");
    const composer = screen.getByTestId("composer-chrome");

    expect(feedRef.current).toBe(scrollport);
    expect(feedContentRef.current).toBe(content);
    expect(scrollport.contains(greeting)).toBe(true);
    expect(composer.contains(greeting)).toBe(false);
    expect(content.className).toBe(feedContentLayoutClass());
    expect(content.style.paddingBottom).toBe(`${FEED_CONTENT_PADDING_BOTTOM_PX}px`);
    expect(scrollport.style.overflowAnchor).toBe(FEED_SCROLLPORT_OVERFLOW_ANCHOR);
    expect(scrollport.style.scrollPaddingBottom).toBe(
      `${FEED_SCROLLPORT_SCROLL_PADDING_BOTTOM_PX}px`,
    );
    expect(content.compareDocumentPosition(composer) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("keeps a short transcript in the feed column, not under the composer", () => {
    renderColumn({
      items: [
        { kind: "msg", msg: { role: "user", text: "hello" } },
        {
          kind: "thinking",
          text: "planning the next step",
          id: "th-short",
          duration_ms: 3000,
        },
      ],
    });
    const content = screen.getByTestId("transcript-feed-content");
    expect(content.textContent).toContain("hello");
    expect(screen.getByTestId("composer-chrome").textContent).not.toContain("hello");
    expect(content.className).toContain("min-h-full");
    expect(content.className).toContain("justify-start");
    expect(content.className).not.toContain("justify-end");
  });
});
